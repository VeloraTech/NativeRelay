"""NativeRelay command-line interface."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from dataclasses import asdict
from datetime import datetime, timezone

from collectors.linux import LinuxCollector
from .model import EventType
from .stream import EventStream, StreamClosed

RUN_EVENT_TYPES = {
    EventType.PROCESS_STARTED,
    EventType.PROCESS_EXITED,
    EventType.FILE_OPENED,
    EventType.FILE_MODIFIED,
}
EXIT_OK = 0
EXIT_ARGUMENT_ERROR = 2
EXIT_COLLECTOR_UNAVAILABLE = 3
EXIT_RUNTIME_FAILURE = 4


def main(argv=None):
    parser = argparse.ArgumentParser(prog="nativerelay", description="Observe local Linux kernel events")
    commands = parser.add_subparsers(dest="command", required=True)

    observe = commands.add_parser("observe", help="stream process events and scoped file open/write events")
    observe.add_argument("--scope", required=True, help="existing directory to include in file events")
    observe.add_argument("--include-paths", action="append", default=[], metavar="PATTERN",
                         help="fnmatch pattern for paths to include (can be repeated)")
    observe.add_argument("--exclude-paths", action="append", default=[], metavar="PATTERN",
                         help="fnmatch pattern for paths to exclude (can be repeated)")
    observe.add_argument("--json", action="store_true", help="emit one JSON event per line")
    observe.add_argument("--include-command", action="store_true", help="include argv (may contain sensitive data)")

    capabilities = commands.add_parser("capabilities", help="show collector capabilities for a scope")
    capabilities.add_argument("--scope", required=True)
    capabilities.add_argument("--include-paths", action="append", default=[], metavar="PATTERN",
                              help="fnmatch pattern for paths to include (can be repeated)")
    capabilities.add_argument("--exclude-paths", action="append", default=[], metavar="PATTERN",
                              help="fnmatch pattern for paths to exclude (can be repeated)")

    run = commands.add_parser("run", help="stream versioned JSONL records for machine consumers")
    run.add_argument("--format", choices=("json",), default="json", help="output format (currently: json/JSONL)")
    run.add_argument("--scope", default=os.getcwd(), help="directory scope for file events (default: current directory)")
    run.add_argument("--include-paths", action="append", default=[], metavar="PATTERN",
                     help="fnmatch pattern for paths to include (can be repeated)")
    run.add_argument("--exclude-paths", action="append", default=[], metavar="PATTERN",
                     help="fnmatch pattern for paths to exclude (can be repeated)")
    run.add_argument("--include-command", action="store_true", help="include argv (may contain sensitive data)")

    args = parser.parse_args(argv)
    if args.command == "run":
        return _run_json(args.scope,
                         include_command=args.include_command,
                         include_paths=args.include_paths or None,
                         exclude_paths=args.exclude_paths or None)

    try:
        collector = LinuxCollector(
            args.scope,
            include_command=getattr(args, "include_command", False),
            include_paths=getattr(args, "include_paths", None) or None,
            exclude_paths=getattr(args, "exclude_paths", None) or None,
        )
    except (OSError, ValueError) as exc:
        print(f"nativerelay: {exc}", file=sys.stderr)
        return EXIT_COLLECTOR_UNAVAILABLE

    if args.command == "capabilities":
        stream = EventStream(1)
        try:
            collector.start(stream)
            print(json.dumps({"platform": "linux", "collector": collector.name,
                              "scope": str(collector.scope), "health": collector.status,
                              "capabilities": [_capability_record(c) for c in collector.capabilities]}, indent=2))
        finally:
            collector.stop()
        return EXIT_OK

    stream = EventStream()
    try:
        collector.start(stream)
        print("NativeRelay Linux observe (Ctrl-C to stop)", file=sys.stderr)
        last_dropped = -1
        last_loss_generation = -1
        while True:
            try:
                event = stream.receive(timeout=1)
            except TimeoutError:
                status = collector.status
                if status["errors"] or status["dropped"] != last_dropped or status["loss_generation"] != last_loss_generation:
                    print(json.dumps({"collector_status": status}), file=sys.stderr)
                    collector.health["errors"].clear()
                    last_dropped = status["dropped"]
                    last_loss_generation = status["loss_generation"]
                continue
            print(event.to_json() if args.json else _human(event), flush=True)
    except KeyboardInterrupt:
        return EXIT_OK
    finally:
        collector.stop()


def _run_json(scope, *, include_command=False, include_paths=None, exclude_paths=None,
              collector_factory=None, stdout=None, stderr=None):
    """Run JSONL event streaming; injectable dependencies keep lifecycle testable."""
    output = stdout if stdout is not None else sys.stdout
    diagnostics = stderr if stderr is not None else sys.stderr
    writer = _JsonlWriter(output)
    stream = EventStream()
    collector = None
    started = False
    exit_code = EXIT_OK
    last_loss_generation = 0
    shutdown = threading.Event()
    shutdown_signal = [None]
    previous_handlers = {}

    def handle_signal(signum, _frame):
        shutdown_signal[0] = signal.Signals(signum).name
        shutdown.set()

    for signum in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if signum is None:
            continue
        try:
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, handle_signal)
        except (ValueError, OSError):
            previous_handlers.pop(signum, None)

    try:
        factory = collector_factory or LinuxCollector
        try:
            collector = factory(scope, include_command=include_command,
                               include_paths=include_paths, exclude_paths=exclude_paths)
        except Exception as exc:
            _diagnostic(diagnostics, f"collector initialization failed: {exc}")
            writer.control("nativerelay.status", state="failed", platform="linux",
                           collector="linux-kernel", scope=scope, error=str(exc), exit_code=EXIT_COLLECTOR_UNAVAILABLE)
            exit_code = EXIT_COLLECTOR_UNAVAILABLE

        if collector is not None:
            try:
                collector.start(stream)
                started = True
            except Exception as exc:
                _diagnostic(diagnostics, f"collector start failed: {exc}")
                writer.control("nativerelay.status", state="failed", platform="linux",
                               collector=getattr(collector, "name", "unknown"), scope=scope,
                               error=str(exc), exit_code=EXIT_COLLECTOR_UNAVAILABLE)
                exit_code = EXIT_COLLECTOR_UNAVAILABLE

        if started:
            status = _collector_status(collector)
            state = _collection_state(collector)
            writer.control("nativerelay.status", state=state, platform="linux",
                           collector=getattr(collector, "name", "linux-kernel"), scope=scope,
                           capabilities=_capabilities(collector), health=status)
            _report_diagnostics(diagnostics, state, status)
            last_status_signature = _status_signature(collector)
            last_loss_generation = 0
            _emit_loss_if_changed(writer, collector, stream, last_loss_generation)

            if state == "unavailable":
                exit_code = EXIT_COLLECTOR_UNAVAILABLE
            else:
                while not shutdown.is_set():
                    try:
                        event = stream.receive(timeout=0.25)
                    except TimeoutError:
                        pass
                    except StreamClosed:
                        if stream.error is not None:
                            _diagnostic(diagnostics, f"event stream failed: {stream.error}")
                            writer.control("nativerelay.status", state="failed", platform="linux",
                                           collector=getattr(collector, "name", "linux-kernel"),
                                           scope=scope, error=str(stream.error), exit_code=EXIT_RUNTIME_FAILURE)
                            exit_code = EXIT_RUNTIME_FAILURE
                        break
                    else:
                        writer.event(event)

                    last_loss_generation = _emit_loss_if_changed(
                        writer, collector, stream, last_loss_generation
                    )
                    current_signature = _status_signature(collector)
                    if current_signature != last_status_signature:
                        current_state = _collection_state(collector)
                        current_status = _collector_status(collector)
                        writer.control("nativerelay.status", state=current_state, platform="linux",
                                       collector=getattr(collector, "name", "linux-kernel"), scope=scope,
                                       capabilities=_capabilities(collector), health=current_status)
                        _report_diagnostics(diagnostics, current_state, current_status)
                        last_status_signature = current_signature
                        if current_state == "unavailable":
                            _diagnostic(diagnostics, "all supported collection sources became unavailable")
                            exit_code = EXIT_RUNTIME_FAILURE
                            break

    except BrokenPipeError:
        # A downstream consumer closed its input; treat that as a normal stop.
        exit_code = EXIT_OK
    except Exception as exc:
        _diagnostic(diagnostics, f"runtime failure: {exc}")
        try:
            writer.control("nativerelay.status", state="failed", platform="linux",
                           collector=getattr(collector, "name", "linux-kernel"), scope=scope,
                           error=str(exc), exit_code=EXIT_RUNTIME_FAILURE)
        except BrokenPipeError:
            pass
        exit_code = EXIT_RUNTIME_FAILURE
    finally:
        if collector is not None:
            try:
                collector.stop()
            except Exception as exc:
                _diagnostic(diagnostics, f"collector shutdown failed: {exc}")
                if exit_code == EXIT_OK:
                    exit_code = EXIT_RUNTIME_FAILURE

        if started and not writer.broken:
            # stop() closes the stream; drain all queued events before the final record.
            while True:
                try:
                    writer.event(stream.receive(timeout=0))
                except TimeoutError:
                    break
                except StreamClosed:
                    break
                except BrokenPipeError:
                    exit_code = EXIT_OK
                    break
            try:
                _emit_loss_if_changed(writer, collector, stream, last_loss_generation)
                final_state = "stopped" if exit_code == EXIT_OK else "failed"
                writer.control("nativerelay.status", state=final_state, platform="linux",
                               collector=getattr(collector, "name", "linux-kernel"), scope=scope,
                               capabilities=_capabilities(collector), health=_collector_status(collector),
                               clean_shutdown=exit_code == EXIT_OK,
                               shutdown_signal=shutdown_signal[0], exit_code=exit_code)
                if exit_code == EXIT_OK:
                    _diagnostic(diagnostics, "collection stopped")
            except BrokenPipeError:
                exit_code = EXIT_OK

        for signum, previous in previous_handlers.items():
            try:
                signal.signal(signum, previous)
            except (ValueError, OSError):
                pass

    return exit_code


class _JsonlWriter:
    def __init__(self, stream):
        self.stream = stream
        self.broken = False

    def _write(self, record):
        try:
            self.stream.write(json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n")
            self.stream.flush()
        except BrokenPipeError:
            self.broken = True
            raise

    def event(self, event):
        # Event.to_json is the canonical Event schema and preserves every field.
        try:
            self.stream.write(event.to_json() + "\n")
            self.stream.flush()
        except BrokenPipeError:
            self.broken = True
            raise

    def control(self, record_type, **fields):
        self._write({"schema_version": 1, "record_type": record_type,
                     "timestamp": datetime.now(timezone.utc).isoformat(), **fields})


def _capability_record(capability):
    return {**asdict(capability), "event_type": capability.event_type.value,
            "status": capability.status.value}


def _capabilities(collector):
    return [_capability_record(capability) for capability in collector.capabilities]


def _collector_status(collector):
    status = getattr(collector, "status", {})
    return dict(status) if isinstance(status, dict) else {}


def _collection_state(collector):
    supported = [cap for cap in collector.capabilities
                 if cap.event_type in RUN_EVENT_TYPES]
    active = [cap for cap in supported if cap.status.value == "available"]
    if not active:
        return "unavailable"
    if len(active) != len(RUN_EVENT_TYPES):
        return "degraded"
    return "running"


def _status_signature(collector):
    status = _collector_status(collector)
    health = {key: value for key, value in status.items()
              if key not in ("losses", "loss_generation")}
    payload = {"capabilities": _capabilities(collector), "health": health}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _emit_loss_if_changed(writer, collector, stream, previous_generation):
    status = _collector_status(collector)
    generation = status.get("loss_generation", stream.loss_generation)
    if generation > previous_generation:
        writer.control("nativerelay.loss", loss_generation=generation,
                       losses=status.get("losses", stream.losses),
                       collector=getattr(collector, "name", "linux-kernel"))
    return generation


def _report_diagnostics(stderr, state, status):
    if state != "running":
        _diagnostic(stderr, f"collection state: {state}")
    for error in status.get("errors", []):
        _diagnostic(stderr, str(error))


def _diagnostic(stderr, message):
    print(f"nativerelay: {message}", file=stderr, flush=True)


def _human(event):
    proc = f"pid={event.process.pid}"
    if event.process.parent_pid is not None: proc += f" ppid={event.process.parent_pid}"
    if event.process.executable: proc += f" exe={event.process.executable}"
    resource = f" {event.resource.path}" if event.resource else ""
    metadata = " " + json.dumps(event.metadata) if event.metadata else ""
    return f"{event.timestamp} {event.type.value} {proc}{resource}{metadata}"


if __name__ == "__main__":
    raise SystemExit(main())