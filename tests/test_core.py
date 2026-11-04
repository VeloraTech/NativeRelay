import json
import unittest
from nativerelay.model import Event, EventType, Process, Resource, decode_event
from nativerelay.stream import EventStream, StreamClosed
from nativerelay.collector import Collector
from nativerelay.model import Capability, CollectorStatus

class CoreTests(unittest.TestCase):
    def make_event(self):
        return Event(EventType.FILE_OPENED, "linux", "test", Process(10, 1, "/bin/demo", ("demo",)),
                     Resource("file", "/tmp/a"), timestamp="2026-01-01T00:00:00+00:00", id="stable", sequence=7)
    def test_round_trip_stable_serialization(self):
        event = self.make_event()
        self.assertEqual(decode_event(event.to_json()), event)
        self.assertEqual(event.to_json(), event.to_json())
        self.assertEqual(event.sequence, 7)
        self.assertEqual(json.loads(event.to_json())["schema_version"], 1)
    def test_stable_id(self):
        self.assertEqual(Event.stable_id("c", "b", 2), Event.stable_id("c", "b", 2))
        self.assertNotEqual(Event.stable_id("c", "b", 2), Event.stable_id("c", "b", 3))
    def test_validation_and_malformed_input(self):
        self.assertEqual(Process(0).pid, 0)
        with self.assertRaises(ValueError): Process(-1)
        with self.assertRaises(ValueError): Event(EventType.PROCESS_STARTED, "linux", "test", Process(1), timestamp="2026-01-01T00:00:00")
        with self.assertRaises(ValueError): Event(EventType.PROCESS_STARTED, "linux", "test", "not-a-process")
        with self.assertRaises(ValueError): Event(EventType.PROCESS_STARTED, "linux", "test", Process(1), metadata={"bad": object()})
        with self.assertRaises(ValueError): decode_event("{}")
        obj = json.loads(self.make_event().to_json()); obj["type"] = "process.telepathy"
        with self.assertRaises(ValueError): decode_event(json.dumps(obj))
    def test_bounded_stream_and_timeout(self):
        stream = EventStream(1); event = self.make_event()
        self.assertTrue(stream.publish(event)); self.assertFalse(stream.publish(event))
        self.assertEqual(stream.dropped, 1)
        self.assertEqual(stream.loss_generation, 1)
        overflow = stream.losses["test"]["event_stream_full"]
        self.assertEqual(overflow["known_dropped"], 1)
        self.assertEqual(overflow["last_sequence"], 7)
        self.assertEqual(stream.receive(), event)
        with self.assertRaises(TimeoutError): stream.receive(timeout=0)
        stream.close()
        with self.assertRaises(StreamClosed): stream.receive()
        self.assertFalse(stream.publish(event))
        self.assertEqual(stream.losses["test"]["event_stream_closed"]["known_dropped"], 1)
        buffered = EventStream(1)
        self.assertTrue(buffered.publish(event))
        buffered.close()
        self.assertEqual(buffered.receive(), event)
        with self.assertRaises(StreamClosed): buffered.receive()
    def test_capability_and_collector_lifecycle(self):
        capability = Capability(EventType.PROCESS_STARTED, CollectorStatus.AVAILABLE, "test", "host", "kernel")
        self.assertEqual(capability.status, CollectorStatus.AVAILABLE)
        self.assertEqual(CollectorStatus.SUPPORTED.value, "supported")
        class FakeCollector(Collector):
            running = False
            @property
            def capabilities(self): return (capability,)
            def start(self, stream): self.running = True
            def stop(self): self.running = False
        collector = FakeCollector(); collector.start(EventStream())
        self.assertTrue(collector.running); collector.stop(); self.assertFalse(collector.running)

if __name__ == "__main__": unittest.main()
