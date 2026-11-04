# Platform support — Capability Matrix

NativeRelay normalizes the event interface across platforms, while platform-specific collectors expose different capabilities and coverage.

**Status legend:**
- **Implemented & Verified** — Code exists, tests pass, validated on real OS in CI
- **Implemented (Best Effort)** — Code exists, but kernel/OS limits prevent complete coverage; loss reported
- **Partially Supported** — Some events implemented, others not; see details
- **Unsupported** — Intentionally not implemented; appears in `EventType` but never emitted
- **Planned / not supported** — No validated supported collector is available; experimental or incomplete source does not count as support

---

## Linux (Phases 1–3 — Implemented, best effort)

| Capability | Event Types | Status | OS Mechanism | Required Privileges | Known Limitations | Verification Evidence |
|---|---|---|---|---|---|---|
| Process start | `process.started` | Implemented & Verified | `NETLINK_CONNECTOR/CN_PROC` fork notification | `CAP_NET_ADMIN` (or root) | Short-lived children included when notification arrives; thread clones filtered; leader exit != all threads stopped; CN_PROC silently ignored outside initial PID/user namespaces; no precise kernel queue loss accounting | CI: Ubuntu 22.04 & 24.04 (root); unit tests for fork packet parsing, thread clone filtering, connector ack handling |
| Process exit | `process.exited` | Implemented & Verified | `NETLINK_CONNECTOR/CN_PROC` exit notification | `CAP_NET_ADMIN` (or root) | Reports thread-group leader exit with `leaderThreadExit: true`; multithreaded process may have other threads running; exit status/signal reported | CI: Ubuntu 22.04 & 24.04 (root); unit tests for exit packet parsing, metadata |
| Process connector loss detection | - | Implemented (Best Effort) | `NLMSG_OVERRUN` netlink message | `CAP_NET_ADMIN` | Only detects netlink buffer overrun; kernel-internal CN_PROC queue loss cannot be precisely quantified; reported as loss with unknown count | Unit test: `test_cn_proc_netlink_overrun_is_reported_as_native_loss` |
| File open | `file.opened` | Implemented & Verified | `fanotify` (`FAN_OPEN`) mount mark + `/proc/self/fd` resolution | `CAP_SYS_ADMIN` (or root) | Mount-wide notification filtered to scope + fnmatch patterns; bind mounts may bypass mark; container/PID/mount namespace boundaries apply; does not prove content read | CI: Ubuntu 22.04 & 24.04 (root); WSL2 6.6.87 (root); unit tests for fanotify parsing, overflow, malformed events, path resolution failures |
| File modify | `file.modified` | Implemented & Verified | `fanotify` (`FAN_MODIFY`) mount mark + `/proc/self/fd` resolution | `CAP_SYS_ADMIN` (or root) | Same as file open; does not identify changed bytes or guarantee durable storage | CI: Ubuntu 22.04 & 24.04 (root); WSL2 6.6.87 (root); unit tests |
| File create | `file.created` | Implemented (Best Effort) | `fanotify` (`FAN_CREATE`) file-handle reporting | `CAP_SYS_ADMIN` (or root) | Requires a compatible fanotify mode/kernel/filesystem; runtime capability may be unavailable; path filtered to scope | Implemented; live create delivery is not covered by current integration workflow |
| File delete | `file.deleted` | Implemented (Best Effort) | `fanotify` (`FAN_DELETE`) file-handle reporting | `CAP_SYS_ADMIN` (or root) | Requires a compatible fanotify mode/kernel/filesystem; runtime capability may be unavailable; path filtered to scope | Implemented; live delete delivery is not covered by current integration workflow |
| File rename | `file.renamed` | Implemented (Best Effort) | `fanotify` move notifications with file-handle reporting | `CAP_SYS_ADMIN` (or root) | Source and destination notifications are separate events with direction metadata; consumer correlation required; runtime capability may be unavailable | Implemented; live rename delivery is not covered by current integration workflow |
| Fanotify loss detection | - | Implemented | `FAN_Q_OVERFLOW`, malformed/truncated events, path resolution failures | `CAP_SYS_ADMIN` | Queue overflow reported with unknown count; malformed/truncated events reported; path resolution failures report known masked operation count | Unit tests: `test_fanotify_overflow_packet_is_reported_with_sequence`, `test_native_loss_reserves_sequence_and_reports_unknown_count`, `test_linux_stream_drop_creates_sequence_gap_and_loss_report` |
| Path filtering | - | Implemented & Verified | Userspace fnmatch on resolved absolute path + basename | None (userspace) | Patterns matched against full path and basename; include patterns require at least one match; exclude patterns filter out; scope boundary always enforced | 5 new unit tests: scope matching, include patterns, exclude patterns, combined, collector init |
| Shutdown reliability | - | Implemented | Socket/fd closure before thread join | None | Sockets closed to unblock blocking reads; threads joined with 2s timeout; loss info preserved after stream close | Unit tests verify stop() behavior; manual verification |
| Event stream bounded buffer | - | Implemented | In-process `queue.Queue` | None | Capacity configurable (default 1024); drops counted exactly; loss reported with sequence | Unit tests: `test_bounded_stream_and_timeout`, `test_linux_stream_drop_creates_sequence_gap_and_loss_report` |
| Process argv collection | - | Implemented (Opt-in) | `/proc/<pid>/cmdline` read | `CAP_DAC_READ_SEARCH` (or root) | Opt-in via `--include-command`; may contain secrets; excluded by default | Unit tests verify command field present/absent |

