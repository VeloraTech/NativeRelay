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
                target = Path(temp) / "controlled.txt"
                with target.open("w", encoding="utf-8") as handle:
                    handle.write("controlled test metadata only\n")
                seen = set()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not {EventType.FILE_OPENED, EventType.FILE_MODIFIED}.issubset(seen):
                    try: event = stream.receive(timeout=max(0, deadline - time.monotonic()))
                    except TimeoutError: break
                    if event.resource and Path(event.resource.path) == target and event.process.pid == os.getpid():
                        seen.add(event.type)
                self.assertIn(EventType.FILE_OPENED, seen)
                self.assertIn(EventType.FILE_MODIFIED, seen)
            finally:
                collector.stop()


if __name__ == "__main__": unittest.main()
