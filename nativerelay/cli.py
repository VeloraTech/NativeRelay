"""NativeRelay command-line interface."""
from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone

from .hardware import get_hardware_profile, get_resource_snapshot
from .stream import EventStream, StreamClosed

LinuxCollector = None

EXIT_OK = 0
EXIT_ARGUMENT_ERROR = 2
EXIT_COLLECTOR_UNAVAILABLE = 3
EXIT_RUNTIME_FAILURE = 4


def main(argv=None):
    parser = argparse.ArgumentParser(prog="nativerelay", description="Observe local operating-system events")
    commands = parser.add_subparsers(dest="command", required=True)

    observe = commands.add_parser("observe", help="stream process and scoped file events")
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

    hardware = commands.add_parser("hardware", help="show hardware profile and resource snapshots")
    hardware.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    hardware.add_argument("--watch", action="store_true", help="continuously stream dynamic resource snapshots")

    args = parser.parse_args(argv)
    if args.command == "run":
        return _run_json(args.scope,
                         include_command=args.include_command,
                         include_paths=args.include_paths or None,
                         exclude_paths=args.exclude_paths or None)

    if args.command == "hardware":
        return _hardware(args.json, args.watch)

    try:
        factory = _platform_collector()
        collector = factory(
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
            print(json.dumps({"platform": getattr(collector, "platform", _platform_id()),
                              "collector": collector.name,
                              "scope": str(collector.scope), "health": collector.status,
                              "capabilities": [_capability_record(c) for c in collector.capabilities]}, indent=2))
        finally:
            collector.stop()
        return EXIT_OK

    stream = EventStream()
    try:
        collector.start(stream)
        print(f"NativeRelay {getattr(collector, 'platform', platform.system().lower())} observe (Ctrl-C to stop)",
              file=sys.stderr)
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


def _hardware(emit_json: bool = False, watch: bool = False) -> int:
    """Display hardware profile or stream dynamic resource snapshots."""
    if watch:
        return _hardware_watch(emit_json)
    try:
        profile = get_hardware_profile()
    except Exception as exc:  # noqa: BLE001 - surface collection errors to the user
        print(f"nativerelay: hardware profile collection failed: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_FAILURE
    if emit_json:
        print(profile.to_json())
    else:
        _print_human_profile(profile)
    return EXIT_OK


def _hardware_watch(emit_json: bool = False) -> int:
    """Continuously stream dynamic resource snapshots until interrupted."""
    interval = 1.0
    try:
        while True:
            try:
                snapshot = get_resource_snapshot()
            except Exception as exc:  # noqa: BLE001 - surface collection errors to the user
                print(f"nativerelay: resource snapshot failed: {exc}", file=sys.stderr)
                return EXIT_RUNTIME_FAILURE
            if emit_json:
                print(snapshot.to_json(), flush=True)
            else:
                _print_human_snapshot(snapshot)
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                break
    except KeyboardInterrupt:
        pass
    return EXIT_OK


def _print_human_profile(profile) -> None:
    """Print a human-readable hardware profile."""
    print(f"Hardware Profile ({profile.metadata.contract_id} v{profile.metadata.contract_version})")
    print(f"  Platform: {profile.metadata.platform} | Collected: {profile.metadata.collected_at}")
    print(f"  OS: {profile.os.name} {profile.os.version} ({profile.os.architecture}) [{profile.os.availability.value}]")
    print(f"  CPU: {profile.cpu.vendor.value} {profile.cpu.brand} "
          f"({profile.cpu.logical_count} logical" +
          (f", {profile.cpu.physical_count} physical" if profile.cpu.physical_count else "") +
          f") [{profile.cpu.availability.value}]")
    if profile.cpu.max_frequency_mhz is not None:
        print(f"    Max frequency: {profile.cpu.max_frequency_mhz} MHz")
    mem = profile.memory
    print(f"  Memory: {mem.total_bytes / (1024 ** 3):.2f} GB total" +
          (f", {mem.available_bytes / (1024 ** 3):.2f} GB available" if mem.available_bytes is not None else "") +
          (f", {mem.usage_percent:.1f}% used" if mem.usage_percent is not None else "") +
          f" [{mem.availability.value}]")
    if profile.gpus:
        print("  GPUs:")
        for gpu in profile.gpus:
            vram = f", {gpu.vram_bytes / (1024 ** 3):.2f} GB VRAM" if gpu.vram_bytes is not None else ""
            print(f"    {gpu.vendor.value} {gpu.model}{vram} [{gpu.availability.value}]")
    else:
        print("  GPUs: none")
    if profile.power:
        bat = profile.power.battery
        bat_str = ""
        if bat:
            bat_str = (
                (f", charge={bat.charge_percent}%" if bat.charge_percent is not None else "")
                + (f", charging={bat.charging}" if bat.charging is not None else "")
            )
        print(f"  Power: {profile.power.source.value}, AC={profile.power.ac_present}{bat_str} "
              f"[{profile.power.availability.value}]")
    else:
        print("  Power: unavailable")
    if profile.thermal:
        print("  Thermal:")
        for zone in profile.thermal:
            temp = f"{zone.temperature_celsius}°C" if zone.temperature_celsius is not None else "n/a"
            print(f"    {zone.id}: {temp} [{zone.availability.value}]")
    else:
        print("  Thermal: none")
    if profile.storage:
        print("  Storage:")
        for dev in profile.storage:
            print(f"    {dev.device_id} ({dev.type.value}, {dev.size_bytes / (1024 ** 3):.2f} GB) [{dev.availability.value}]")
    else:
        print("  Storage: none")
    if profile.displays:
        print("  Displays:")
        for disp in profile.displays:
            refresh = f", {disp.refresh_rate_hz} Hz" if disp.refresh_rate_hz is not None else ""
            print(f"    {disp.id} ({disp.type.value}, {disp.width_px}x{disp.height_px}{refresh}) [{disp.availability.value}]")
    else:
        print("  Displays: none")


def _print_human_snapshot(snapshot) -> None:
    """Print a single human-readable resource snapshot line."""
    parts = [f"collected_at={snapshot.collected_at}"]
    if snapshot.cpu_usage_percent is not None:
        parts.append(f"cpu={snapshot.cpu_usage_percent:.1f}%")
    if snapshot.memory_usage_percent is not None:
        parts.append(f"mem={snapshot.memory_usage_percent:.1f}%")
    if snapshot.memory_usage_bytes is not None:
        parts.append(f"mem_bytes={snapshot.memory_usage_bytes}")
    if snapshot.disk_read_bytes_per_sec is not None:
        parts.append(f"disk_read={snapshot.disk_read_bytes_per_sec:.1f} B/s")
    if snapshot.disk_write_bytes_per_sec is not None:
        parts.append(f"disk_write={snapshot.disk_write_bytes_per_sec:.1f} B/s")
    if snapshot.network_sent_bytes_per_sec is not None:
        parts.append(f"net_sent={snapshot.network_sent_bytes_per_sec:.1f} B/s")
    if snapshot.network_recv_bytes_per_sec is not None:
        parts.append(f"net_recv={snapshot.network_recv_bytes_per_sec:.1f} B/s")
    if snapshot.battery_charge_percent is not None:
        parts.append(f"battery={snapshot.battery_charge_percent:.1f}%")
    if snapshot.battery_charging is not None:
        parts.append(f"charging={snapshot.battery_charging}")
    if snapshot.temperature_celsius is not None:
        parts.append(f"temp={snapshot.temperature_celsius}°C")
    parts.append(f"availability={snapshot.availability.value}")
    print(" | ".join(parts), flush=True)


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

    for signum in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None),
                   getattr(signal, "SIGBREAK", None)):
        if signum is None:
            continue
        try:
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, handle_signal)
        except (ValueError, OSError):
            previous_handlers.pop(signum, None)

    try:
        platform_name = _platform_id()
        factory = collector_factory or _platform_collector()
        try:
            collector = factory(scope, include_command=include_command,
                               include_paths=include_paths, exclude_paths=exclude_paths)
        except Exception as exc:
            _diagnostic(diagnostics, f"collector initialization failed: {exc}")
            writer.control("nativerelay.status", state="failed", platform=platform_name,
                           collector="native", scope=scope, error=str(exc), exit_code=EXIT_COLLECTOR_UNAVAILABLE)
            exit_code = EXIT_COLLECTOR_UNAVAILABLE

        if collector is not None:
            platform_name = getattr(collector, "platform", platform_name)
            collector_name = getattr(collector, "name", "native")
            try:
                collector.start(stream)
                started = True
            except Exception as exc:
                _diagnostic(diagnostics, f"collector start failed: {exc}")
                writer.control("nativerelay.status", state="failed", platform=platform_name,
                               collector=collector_name, scope=scope,
                               error=str(exc), exit_code=EXIT_COLLECTOR_UNAVAILABLE)
                exit_code = EXIT_COLLECTOR_UNAVAILABLE

        if started:
            status = _collector_status(collector)
            state = _collection_state(collector)
            writer.control("nativerelay.status", state=state, platform=platform_name,
                           collector=collector_name, scope=scope,
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
                            writer.control("nativerelay.status", state="failed", platform=platform_name,
                                           collector=collector_name,
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
                        writer.control("nativerelay.status", state=current_state, platform=platform_name,
                                       collector=collector_name, scope=scope,
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
            writer.control("nativerelay.status", state="failed", platform=platform_name,
                           collector=getattr(collector, "name", "native"), scope=scope,
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
                writer.control("nativerelay.status", state=final_state, platform=platform_name,
                               collector=getattr(collector, "name", "native"), scope=scope,
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
    supported = [cap for cap in collector.capabilities if cap.status.value != "unsupported"]
    active = [cap for cap in supported if cap.status.value in ("available", "degraded")]
    if not active:
        return "unavailable"
    if len(active) != len(supported) or any(
        cap.status.value == "degraded" for cap in active
    ):
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
                       collector=getattr(collector, "name", "native"))
    return generation


def _report_diagnostics(stderr, state, status):
    if state != "running":
        _diagnostic(stderr, f"collection state: {state}")
    for error in status.get("errors", []):
        _diagnostic(stderr, str(error))


def _diagnostic(stderr, message):
    print(f"nativerelay: {message}", file=stderr, flush=True)


def _human(event):
    proc = "pid=unknown" if event.process.pid == 0 else f"pid={event.process.pid}"
    if event.process.parent_pid is not None: proc += f" ppid={event.process.parent_pid}"
    if event.process.executable: proc += f" exe={event.process.executable}"
    resource = f" {event.resource.path}" if event.resource else ""
    metadata = " " + json.dumps(event.metadata) if event.metadata else ""
    return f"{event.timestamp} {event.type.value} {proc}{resource}{metadata}"


def _platform_id():
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return platform.system().lower() or "unknown"


def _platform_collector():
    if LinuxCollector is not None:
        return LinuxCollector
    if sys.platform == "win32":
        from collectors.windows import WindowsCollector
        return WindowsCollector
    if sys.platform.startswith("linux"):
        from collectors.linux import LinuxCollector as linux_collector
        return linux_collector
    raise OSError(f"NativeRelay has no collector for {_platform_id()}")


if __name__ == "__main__":
    raise SystemExit(main())