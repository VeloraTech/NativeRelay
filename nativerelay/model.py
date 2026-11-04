"""Version 1 normalized event and capability model.

Timestamps are timezone-aware ISO-8601 instants. ``sequence`` is optional and, when a
collector supplies it, orders events as normalized by that collector only.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import uuid
from typing import Any

class EventType(str, Enum):
    PROCESS_STARTED = "process.started"
    PROCESS_EXITED = "process.exited"
    FILE_OPENED = "file.opened"
    FILE_CREATED = "file.created"
    FILE_MODIFIED = "file.modified"
    FILE_DELETED = "file.deleted"
    FILE_RENAMED = "file.renamed"

class CollectorStatus(str, Enum):
    SUPPORTED = "supported"
    AVAILABLE = "available"
    DEGRADED = "degraded"
    PERMISSION_DENIED = "permission_denied"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"

@dataclass(frozen=True)
class Process:
    """Process identity; PID 0 means the native source did not identify an actor."""
    pid: int
    parent_pid: int | None = None
    executable: str | None = None
    command: tuple[str, ...] | None = None
    def __post_init__(self):
        if not isinstance(self.pid, int) or self.pid < 0 or (self.parent_pid is not None and (not isinstance(self.parent_pid, int) or self.parent_pid < 0)):
            raise ValueError("process IDs must be non-negative (zero means unknown)")
        if self.executable is not None and not isinstance(self.executable, str): raise ValueError("executable must be a string")
        if self.command is not None and not all(isinstance(x, str) for x in self.command):
            raise ValueError("command must contain strings")

@dataclass(frozen=True)
class Resource:
    type: str
    path: str
    def __post_init__(self):
        if not isinstance(self.type, str) or not isinstance(self.path, str) or not self.type or not self.path:
            raise ValueError("resource type and path are required")

@dataclass(frozen=True)
class Event:
    type: EventType
    platform: str
    collector: str
    process: Process
    resource: Resource | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)
    evidence: str = "observed"
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    sequence: int | None = None
    def __post_init__(self):
        if not isinstance(self.type, EventType): object.__setattr__(self, "type", EventType(self.type))
        if not isinstance(self.process, Process): raise ValueError("process must be a Process")
        if self.resource is not None and not isinstance(self.resource, Resource): raise ValueError("resource must be a Resource")
        if not all(isinstance(value, str) and value for value in (self.platform, self.collector, self.id)):
            raise ValueError("platform, collector, and id are required")
        if self.evidence not in ("observed", "derived"): raise ValueError("evidence must be observed or derived")
        try:
            parsed_timestamp = datetime.fromisoformat(self.timestamp.replace("Z", "+00:00"))
            if parsed_timestamp.utcoffset() is None: raise ValueError("timezone required")
        except (ValueError, AttributeError) as exc: raise ValueError("timestamp must be ISO-8601 with timezone") from exc
        if self.sequence is not None and (not isinstance(self.sequence, int) or self.sequence < 0):
            raise ValueError("sequence must be a non-negative integer or None")
        if not isinstance(self.metadata, dict): raise ValueError("metadata must be an object")
        try: json.dumps(self.metadata)
        except (TypeError, ValueError) as exc: raise ValueError("metadata must be JSON serializable") from exc
    @staticmethod
    def stable_id(collector: str, boot_id: str, sequence: int) -> str:
        raw = f"{collector}\0{boot_id}\0{sequence}".encode()
        return hashlib.sha256(raw).hexdigest()
    def to_dict(self):
        return {"id": self.id, "timestamp": self.timestamp, "type": self.type.value,
                "platform": self.platform, "collector": self.collector, "process": asdict(self.process),
                "resource": asdict(self.resource) if self.resource else None,
                "metadata": self.metadata, "evidence": self.evidence, "sequence": self.sequence, "schema_version": 1}
    def to_json(self): return json.dumps(self.to_dict(), separators=(",", ":"), ensure_ascii=True)

@dataclass(frozen=True)
class Capability:
    event_type: EventType
    status: CollectorStatus
    reason: str
    scope: str
    attribution: str
    def __post_init__(self):
        if not isinstance(self.event_type, EventType): object.__setattr__(self, "event_type", EventType(self.event_type))
        if not isinstance(self.status, CollectorStatus): object.__setattr__(self, "status", CollectorStatus(self.status))
        if not self.reason or not self.scope or not self.attribution: raise ValueError("capability reason, scope, and attribution are required")

def decode_event(data: str) -> Event:
    try:
        raw = json.loads(data)
        return Event(type=EventType(raw["type"]), platform=raw["platform"], collector=raw["collector"],
                     process=Process(**{**raw["process"], "command": tuple(raw["process"]["command"]) if raw["process"].get("command") is not None else None}),
                     resource=Resource(**raw["resource"]) if raw.get("resource") else None,
                     timestamp=raw["timestamp"], metadata=raw.get("metadata", {}), evidence=raw["evidence"], id=raw["id"],
                     sequence=raw.get("sequence"))
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid NativeRelay event: {exc}") from exc
