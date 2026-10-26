# NativeRelay
[![PyPI Downloads](https://static.pepy.tech/personalized-badge/nativerelay?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/nativerelay)

NativeRelay is a local-first, embeddable operating-system observability layer for developer tools. It normalizes the event interface across platforms, while platform-specific collectors expose different capabilities and coverage.

**System telemetry is currently Linux-supported.** macOS and Windows collectors are future work. NativeRelay reports native observations; it does not identify AI agents, infer file contents, or depend on AgentTrace.

## Phase 3 status

The Linux collector adds filesystem create, delete, and rename events via fanotify's file-handle reporting (FAN_CREATE, FAN_DELETE, FAN_MOVE). Events are attributed to the responsible process (PID reported via FAN_REPORT_PID) and paths are resolved via file handles (open_by_handle_at). Events are filtered by the selected scope and optional include/exclude patterns. Rename events are emitted as FAN_MOVED_FROM/FAN_MOVED_TO pairs with metadata indicating direction. (`--include-paths`/`--exclude-paths`),
improved shutdown reliability, and enhanced loss reporting. The process connector
(`NETLINK_CONNECTOR/CN_PROC`) reports netlink overrun as loss with unknown count;
fanotify queue overflow and malformed events are similarly reported. File events
support fnmatch-style include/exclude patterns. Collector shutdown closes sockets
before joining threads to prevent hangs.

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

Python 3.11 or newer, Linux, and no third-party runtime dependencies are required. From a checkout:

```sh
python -m nativerelay.cli capabilities --scope /path/to/workspace
python -m nativerelay.cli observe --scope /path/to/workspace
python -m nativerelay.cli observe --scope /path/to/workspace --json
nativerelay run --format json --scope /path/to/workspace
# Path filtering (Phase 2):
nativerelay run --format json --scope /path/to/workspace --include-paths "*.py" --exclude-paths "test_*"
```

`nativerelay run --format json` is the installed console command for machine consumers. It writes compact JSON Lines to stdout: normalized event objects retain the event schema, while control records use `record_type` values `nativerelay.status` and `nativerelay.loss` (both schema version 1). Status records report startup, capability/health changes, and shutdown; loss records include the loss generation and cumulative loss ledger. Human diagnostics go to stderr. The command uses the current directory as its default scope and exits cleanly on Ctrl-C or SIGTERM. Exit codes are 0 for clean shutdown, 2 for invalid arguments, 3 when collection cannot start or no source is available, and 4 for runtime failure. Loss is reported, never recovered.

The process connector may be unavailable depending on kernel configuration and privilege; subscribing to its netlink multicast group generally requires root or `CAP_NET_ADMIN`. `fanotify` mount monitoring requires `CAP_SYS_ADMIN`; the CLI reports each degraded state. Use least privilege that satisfies the kernel on the target system. `--include-command` opts into process argument collection and should be used with care. Path filtering uses fnmatch patterns matched against the full path and basename.

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
        print(event.type.value, event.process.pid, event.resource)
except StreamClosed:
    pass
finally:
    collector.stop()
```

The event contract does not require consumers to import `collectors.linux`; future macOS and Windows collectors can implement the common `Collector` interface while reporting their own capabilities.

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

Phase 2 is complete for the documented event set and remains pre-release. Path filtering for file events (`--include-paths`/`--exclude-paths`), improved shutdown reliability (socket closure before thread join), and enhanced loss reporting (CN_PROC netlink overrun, fanotify overflow, malformed events, path resolution failures) are implemented. Controlled CN_PROC process-lifecycle and fanotify file-event integration tests passed in the privileged GitHub Actions Linux workflow; the workflow targets Ubuntu 22.04 and 24.04. The fanotify smoke test also passed under root in WSL2 on Linux 6.6.87. WSL could not validate CN_PROC because the distro uses a non-initial PID namespace. Precise CN_PROC loss accounting remains best-effort (netlink overrun is detected but kernel-internal queue loss cannot be precisely quantified). NativeRelay does not provide complete audit coverage. See the capability and limitations documentation before use.

MIT licensed. No cloud service, account, network connection, or remote telemetry is required.
