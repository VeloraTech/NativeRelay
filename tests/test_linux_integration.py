"""Opt-in live Linux tests for CN_PROC and fanotify.

Run on a Linux host with NATIVERELAY_LINUX_INTEGRATION=1. The fanotify test
requires CAP_SYS_ADMIN; the process test generally requires CAP_NET_ADMIN.
"""
import os
import platform
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from collectors.linux import LinuxCollector
from nativerelay.model import CollectorStatus, EventType
from nativerelay.stream import EventStream

ENABLED = platform.system() == "Linux" and os.environ.get("NATIVERELAY_LINUX_INTEGRATION") == "1"
REQUIRED = os.environ.get("NATIVERELAY_LINUX_INTEGRATION_REQUIRED") == "1"


def require_capability(testcase, capability, name):
    if capability.status == CollectorStatus.AVAILABLE:
        return
    message = f"{name} unavailable: {capability.reason}"
    if REQUIRED:
        testcase.fail(message)
    testcase.skipTest(message)


@unittest.skipUnless(ENABLED, "set NATIVERELAY_LINUX_INTEGRATION=1 on Linux to run kernel integration tests")
class LinuxCollectorIntegrationTests(unittest.TestCase):
    def test_controlled_child_process_lifecycle_and_parent(self):
        with tempfile.TemporaryDirectory(prefix="nativerelay-proc-") as temp:
            collector = LinuxCollector(temp)
            stream = EventStream(capacity=4096)
            collector.start(stream)
            try:
                process_cap = next(c for c in collector.capabilities if c.event_type == EventType.PROCESS_STARTED)
                require_capability(self, process_cap, "CN_PROC")
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.2)"])
                seen = {}
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not {EventType.PROCESS_STARTED, EventType.PROCESS_EXITED}.issubset(seen):
                    try: event = stream.receive(timeout=max(0, deadline - time.monotonic()))
                    except TimeoutError: break
                    if event.process.pid == child.pid and event.type in (EventType.PROCESS_STARTED, EventType.PROCESS_EXITED):
                        seen[event.type] = event
                child.wait(timeout=2)
                self.assertIn(EventType.PROCESS_STARTED, seen)
                self.assertIn(EventType.PROCESS_EXITED, seen)
                self.assertEqual(seen[EventType.PROCESS_STARTED].process.parent_pid, os.getpid())
                self.assertTrue(seen[EventType.PROCESS_EXITED].metadata["leaderThreadExit"])
            finally:
                collector.stop()

    def test_scoped_fanotify_open_and_modify_attribution(self):
        with tempfile.TemporaryDirectory(prefix="nativerelay-fan-") as temp:
            collector = LinuxCollector(temp)
            stream = EventStream(capacity=4096)
            collector.start(stream)
            try:
                file_cap = next(c for c in collector.capabilities if c.event_type == EventType.FILE_OPENED)
                require_capability(self, file_cap, "fanotify")
                # Debug: print fanotify mode and health
                import sys
                print(f"fanotify_mode: {collector.fanotify_mode}", file=sys.stderr)
                print(f"health: {collector.status}", file=sys.stderr)
                
                mode = collector.fanotify_mode
                target = Path(temp) / "controlled.txt"
                
                # In dir-only mode (FAN_CLASS_NOTIF), only OPEN and MODIFY are supported
                # Creating/modifying files in the dir triggers OPEN/MODIFY on the directory
                if mode == "dir-only":
                    watch_path = Path(temp)
                    expected_types = {EventType.FILE_OPENED, EventType.FILE_MODIFIED}
                else:
                    watch_path = target
                    expected_types = {EventType.FILE_OPENED, EventType.FILE_MODIFIED}
                
                # Write file (triggers MODIFY on dir in dir-only mode)
                with target.open("w", encoding="utf-8") as handle:
                    handle.write("controlled test metadata only\n")
                # Open the directory itself (triggers OPEN on dir in dir-only mode)
                if mode == "dir-only":
                    # Use os.open with O_DIRECTORY to reliably trigger fanotify OPEN
                    dir_fd = os.open(temp, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.listdir(dir_fd)  # read directory via fd
                    finally:
                        os.close(dir_fd)
                else:
                    # Read file (triggers OPEN on file in other modes)
                    with target.open("r", encoding="utf-8") as handle:
                        handle.read()
                
                seen = set()
                all_events = []
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not expected_types.issubset(seen):
                    try: event = stream.receive(timeout=max(0, deadline - time.monotonic()))
                    except TimeoutError: break
                    if event.resource:
                        all_events.append((event.type, event.resource.path, event.process.pid))
                        print(f"DEBUG: event={event.type} path={event.resource.path} pid={event.process.pid} watch_path={watch_path} current_pid={os.getpid()}", file=sys.stderr)
                    if event.resource and Path(event.resource.path) == watch_path and event.process.pid == os.getpid():
                        seen.add(event.type)
                if not expected_types.issubset(seen):
                    print(f"DEBUG: All received events: {all_events}", file=sys.stderr)
                
                for et in expected_types:
                    self.assertIn(et, seen, f"Missing {et} for {watch_path} in mode {mode}")
            finally:
                collector.stop()


if __name__ == "__main__": unittest.main()
