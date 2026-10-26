import io
import json
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from nativerelay.cli import EXIT_COLLECTOR_UNAVAILABLE, EXIT_OK, EXIT_RUNTIME_FAILURE, main
from nativerelay.model import Capability, CollectorStatus, Event, EventType, Process, Resource
from nativerelay.stream import EventStream


class FakeCollector:
    name = "fake-linux"

    def __init__(self, scope, *, include_command=False, mode="event",
                 include_paths=None, exclude_paths=None):
        self.scope = scope
        self.mode = mode
        self.stream = None
        self.stopped = False
        self._capabilities = tuple(
            Capability(event_type, CollectorStatus.AVAILABLE, "test source", scope, "test PID")
            for event_type in (EventType.PROCESS_STARTED, EventType.PROCESS_EXITED,
                               EventType.FILE_OPENED, EventType.FILE_MODIFIED)
        )
        self._event = Event(EventType.FILE_MODIFIED, "linux", self.name, Process(42, 7, "/bin/demo", ("demo",)),
                            Resource("file", "/tmp/example.txt"),
                            timestamp="2026-10-07T10:00:00+00:00", id="event-42", sequence=15)

    @property
    def capabilities(self):
        return self._capabilities

    @property
    def status(self):
        if self.stream is None:
            losses, generation, dropped = {}, 0, 0
        else:
            losses, generation, dropped = self.stream.losses, self.stream.loss_generation, self.stream.dropped
        return {"process": "available", "filesystem": "available", "dropped": dropped,
                "errors": [], "losses": losses, "loss_generation": generation}

    def start(self, stream):
        self.stream = stream
        if self.mode == "event":
            stream.publish(self._event)
        elif self.mode == "loss":
            stream.report_loss(self.name, "fanotify_queue_overflow", count=None, sequence=16)
        elif self.mode == "degraded":
            self._capabilities = tuple(
                Capability(c.event_type, CollectorStatus.PERMISSION_DENIED if c.event_type == EventType.FILE_OPENED else c.status,
                           "test permission denial" if c.event_type == EventType.FILE_OPENED else c.reason,
                           c.scope, c.attribution) for c in self._capabilities
            )
        elif self.mode == "stream_error":
            stream.close(OSError("test stream failure"))
        stream.close()

    def stop(self):
        self.stopped = True
        if self.stream is not None:
            self.stream.close()


class CliTests(unittest.TestCase):
    def run_fake(self, mode="event"):
        stdout, stderr = io.StringIO(), io.StringIO()
        made = []

        def factory(scope, *, include_command=False, include_paths=None, exclude_paths=None):
            collector = FakeCollector(scope, include_command=include_command, mode=mode,
                                      include_paths=include_paths, exclude_paths=exclude_paths)
            made.append(collector)
            return collector

        with patch("nativerelay.cli.LinuxCollector", factory):
            code = main_with_streams(["run", "--format", "json", "--scope", "/tmp/native-relay-test"], stdout, stderr)
        return code, stdout, stderr, made[0]

    def test_console_script_is_declared_for_installed_package(self):
        project = tomllib.loads(Path("pyproject.toml").read_text())
        self.assertEqual(project["project"]["scripts"]["nativerelay"], "nativerelay.cli:main")

    def test_jsonl_preserves_event_fields_and_keeps_diagnostics_on_stderr(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        fake = FakeCollector("/tmp/native-relay-test")
        with patch("nativerelay.cli.LinuxCollector", return_value=fake):
            code = main_with_streams(["run", "--format", "json", "--scope", fake.scope], stdout, stderr)
        records = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(all(line.endswith("\n") for line in stdout.getvalue().splitlines(keepends=True)))
        event = next(record for record in records if record.get("id") == "event-42")
        self.assertEqual(event, json.loads(fake._event.to_json()))
        self.assertEqual(event["sequence"], 15)
        self.assertEqual(records[0]["record_type"], "nativerelay.status")
        self.assertEqual(records[-1]["record_type"], "nativerelay.status")
        self.assertEqual(records[-1]["state"], "stopped")
        self.assertTrue(fake.stopped)
        self.assertIn("collection stopped", stderr.getvalue())
        self.assertNotIn("nativerelay:", stdout.getvalue())

    def test_loss_control_record_preserves_ledger_and_generation(self):
        code, stdout, _, _ = self.run_fake("loss")
        records = [json.loads(line) for line in stdout.getvalue().splitlines()]
        loss = next(record for record in records if record.get("record_type") == "nativerelay.loss")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(loss["schema_version"], 1)
        self.assertEqual(loss["loss_generation"], 1)
        details = loss["losses"]["fake-linux"]["fanotify_queue_overflow"]
        self.assertTrue(details["unknown_count"])
        self.assertEqual(details["last_sequence"], 16)

    def test_degraded_status_is_json_and_human_diagnostic_is_stderr(self):
        code, stdout, stderr, _ = self.run_fake("degraded")
        records = [json.loads(line) for line in stdout.getvalue().splitlines()]
        started = records[0]
        file_open = next(c for c in started["capabilities"] if c["event_type"] == "file.opened")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(started["state"], "degraded")
        self.assertEqual(file_open["status"], "permission_denied")
        self.assertIn("collection state: degraded", stderr.getvalue())
        self.assertTrue(all(isinstance(record, dict) for record in records))

    def test_collector_initialization_failure_has_json_status_and_stderr_error(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("nativerelay.cli.LinuxCollector", side_effect=OSError("Linux only")):
            code = main_with_streams(["run", "--format", "json"], stdout, stderr)
        record = json.loads(stdout.getvalue())
        self.assertEqual(code, EXIT_COLLECTOR_UNAVAILABLE)
        self.assertEqual(record["record_type"], "nativerelay.status")
        self.assertEqual(record["state"], "failed")
        self.assertIn("Linux only", stderr.getvalue())

    def test_stream_error_is_fatal_but_keeps_stdout_valid_jsonl(self):
        code, stdout, stderr, _ = self.run_fake("stream_error")
        records = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(code, EXIT_RUNTIME_FAILURE)
        self.assertTrue(any(r.get("state") == "failed" for r in records))
        self.assertIn("test stream failure", stderr.getvalue())

    def test_invalid_format_is_rejected(self):
        stderr = io.StringIO()
        with patch("sys.stderr", stderr), self.assertRaises(SystemExit) as raised:
            main(["run", "--format", "yaml"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("invalid choice", stderr.getvalue())


def main_with_streams(args, stdout, stderr):
    from nativerelay import cli
    with patch.object(cli.sys, "stdout", stdout), patch.object(cli.sys, "stderr", stderr):
        return cli.main(args)


if __name__ == "__main__":
    unittest.main()