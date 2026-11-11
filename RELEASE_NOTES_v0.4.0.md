# NativeRelay v0.4.0 — Stable Release

NativeRelay 0.4.0 is the stable release of the Windows collector, alongside the existing Linux collector.

## Highlights

- Windows process lifecycle monitoring using WMI when available, with Toolhelp polling fallback.
- Scoped recursive filesystem notifications for file creation, modification, deletion, and rename.
- Graceful JSONL shutdown handling, including Windows Ctrl+Break support.
- Capability and health reporting so consumers can distinguish available, degraded, and unavailable collection.
- Loss reporting for detected notification and stream drops.
- No new runtime Python dependencies.

## Platform support

- **Windows 10/11:** process monitoring and scoped filesystem notifications. WMI may be unavailable on some hosts; Toolhelp polling can miss short-lived processes. File-open events are not supported. Windows 11 client and WMI-path validation remain limited.
- **Linux:** existing CN_PROC and fanotify collectors remain available subject to kernel support and privileges.
- **macOS:** no collector is provided.

## Install

```bash
python -m pip install nativerelay==0.4.0
nativerelay --version
```

## Validation

- Stable tag: `v0.4.0`
- GitHub Actions Windows collector validation: passed.
- GitHub Actions Linux collector validation: passed.
- PyPI Trusted Publishing: passed.
- AgentTrace integration: installs `nativerelay==0.4.0` from PyPI and verifies Windows process-start/process-exit and file-create/file-modify observations.

## Known limitations

- Windows file events are not attributed to the process that caused them.
- Windows file-open events are unsupported.
- Toolhelp polling may miss short-lived processes and does not report processes already running when collection starts.
- WMI may be blocked by host policy or permissions.
- Filesystem monitoring is limited to the configured scope; detected loss is reported but missed events cannot be replayed.
