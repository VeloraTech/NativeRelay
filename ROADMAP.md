# NativeRelay roadmap

## Phase 0 — Repository and design foundation: complete

Project scope, design principles, initial event model, and platform research were documented.

## Phase 1 — Linux reference collector: complete

- [x] Platform-neutral event model and JSON serialization
- [x] Collector contract, capability states, and bounded in-process stream
- [x] Linux process fork/exit collector using CN_PROC
- [x] Linux file open/write-close collector using fanotify
- [x] Caller-selected directory scope and health reporting
- [x] Developer CLI and local JSON output
- [x] Core/decoder tests and implementation-limit documentation
- [x] Opt-in controlled Linux process/filesystem integration test harness
- [x] CN_PROC process lifecycle validation through privileged GitHub Actions Linux integration tests
- [x] Linux integration verification through the Ubuntu 22.04 and 24.04 CI matrix
- [x] fanotify open/modify attribution validated on WSL2 Linux 6.6 (root)

## Phase 2 — Linux reliability and consumer API: complete

- [x] Process connector loss detection: CN_PROC netlink overrun reported as loss with unknown count
- [x] Fanotify reliability: queue overflow, malformed events, path resolution failures reported as loss
- [x] Improved shutdown: sockets closed before thread joins to prevent hangs
- [x] Path filtering: `--include-paths` and `--exclude-paths` with fnmatch patterns for file events
- [x] Consumer API stabilization: CLI options, event/capability versioning, stream cancellation ergonomics
- [x] Integration tests: path filtering unit tests, existing CN_PROC/fanotify integration tests
- [x] Documentation: updated README, linux-support.md, CLI help

## Phase 3 — Filesystem event coverage: complete

- [x] File create events (FAN_CREATE): file-handle reporting with process attribution
- [x] File delete events (FAN_DELETE): file-handle reporting with process attribution
- [x] File rename events (FAN_MOVE/FAN_RENAME): FAN_MOVED_FROM/FAN_MOVED_TO pairs with metadata
- [x] Path filtering for new event types
- [x] Loss reporting for new event types
- [x] Capability reporting for new event types
- [x] Documentation updates

## Phase 4 — Windows research and collector

Evaluate ETW providers, file/process attribution, privileges, supported Windows versions, event loss, and equivalent semantics before implementation.

## Phase 5 — macOS research and collector

Evaluate Endpoint Security permissions, entitlements, event semantics, packaging, and distribution requirements before implementation.

## Later

Cross-platform semantic stabilization and additional event families may follow once multiple working collectors demonstrate a need. No AgentTrace-specific behavior, cloud service, dashboard, file-content capture, or secret collection is planned for this phase.
