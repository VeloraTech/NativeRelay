# Linux collector: support, capabilities, and limits

The Linux reference collector implements the Linux event set in roadmap Phases 1–3. Process and filesystem collection use independent kernel sources; file events are filtered to the caller-selected directory and optional path patterns. All observations are best effort, not a complete audit log.

## Process lifecycle: `NETLINK_CONNECTOR/CN_PROC`

The process connector delivers fork and exit notifications through netlink. Fork notifications include parent and child PIDs, so a short-lived child does not need to survive a process-table scan. Thread clones are ignored. Exit events describe the thread-group leader; `leaderThreadExit: true` does not mean every thread in the group has stopped.

The collector opportunistically reads `/proc/<pid>/stat` and `/proc/<pid>/exe` for metadata; those values may no longer be available when userspace handles the event. Arguments are omitted by default and read from `/proc/<pid>/cmdline` only with `--include-command`; command-line values can contain secrets. Timestamps are userspace receipt/normalization times.

Availability depends on kernel configuration, namespace, security policy, and privileges. Netlink multicast subscription generally requires root or `CAP_NET_ADMIN`. The collector waits for the connector acknowledgement before reporting the source as available. A surfaced `NLMSG_OVERRUN` is reported as loss with an unknown count, but kernel-internal or otherwise unreported loss cannot be quantified. Process events remain best effort even when the capability is `available`.

## File activity: `fanotify`

The collector tries fanotify modes in order, preferring file-handle reporting for create, delete, and rename events, then falling back to modes with more limited coverage. Depending on the mode, monitoring may be filesystem-wide and filtered in userspace, mount-scoped, or limited to the exact directory and non-recursive. Mount/filesystem notification generally requires `CAP_SYS_ADMIN`. Namespace boundaries and alternate bind mounts can affect visibility.

| Event type | Support and meaning |
|---|---|
| `file.opened` | Best effort. An open notification does not prove that file contents were read. |
| `file.modified` | Best effort. Does not identify changed bytes or guarantee durable storage. |
| `file.created` | Implemented in file-handle reporting mode; availability depends on kernel/filesystem support and selected mode. |
| `file.deleted` | Implemented in file-handle reporting mode; availability depends on kernel/filesystem support and selected mode. |
| `file.renamed` | Implemented in file-handle reporting mode. `FAN_MOVED_FROM` and `FAN_MOVED_TO` are emitted as separate events with direction metadata; consumers must correlate them. |

The capability report describes the event types available after collector startup. In fallback modes, some event types may be unavailable. The live integration workflow currently exercises open/modify attribution, not create/delete/rename delivery. Do not treat implementation or an `available` state as proof of complete coverage.

File-handle paths are resolved with `open_by_handle_at`; ordinary event-descriptor paths are resolved through `/proc/self/fd`. Path filters use fnmatch-style patterns against the absolute path and basename, and the configured scope boundary is always enforced.

## Loss, privacy, and shutdown

The collector reports fanotify queue overflow, malformed/truncated records, and path-resolution failures to the stream loss ledger. Stream-full rejections have exact counts; native queue overflow has an unknown count. Loss is reported, not recovered. `status["dropped"]` counts stream-full rejections; inspect `status["losses"]` and `loss_generation` for the unified ledger.

The collector does not read file contents, hash files, read environment-variable values, or send telemetry remotely. Command-line collection is opt-in. Elevated privileges are not requested automatically. Shutdown closes native sources before joining their reader threads.

## Validation status

Unit tests cover the event model and stream, connector parsing and acknowledgement, surfaced overrun handling, fanotify loss parsing, and path filtering. The opt-in integration tests use a temporary child process and filesystem objects.

The privileged [Linux collector workflow](../.github/workflows/linux-collector.yml) runs on Ubuntu 22.04 and 24.04 and requires live CN_PROC process lifecycle/parent-PID and fanotify open/modify attribution checks to pass. It does not currently validate live create/delete/rename delivery. On the documented WSL2 development environment, root fanotify open/modify testing passed, while CN_PROC did not acknowledge subscription from the distro's non-initial PID namespace.

Use the runtime `capabilities` command and inspect collector status; a quiet event stream is not evidence that no activity occurred:

```sh
python -m nativerelay.cli capabilities --scope /workspace
python -m nativerelay.cli observe --scope /workspace --json
```