**Linux CI Verification:**
- GitHub Actions: Ubuntu 22.04 & 24.04 (root) — CN_PROC process lifecycle + parent PID; fanotify open/modify attribution
- WSL2: Linux 6.6.87 (root) — fanotify smoke test passed; CN_PROC unavailable (non-initial PID namespace)
- Linux parser tests run in the cross-platform unit suite; Linux live tests run in the privileged Ubuntu CI matrix

## Windows (Phase 4 — Implemented, best effort; Windows 10/11 target)

The CLI selects `WindowsCollector` on Windows. The live integration test is designed for Windows 10/11. Local live validation is performed on Windows 10; Windows CI uses Windows Server runners for API coverage, not direct Windows 11 client validation.

| Capability | Event Types | Status | OS Mechanism | Required Privileges | Known Limitations | Verification |
|---|---|---|---|---|---|---|
| Process start/exit | `process.started`, `process.exited` | Implemented (Best Effort) | WMI `Win32_ProcessStartTrace` / `Win32_ProcessStopTrace`, with Toolhelp snapshot polling fallback | WMI policy for notifications; snapshot fallback uses standard process enumeration | No initial events for already-running processes; fallback polls every 200 ms and can miss short-lived processes; command line is only queried in WMI mode with explicit opt-in | Windows 10 live integration test and Windows CI |
| File open | `file.opened` | Unsupported | Not provided by `ReadDirectoryChangesW` | — | No file-open events | Capability reports unsupported |
| File create/modify/delete/rename | `file.created`, `file.modified`, `file.deleted`, `file.renamed` | Implemented (Best Effort) | Recursive `ReadDirectoryChangesW` | Read/list access to selected directory | No process attribution (`process.pid` is 0); rename old/new notifications are separate; activity outside scope is not monitored | Windows 10 live integration test and Windows CI |
| Loss detection | - | Implemented (Best Effort) | Directory notification overflow / zero-byte completion and bounded event stream | Same as directory notifications | Native overflow count is unknown; no recovery/replay | Unit and live integration tests |
| Path filtering | - | Implemented | Userspace fnmatch against absolute path or basename | None beyond directory access | Scope boundary always enforced | Unit tests |
| Process command line | - | Implemented (Opt-in) | WMI `Win32_Process` query at process-start notification | WMI process-query access | May race with short-lived processes and can contain secrets; excluded by default | Unit coverage / opt-in |
| Shutdown | - | Implemented | Stop WMI subscription process and cancel overlapped directory I/O | None | Native cancellation behavior is OS-dependent | Live integration test |

File activity is not attributed to the process that caused it: PID 0 is the explicit unknown-actor sentinel. File-open telemetry and a complete audit guarantee are not provided.

---

## macOS (Phase 5 — Planned)

| Capability | Event Types | Status | OS Mechanism (Candidate) | Required Privileges | Known Limitations | Verification Evidence |
|---|---|---|---|---|---|---|
| Process start | `process.started` | Planned | Endpoint Security (`ES_EVENT_TYPE_NOTIFY_EXEC`), `proc_notify` | Full Disk Access + `com.apple.security.endpoint-security` entitlement | Requires Apple-granted entitlement; not available in App Store; SIP may limit visibility | None — Phase 5 research not started |
| Process exit | `process.exited` | Planned | Endpoint Security (`ES_EVENT_TYPE_NOTIFY_EXIT`) | Same as start | Same as start | None |
| File open | `file.opened` | Planned | Endpoint Security (`ES_EVENT_TYPE_NOTIFY_OPEN`), FSEvents | Same as start | ES requires entitlement; FSEvents no process attribution; no real-time modify | None |
| File modify | `file.modified` | Planned | Endpoint Security, FSEvents | Same as start | No native real-time modify with process attribution | None |
| File create/delete/rename | `file.created`, `file.deleted`, `file.renamed` | Planned | Endpoint Security, FSEvents | Same as start | FSEvents provides rename/create/delete but no process attribution | None |
| Loss detection | - | Planned | Endpoint Security dropped events | Same as start | ES reports dropped event count | None |
| Path filtering | - | Planned | Userspace fnmatch | None | Same as Linux | None |

**Research needed:** Endpoint Security entitlement process, App Store distribution constraints, SIP/transparency impacts, process attribution reliability, event ordering, event loss behavior, packaging requirements.

---

## Summary Matrix

| Capability | Linux | Windows | macOS |
|---|---|---|---|
| `process.started` | Implemented & Verified | Implemented (Best Effort) | Planned |
| `process.exited` | Implemented & Verified | Implemented (Best Effort) | Planned |
| `file.opened` | Implemented & Verified | Unsupported | Planned |
| `file.modified` | Implemented & Verified | Implemented (Best Effort) | Planned |
| `file.created` | Implemented (Best Effort) | Implemented (Best Effort) | Planned |
| `file.deleted` | Implemented (Best Effort) | Implemented (Best Effort) | Planned |
| `file.renamed` | Implemented (Best Effort) | Implemented (Best Effort) | Planned |
| Loss detection | Implemented (Best Effort) | Implemented (Best Effort) | Planned |
| Path filtering | Implemented & Verified | Implemented | Planned |
| Opt-in argv | Implemented | Implemented (Best Effort) | Planned |
| Shutdown reliability | Implemented | Implemented | Planned |

---

## Governance

- **Update this matrix** whenever a capability changes status (implementation, test, verification, limitation discovered).
- **Keep consistent** with `ROADMAP.md`, `linux-support.md`, implementation code, and test results.
- **Do not mark** Windows/macOS capabilities as implemented based on roadmap alone — only when code exists, tests pass, and real-OS verification is documented.
- **Query collector `capabilities`** at runtime; do not infer support from `EventType` enum presence.