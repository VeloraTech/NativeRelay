# NativeRelay
[![PyPI Downloads](https://static.pepy.tech/personalized-badge/nativerelay?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/nativerelay)

NativeRelay is a local-first, embeddable operating-system observability layer for developer tools. It normalizes the event interface across platforms, while platform-specific collectors expose different capabilities and coverage.

**Linux and Windows 10/11 have collectors with platform-specific coverage.** Windows process start/exit events use WMI trace notifications when permitted, otherwise a degraded Toolhelp snapshot fallback; scoped recursive file create/modify/delete/rename events use `ReadDirectoryChangesW`, which cannot identify the responsible process. File-open events are unsupported on Windows. macOS collector work is future work. NativeRelay reports native observations; it does not identify AI agents, infer file contents, or depend on AgentTrace.

## Phase 3 status

The Linux collector implements filesystem create, delete, and rename events via fanotify file-handle reporting (FAN_CREATE, FAN_DELETE, FAN_MOVED_FROM/FAN_MOVED_TO), when the kernel and filesystem support the required mode. Events are attributed to the reported process ID and paths are resolved via file handles. Rename notifications are emitted as separate source/destination events with direction metadata; consumers must correlate the pair. File events are filtered by the selected scope and optional include/exclude patterns. The process connector
(`NETLINK_CONNECTOR/CN_PROC`) reports netlink overrun as loss with unknown count;
fanotify queue overflow and malformed events are similarly reported. File events
support fnmatch-style include/exclude patterns. Collector shutdown closes sockets
before joining threads to prevent hangs.

On Windows 10/11, WMI process trace notifications provide process start and stop
events when policy permits. If WMI subscription is denied, NativeRelay falls
back to Toolhelp process snapshots every 200 ms; that best-effort fallback can
miss short-lived processes. Process start events cover activity after collection
starts, not processes already running. `ReadDirectoryChangesW` reports recursive create,
modify, delete, and rename notifications within the selected directory. Those
file events have an unknown actor (`process.pid` is `0`); Windows file-open
events are unsupported. Notification-buffer overflows are reported as loss
with an unknown count.

| Event | Linux status | Notes |
|---|---|---|
| `process.started`, `process.exited` | Best effort | Kernel fork/leader-exit notifications; short-lived forked processes are included when notifications arrive. A leader exit does not guarantee all threads in its group have stopped. CN_PROC netlink overrun reported as loss. |
| `file.opened` | Best effort | `fanotify` open event with PID and path for the selected scope, filtered by path patterns. |
| `file.modified` | Best effort | `fanotify` modification notification; does not report changed bytes or guarantee durable storage. |
| `file.created` | Best effort | `fanotify` create event with PID and path for the selected scope, filtered by path patterns. |
| `file.deleted` | Best effort | `fanotify` delete event with PID and path for the selected scope, filtered by path patterns. |
| `file.renamed` | Best effort | `fanotify` move/rename event with PID and path for the selected scope, filtered by path patterns. |

Capabilities distinguish supported (implemented but not started), available (active), degraded, permission-denied, unavailable, and unsupported states through the capability API/CLI. Stream-full drops are counted exactly; fanotify overflow, malformed events, and path resolution failures are reported with unknown or known loss counts. CN_PROC netlink overrun is reported as loss. Inspect the stream loss ledger and Linux collector status even during receive timeouts. No file contents or environment values are read. Process argv collection is opt-in because arguments can contain secrets.

## Run locally

Python 3.11 or newer is required. Linux and Windows 10/11 use only standard-library Python modules and native OS facilities. From a Linux checkout:

```sh
python -m nativerelay.cli capabilities --scope /path/to/workspace
python -m nativerelay.cli observe --scope /path/to/workspace
python -m nativerelay.cli observe --scope /path/to/workspace --json
nativerelay run --format json --scope /path/to/workspace
# Path filtering:
nativerelay run --format json --scope /path/to/workspace --include-paths "*.py" --exclude-paths "test_*"
```

`nativerelay run --format json` is the installed console command for machine consumers. It writes compact JSON Lines to stdout: normalized event objects retain the event schema, while control records use `record_type` values `nativerelay.status` and `nativerelay.loss` (both schema version 1). Status records report startup, capability/health changes, and shutdown; loss records include the loss generation and cumulative loss ledger. Human diagnostics go to stderr. The command uses the current directory as its default scope and exits cleanly on Ctrl-C, SIGTERM, or Ctrl+Break on Windows. Exit codes are 0 for clean shutdown, 2 for invalid arguments, 3 when collection cannot start or no source is available, and 4 for runtime failure. Loss is reported, never recovered.

The process connector may be unavailable depending on kernel configuration and privilege; subscribing to its netlink multicast group generally requires root or `CAP_NET_ADMIN`. `fanotify` mount monitoring requires `CAP_SYS_ADMIN`; the CLI reports each degraded state. Use least privilege that satisfies the kernel on the target system. `--include-command` opts into process argument collection and should be used with care. On Windows, WMI policy can prevent process trace subscriptions, in which case the collector falls back to 200 ms process snapshots and reports degraded coverage. Windows command-line collection is also opt-in. Path filtering uses fnmatch patterns matched against the full path and basename.

## Embed

The platform-neutral event model, bounded stream, and collector contract live in `nativerelay`; import platform-specific collectors only at the application boundary. A consumer receives normalized `Event` objects, handles timeout and shutdown explicitly, and may serialize with `Event.to_json()`:

```python
from nativerelay import EventStream, StreamClosed
from collectors.linux import LinuxCollector

stream = EventStream(capacity=1024)
collector = LinuxCollector("/workspace")
collector.start(stream)
try:
    while True:
        try:
            event = stream.receive(timeout=1.0)
        except TimeoutError:
            # Consult collector.capabilities and collector.status before treating silence as meaningful.
            continue
        actor = "unknown" if event.process.pid == 0 else str(event.process.pid)
        print(event.type.value, actor, event.resource)
except StreamClosed:
    pass
finally:
    collector.stop()
```

The event contract does not require consumers to import a platform collector at the core boundary. Linux and Windows collectors report their own capabilities; macOS remains future work.

## Documentation

- [Architecture](docs/architecture.md)
- [Event model](docs/event-model.md)
- [Linux support and limits](docs/linux-support.md)
- [Platform support](docs/platform-support.md)
- [Privacy](docs/privacy.md)
- [Development and testing](docs/development.md)
- [Roadmap](ROADMAP.md)
- [Design principles](docs/design-principles.md)

## Project status

NativeRelay 0.4.0 is the stable release of the Linux and Windows collectors. Linux Phases 1–3 in the roadmap are marked complete. The privileged GitHub Actions workflow on Ubuntu 22.04 and 24.04 validates CN_PROC process lifecycle and fanotify open/modify attribution. Create/delete/rename handling is implemented for compatible fanotify modes, but the live Linux integration workflow does not validate those event types. Windows Phase 4 is a good-faith, best-effort implementation: live integration passed on Windows 10 using the Toolhelp process-polling fallback and on Windows Server 2022/2025 CI. WMI-backed process delivery and Windows 11 client behavior remain unverified. Windows file events have no process attribution, and file-open events are unsupported. Prerelease tags build and run tests without publishing to PyPI; stable `vX.Y.Z` tags publish to PyPI only after validation. Capabilities and coverage depend on OS facilities and permissions; use the runtime capability report and read the [platform support matrix](docs/platform-support.md). NativeRelay is best-effort observability, not complete audit coverage.

MIT licensed. No cloud service, account, network connection, or remote telemetry is required.
