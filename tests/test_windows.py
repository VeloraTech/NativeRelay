import os
import platform
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from threading import Lock

from collectors.windows.collector import (
    FILE_ACTION_ADDED,
    WindowsCollector,
    _PathFilter,
)
from nativerelay.model import CollectorStatus, EventType
from nativerelay.stream import EventStream


class WindowsCollectorUnitTests(unittest.TestCase):
    def test_path_filter_enforces_scope_and_patterns(self):
        with tempfile.TemporaryDirectory() as temp:
            scope = Path(temp).resolve()
            path_filter = _PathFilter(scope, ["*.py"], ["test_*"])
            self.assertTrue(path_filter.matches(scope / "module.py"))
            self.assertFalse(path_filter.matches(scope / "test_module.py"))
            self.assertFalse(path_filter.matches(scope.parent / "outside.py"))

    def test_file_notification_maps_to_canonical_event_and_unknown_actor(self):
        with tempfile.TemporaryDirectory() as temp:
            scope = Path(temp).resolve()
            collector = object.__new__(WindowsCollector)
            collector.scope = scope
            collector.name = "windows-native"
            collector.platform = "windows"
            collector._path_filter = _PathFilter(scope)
            collector._stream = EventStream(8)
            collector._lock = Lock()
            collector._sequence = 0
            collector.health = {"dropped": 0, "errors": []}
            name = "created.txt"
            encoded = name.encode("utf-16le")
            data = struct.pack("<III", 0, FILE_ACTION_ADDED, len(encoded)) + encoded

            collector._parse_notifications(data)
            event = collector._stream.receive(timeout=0)
            self.assertEqual(event.type, EventType.FILE_CREATED)
            self.assertEqual(event.process.pid, 0)
            self.assertEqual(event.resource.path, str(scope / name))
            self.assertEqual(event.sequence, 1)

    def test_process_trace_record_preserves_native_identity(self):
        collector = object.__new__(WindowsCollector)
        collector.name = "windows-native"
        collector.platform = "windows"
        collector._stream = EventStream(8)
        collector._lock = Lock()
        collector._sequence = 0
        collector.health = {"dropped": 0, "errors": []}

        collector._publish_process_record({
            "kind": "start",
            "pid": 42,
            "parent_pid": 7,
            "process_name": "demo.exe",
            "session_id": 3,
        })
        event = collector._stream.receive(timeout=0)
        self.assertEqual(event.type, EventType.PROCESS_STARTED)
        self.assertEqual(event.process.pid, 42)
        self.assertEqual(event.process.parent_pid, 7)
        self.assertEqual(event.process.executable, "demo.exe")
        self.assertEqual(event.metadata["sessionId"], 3)

    @unittest.skipUnless(os.name == "nt", "Windows command-line parsing requires Windows")
    def test_opt_in_command_line_is_parsed_into_arguments(self):
        parsed = WindowsCollector._split_command_line(
            '"C:\\Program Files\\demo.exe" --name "two words"'
        )
        self.assertEqual(parsed, (
            "C:\\Program Files\\demo.exe", "--name", "two words",
        ))

    def test_unsupported_open_capability_is_explicit(self):
        if platform.system() != "Windows":
            self.skipTest("Windows collector constructor requires Windows")
        with tempfile.TemporaryDirectory() as temp:
            collector = WindowsCollector(temp)
            opened = next(c for c in collector.capabilities if c.event_type == EventType.FILE_OPENED)
            self.assertEqual(opened.status, CollectorStatus.UNSUPPORTED)


@unittest.skipUnless(platform.system() == "Windows", "live collector test requires Windows 10/11")
class WindowsCollectorIntegrationTests(unittest.TestCase):
    def test_process_and_recursive_file_notifications(self):
        with tempfile.TemporaryDirectory(prefix="nativerelay-win-") as temp:
            scope = Path(temp)
            child_code = "import time; time.sleep(0.5)"
            collector = WindowsCollector(temp)
            stream = EventStream(capacity=4096)
            collector.start(stream)
            child = None
            try:
                status = collector.status
                self.assertEqual(status["filesystem"], "available", status)
                self.assertIn(status["process"], {
                    "available", CollectorStatus.PERMISSION_DENIED.value,
                    CollectorStatus.UNAVAILABLE.value, CollectorStatus.DEGRADED.value,
                }, status)
                process_available = status["process"] in {
                    CollectorStatus.AVAILABLE.value, CollectorStatus.DEGRADED.value,
                }

                nested = scope / "nested"
                nested.mkdir()
                target = nested / "created.txt"
                target.write_text("native relay", encoding="utf-8")
                target.write_text("updated", encoding="utf-8")
                renamed = nested / "renamed.txt"
                target.rename(renamed)
                renamed.unlink()

                child = subprocess.Popen([sys.executable, "-c", child_code])
                deadline = time.monotonic() + 15
                observed = set()
                while time.monotonic() < deadline:
                    try:
                        event = stream.receive(timeout=min(0.5, max(0, deadline - time.monotonic())))
                    except TimeoutError:
                        continue
                    if process_available and event.type == EventType.PROCESS_STARTED and event.process.pid == child.pid:
                        observed.add(EventType.PROCESS_STARTED)
                    if process_available and event.type == EventType.PROCESS_EXITED and event.process.pid == child.pid:
                        observed.add(EventType.PROCESS_EXITED)
                    if event.type == EventType.FILE_CREATED and event.resource.path.endswith("created.txt"):
                        observed.add(EventType.FILE_CREATED)
                    if event.type == EventType.FILE_MODIFIED and event.resource.path.endswith("created.txt"):
                        observed.add(EventType.FILE_MODIFIED)
                    if event.type == EventType.FILE_RENAMED:
                        observed.add(EventType.FILE_RENAMED)
                    if event.type == EventType.FILE_DELETED and event.resource.path.endswith("renamed.txt"):
                        observed.add(EventType.FILE_DELETED)
                    file_events = {
                        EventType.FILE_CREATED, EventType.FILE_MODIFIED,
                        EventType.FILE_RENAMED, EventType.FILE_DELETED,
                    }
                    expected = file_events | (
                        {EventType.PROCESS_STARTED, EventType.PROCESS_EXITED}
                        if process_available else set()
                    )
                    if expected.issubset(observed):
                        break
                child.wait(timeout=3)
                expected = {
                    EventType.FILE_CREATED, EventType.FILE_MODIFIED,
                    EventType.FILE_RENAMED, EventType.FILE_DELETED,
                }
                if process_available:
                    expected.update({EventType.PROCESS_STARTED, EventType.PROCESS_EXITED})
                self.assertTrue(
                    expected.issubset(observed),
                    f"missing {expected - observed}; collector status: {collector.status}",
                )
            finally:
                if child is not None and child.poll() is None:
                    child.terminate()
                    child.wait(timeout=3)
                collector.stop()


if __name__ == "__main__":
    unittest.main()
