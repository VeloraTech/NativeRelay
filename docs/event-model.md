# Event model

`nativerelay.model` is the platform-neutral source of truth for the normalized contract. JSON is its serialization, with `schema_version: 1`; it is used for inspection and exchange rather than as a separate architecture.

## Event families

| Event | Meaning |
|---|---|
| `process.started` | The native source reported a process start. |
| `process.exited` | The native source reported a process exit. It does not imply every thread independently exited. |
| `file.opened` | The source reported an open operation; this does not prove bytes were read. Unsupported by the Windows collector. |
| `file.created` | The source reported creation. The Linux collector implements this in compatible fanotify modes; availability depends on the runtime mode and system support. |
| `file.modified` | The source reported a modification notification; changed bytes and durable storage are unknown. |
| `file.deleted` | The source reported deletion. The Linux collector implements this in compatible fanotify modes; availability depends on the runtime mode and system support. |
| `file.renamed` | The source reported a move/rename. Linux emits separate source/destination notifications with direction metadata; consumers must correlate them. |

Collectors emit only observations their native source supports. The supported Linux collector emits `observed` evidence; derived observations must be labelled `derived` and must not be presented as native-source facts.

## Shape and identity

Each `Event` contains `id`, `timestamp`, `type`, `platform`, `collector`, `process`, optional `resource`, `metadata`, `evidence`, optional `sequence`, and schema version in its JSON representation. Process fields are PID, optional parent PID, optional executable, and optional command/argv. PID `0` means the native source did not identify an actor; Windows directory-change file events use this sentinel rather than attributing activity to the collector process. Command is absent (`null`) unless explicitly opted in by the collector and available from the source. A resource contains a type and path; no file content is read.

IDs are random UUIDs by default and remain unchanged after emission. `Event.stable_id(collector, boot_id, sequence)` is available when a source identity and durable sequence exist. Linux currently uses UUIDs. PID is an operating-system identifier that can be reused; it is not a durable process identity. A consumer needing correlation across reuse should use lifecycle boundaries and its own higher-level identity.

Timestamps are timezone-aware ISO-8601 instants. Linux uses userspace receipt/normalization time; Windows timestamps WMI process events when normalized and directory events when received. Collectors assign an optional monotonically increasing sequence in normalized publication order, shared across collector threads for one run. This does not claim kernel causality or ordering between different collectors. Sequence gaps can signal events that were dropped from the bounded stream.

## Capabilities and health

A `Capability` is per event type and includes `status`, `reason`, `scope`, and attribution source. Status values are `supported` (implemented but not started), `available` (initialized and currently able to observe), `degraded`, `permission_denied`, `unavailable`, and `unsupported`. `available` is not a guarantee of complete coverage. Platform process and filesystem sources initialize independently.

A timeout from `EventStream.receive(timeout=...)` means no event arrived before the deadline; it does not mean no operating-system activity occurred. Consumers should use finite receive timeouts and inspect `loss_generation`/`losses` so native loss notifications with no following event are still noticed. `EventStream.dropped` counts exact events rejected because the in-memory queue was full. The `losses` snapshot groups reports by source and reason, with `occurrences`, exact `known_dropped`, `unknown_count`, and `last_sequence`; `loss_generation` advances for each new report. Event-stream drops are reported automatically. Linux also reports fanotify overflow (loss count unknown), malformed/truncated native records, and file-event path resolution failures (known operation count). Linux `status["dropped"]` counts only stream-full event rejections; inspect `status["losses"]` for all detected loss. A surfaced CN_PROC netlink overrun is reported, but kernel-internal or otherwise unreported loss cannot be quantified; process observations remain best effort even when available. Detected loss is not recovered or replayed. `EventStream.close()` stops accepting events, drains buffered events, and then `receive()` raises `StreamClosed`; its optional `error` carries a producer failure. Linux collector shutdown closes its stream.

## Example JSON

```json
{"schema_version":1,"id":"b2d8d00f-96a1-4ce6-9912-6bf3ea913002","sequence":18,"timestamp":"2026-10-07T12:00:00+00:00","type":"file.opened","platform":"linux","collector":"linux-kernel","process":{"pid":4218,"parent_pid":4000,"executable":"/usr/bin/editor","command":null},"resource":{"type":"file","path":"/workspace/src/main.py"},"metadata":{},"evidence":"observed"}
```

Consumers should branch on known event types and schema version, query capabilities rather than infer support from silence, and inspect stream/collector health for gaps. NativeRelay does not identify applications, AI agents, sessions, or runs.


## Machine CLI JSONL

`nativerelay run --format json` writes event objects and control objects as compact JSON Lines to stdout. Event records are exactly the canonical `Event.to_json()` shape above. Control records have `schema_version: 1`, a timestamp, and `record_type`:

- `nativerelay.status` reports current collection state and capabilities, then a final `stopped` or `failed` state. Status can be emitted again when capabilities or health change. Final status includes `clean_shutdown`, `shutdown_signal`, and `exit_code`.
- `nativerelay.loss` reports the updated `loss_generation` and cumulative `losses` ledger whenever a new loss report is detected. The ledger can include exact stream rejections and unknown-count native overflow reports.

Diagnostics are written to stderr, leaving stdout parseable as JSONL. Consumers must treat a timeout or absence of event lines as silence only while status remains usable and no loss has been reported. A loss record makes the affected interval uncertain; it does not imply recovery or replay.
