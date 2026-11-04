# Architecture

NativeRelay separates the event contract from OS-specific collection.

```text
Operating system
        ↓
Platform collector (Linux; Windows 10/11; macOS future)
        ↓
NativeRelay normalized Event / bounded EventStream
        ↓
Consumer
```

Linux uses CN_PROC and fanotify. Windows uses WMI process trace notifications, a Toolhelp snapshot fallback when WMI is unavailable, and `ReadDirectoryChangesW` for scoped filesystem notifications. The core event, capability, and stream contract has no platform API imports. macOS collection remains future work.

The `nativerelay` package owns event types, validation/serialization, capability states, a collector contract, and a bounded in-process stream. `collectors/linux` owns the Linux kernel mechanisms, `/proc` metadata lookup, path scoping, and health reporting. `collectors/windows` owns WMI process subscription, directory notifications, path scoping, and Windows health reporting.

This architecture normalizes the interface, not the underlying guarantees. A consumer inspects capabilities and collector status; unsupported telemetry is never represented as an empty stream. Windows file notifications cannot identify an actor, so the normalized process PID is `0` when the source provides no process identity. Windows does not emit `file.opened`. See the [platform support matrix](platform-support.md) and [Linux support](linux-support.md) for source-specific behavior.

The current scope is a directory selected by the caller. Process lifecycle events are host-visible kernel notifications, subject to kernel policy and event loss. File events use a mount mark filtered to that directory. The local event stream is bounded and does not persist events. JSON is a serialization/debug format, not a trusted or tamper-proof ledger.

NativeRelay reports observations only. Process IDs are not application identities; file opens are not proof that file data was read. Higher-level identity or interpretation remains the responsibility of a consumer. NativeRelay has no AgentTrace dependency or integration logic.
