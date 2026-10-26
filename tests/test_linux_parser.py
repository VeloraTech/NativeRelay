import struct
import unittest
import errno
import threading
from nativerelay.model import Event, Process
from nativerelay.stream import EventStream
from pathlib import Path
from collectors.linux.collector import LinuxCollector, CN_IDX_PROC, CN_VAL_PROC, PROC_EVENT_FORK, FAN_EVENT_METADATA_FMT, FAN_Q_OVERFLOW, NLMSG_OVERRUN
from nativerelay.model import EventType, CollectorStatus

class LinuxParserTests(unittest.TestCase):
    def test_malformed_packet_rejected_without_crashing_collector(self):
        collector = LinuxCollector.__new__(LinuxCollector)
        with self.assertRaises(ValueError): collector._parse_proc_packet(struct.pack("=IHHII", 100, 0, 0, 0, 0))
    def test_fork_packet_preserves_parent_child_ids(self):
        collector = LinuxCollector.__new__(LinuxCollector)
        seen = []
        collector._emit_process = lambda typ, pid, ppid, metadata=None: seen.append((typ.value, pid, ppid))
        proc_event = struct.pack("=IIQ", PROC_EVENT_FORK, 0, 123456) + struct.pack("=IIII", 101, 101, 202, 202)
        cn = struct.pack("=IIIIHH", CN_IDX_PROC, CN_VAL_PROC, 7, 0, len(proc_event), 0) + proc_event
        nl = struct.pack("=IHHII", 16 + len(cn), 0x10, 0, 9, 0)
        collector._parse_proc_packet(nl + cn)
        self.assertEqual(seen, [("process.started", 202, 101)])
    def test_thread_clone_is_not_reported_as_new_process(self):
        collector = LinuxCollector.__new__(LinuxCollector)
        seen = []
        collector._emit_process = lambda *args, **kwargs: seen.append(args)
        proc_event = struct.pack("=IIQ", PROC_EVENT_FORK, 0, 123456) + struct.pack("=IIII", 101, 101, 303, 202)
        cn = struct.pack("=IIIIHH", CN_IDX_PROC, CN_VAL_PROC, 7, 0, len(proc_event), 0) + proc_event
        nl = struct.pack("=IHHII", 16 + len(cn), 0x10, 0, 9, 0)
        collector._parse_proc_packet(nl + cn)
        self.assertEqual(seen, [])
    def test_unsupported_and_permission_denied_capabilities(self):
        collector = LinuxCollector.__new__(LinuxCollector)
        collector.scope = Path("/controlled/scope")
        collector._caps = collector._initial_capabilities()
        collector.health = {"errors": []}
        self.assertEqual(next(c for c in collector.capabilities if c.event_type == EventType.FILE_CREATED).status, CollectorStatus.UNSUPPORTED)
    def test_fanotify_overflow_packet_is_reported_with_sequence(self):
        collector = LinuxCollector.__new__(LinuxCollector)
        collector._publish_lock = threading.Lock()
        collector._sequence = 9
        collector._stream = EventStream(4)
        collector.name = "linux-kernel"
        collector.health = {"errors": []}
        collector._parse_fanotify_data(struct.pack(FAN_EVENT_METADATA_FMT, 24, 3, 0, 24, FAN_Q_OVERFLOW, -1, 0))
        report = collector._stream.losses["linux-kernel"]["fanotify_queue_overflow"]
        self.assertTrue(report["unknown_count"])
        self.assertEqual(report["last_sequence"], 9)
        self.assertEqual(collector._sequence, 10)
        self.assertEqual(collector.status["loss_generation"], 1)
        self.assertEqual(collector.status["losses"]["linux-kernel"]["fanotify_queue_overflow"]["unknown_count"], True)
    def test_cn_proc_netlink_overrun_is_reported_as_native_loss(self):
        collector = LinuxCollector.__new__(LinuxCollector)
        collector._publish_lock = threading.Lock()
        collector._sequence = 12
        collector._stream = EventStream(4)
        collector.name = "linux-kernel"
        collector._parse_proc_packet(struct.pack("=IHHII", 16, NLMSG_OVERRUN, 0, 1, 0))
        report = collector._stream.losses["linux-kernel"]["cn_proc_netlink_overrun"]
        self.assertTrue(report["unknown_count"])
        self.assertEqual(report["last_sequence"], 12)
        self.assertEqual(collector._sequence, 13)
    def test_native_loss_reserves_sequence_and_reports_unknown_count(self):
        collector = LinuxCollector.__new__(LinuxCollector)
        collector._publish_lock = threading.Lock()
        collector._sequence = 4
        collector._stream = EventStream(1)
        collector.name = "linux-kernel"
        collector.health = {"dropped": 0, "native_losses": {}, "errors": []}
        collector._record_native_loss("fanotify_queue_overflow", count=None)
        report = collector._stream.losses["linux-kernel"]["fanotify_queue_overflow"]
        self.assertEqual(report["occurrences"], 1)
        self.assertEqual(report["known_dropped"], 0)
        self.assertTrue(report["unknown_count"])
        self.assertEqual(report["last_sequence"], 4)
        self.assertEqual(collector._sequence, 5)

    def test_linux_stream_drop_creates_sequence_gap_and_loss_report(self):
        collector = LinuxCollector.__new__(LinuxCollector)
        collector._publish_lock = threading.Lock()
        collector._sequence = 0
        collector._stream = EventStream(1)
        collector.name = "linux-kernel"
        collector.health = {"dropped": 0, "native_losses": {}, "errors": []}
        first = Event(EventType.FILE_OPENED, "linux", collector.name, Process(10))
        second = Event(EventType.FILE_OPENED, "linux", collector.name, Process(10))
        collector._publish(first)
        collector._publish(second)
        self.assertEqual(collector._stream.dropped, 1)
        queued = collector._stream.receive()
        self.assertEqual(queued.sequence, 0)
        third = Event(EventType.FILE_OPENED, "linux", collector.name, Process(10))
        collector._publish(third)
        self.assertEqual(collector._stream.receive().sequence, 2)
        report = collector._stream.losses["linux-kernel"]["event_stream_full"]
        self.assertEqual(report["known_dropped"], 1)
        self.assertEqual(report["last_sequence"], 1)
    def test_connector_ack_controls_capability_state(self):
        collector = LinuxCollector.__new__(LinuxCollector)
        collector.scope = Path("/controlled/scope")
        collector._caps = collector._initial_capabilities()
        collector.health = {"process": "starting", "errors": []}
        body = struct.pack("=IIQi", 0, 0, 1, 0)
        cn = struct.pack("=IIIIHH", CN_IDX_PROC, CN_VAL_PROC, 1, 0, len(body), 0) + body
        nl = struct.pack("=IHHII", 16 + len(cn), 3, 0, 1, 0)
        collector._parse_proc_packet(nl + cn)
        self.assertEqual(collector.health["process"], "available")
        self.assertEqual(next(c for c in collector.capabilities if c.event_type == EventType.PROCESS_STARTED).status, CollectorStatus.AVAILABLE)
        body = struct.pack("=IIQi", 0, 0, 2, errno.EPERM)
        cn = struct.pack("=IIIIHH", CN_IDX_PROC, CN_VAL_PROC, 2, 0, len(body), 0) + body
        nl = struct.pack("=IHHII", 16 + len(cn), 3, 0, 2, 0)
        collector._parse_proc_packet(nl + cn)
        self.assertEqual(next(c for c in collector.capabilities if c.event_type == EventType.PROCESS_STARTED).status, CollectorStatus.PERMISSION_DENIED)
        collector._failure("filesystem", PermissionError("denied"))
        self.assertEqual(next(c for c in collector.capabilities if c.event_type == EventType.FILE_OPENED).status, CollectorStatus.PERMISSION_DENIED)
        self.assertEqual(next(c for c in collector.capabilities if c.event_type == EventType.FILE_CREATED).status, CollectorStatus.UNSUPPORTED)

    def test_path_filter_matches_scope(self):
        from collectors.linux.collector import _PathFilter
        scope = Path("/workspace")
        filter = _PathFilter(scope, None, None)
        self.assertTrue(filter.matches(Path("/workspace/file.txt")))
        self.assertTrue(filter.matches(Path("/workspace/src/main.py")))
        self.assertFalse(filter.matches(Path("/other/file.txt")))

    def test_path_filter_include_patterns(self):
        from collectors.linux.collector import _PathFilter
        scope = Path("/workspace")
        filter = _PathFilter(scope, include_patterns=["*.py"], exclude_patterns=None)
        self.assertTrue(filter.matches(Path("/workspace/main.py")))
        self.assertTrue(filter.matches(Path("/workspace/src/main.py")))
        self.assertFalse(filter.matches(Path("/workspace/file.txt")))
        self.assertFalse(filter.matches(Path("/workspace/data.json")))

    def test_path_filter_exclude_patterns(self):
        from collectors.linux.collector import _PathFilter
        scope = Path("/workspace")
        filter = _PathFilter(scope, include_patterns=None, exclude_patterns=["*.tmp", "*.log"])
        self.assertTrue(filter.matches(Path("/workspace/main.py")))
        self.assertTrue(filter.matches(Path("/workspace/src/main.py")))
        self.assertFalse(filter.matches(Path("/workspace/temp.tmp")))
        self.assertFalse(filter.matches(Path("/workspace/debug.log")))

    def test_path_filter_include_and_exclude(self):
        from collectors.linux.collector import _PathFilter
        scope = Path("/workspace")
        filter = _PathFilter(scope, include_patterns=["*.py", "*.txt"], exclude_patterns=["test_*"])
        self.assertTrue(filter.matches(Path("/workspace/main.py")))
        self.assertTrue(filter.matches(Path("/workspace/readme.txt")))
        self.assertFalse(filter.matches(Path("/workspace/test_main.py")))
        self.assertFalse(filter.matches(Path("/workspace/data.json")))

    def test_collector_initializes_path_filter(self):
        from collectors.linux.collector import _PathFilter
        collector = LinuxCollector.__new__(LinuxCollector)
        collector.scope = Path("/test/scope")
        collector.include_command = False
        collector._path_filter = _PathFilter(collector.scope, ["*.py"], ["test_*"])
        self.assertTrue(collector._path_filter.matches(Path("/test/scope/main.py")))
        self.assertFalse(collector._path_filter.matches(Path("/test/scope/test_main.py")))

    def test_path_filter_outside_scope(self):
        from collectors.linux.collector import _PathFilter
        scope = Path("/workspace")
        filter = _PathFilter(scope, None, None)
        # Paths clearly outside scope
        self.assertFalse(filter.matches(Path("/other/file.txt")))
        self.assertFalse(filter.matches(Path("/etc/passwd")))


if __name__ == "__main__": unittest.main()
