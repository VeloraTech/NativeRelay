# Development and testing

NativeRelay uses Python 3.11+ and has no runtime dependencies. Run from the repository root:

```sh
python -m unittest discover -s tests -v
python -m nativerelay.cli capabilities --scope /tmp
python -m nativerelay.cli observe --scope /tmp --json
nativerelay run --format json --scope /tmp
```

On Windows 10/11, use a Windows directory for `--scope`, for example:

```powershell
python -m nativerelay.cli capabilities --scope "$env:TEMP"
nativerelay run --format json --scope "$env:TEMP"
```

Core tests are platform-independent. Process decoder tests use constructed connector messages and do not require kernel privileges. Live integration checks for CN_PROC and fanotify must be performed on Linux; process connector subscription generally requires root or `CAP_NET_ADMIN`, and fanotify mount notification requires root or `CAP_SYS_ADMIN`. Use a fresh temporary directory and helper process for integration tests. Do not rely on the developer's home directory or internet access.

The live Linux checks are opt-in so ordinary unit runs remain unprivileged and deterministic:

```sh
NATIVERELAY_LINUX_INTEGRATION=1 python3 -m unittest discover -s tests -p test_linux_integration.py -v
```

The tests launch a short-lived controlled child and create/write a file under a temporary directory. They verify fork/exit delivery, parent PID, file open/modify delivery, and PID attribution. A test skips when its kernel facility or required privilege is unavailable. A passing skip is not evidence of live telemetry support; record whether each test ran or skipped on the target kernel.

Set `NATIVERELAY_LINUX_INTEGRATION_REQUIRED=1` in privileged CI to fail instead of skip when either kernel mechanism is unavailable. The Linux workflow runs this strict mode as root so missing CN_PROC or fanotify coverage makes the job fail visibly.

The controlled CN_PROC and fanotify integration tests passed in the privileged GitHub Actions Linux workflow targeting Ubuntu 22.04 and 24.04. On the current WSL2 Linux 6.6.87 kernel, fanotify passed as root and correctly failed without privilege. CN_PROC subscription timed out for both the normal distro user and root because the distro uses a non-initial PID namespace; the hosted Linux CI run provides the live CN_PROC validation.

Windows unit and live integration checks are in `tests/test_windows.py`. The live test uses WMI process start/stop notifications (or the explicitly degraded Toolhelp snapshot fallback when WMI access is denied) and a temporary directory to verify recursive create/modify/delete/rename events. Run it on Windows 10/11 with:

```powershell
python -m unittest discover -s tests -p test_windows.py -v
```

Windows CI uses hosted Windows Server runners to exercise these APIs; that is not a substitute for validating on Windows 11 client systems. File notifications do not include process identity, and Windows file-open events are unsupported.

The legacy `observe --json` command emits one compact JSON event per line. The installed `nativerelay run --format json` command is intended for machine consumers: event lines use the canonical event schema, and `nativerelay.status` / `nativerelay.loss` control records make startup, health changes, known loss, and shutdown explicit. All human diagnostics go to stderr. Ctrl-C and SIGTERM stop the collector and drain already-buffered events before the final status record. Exit codes are 0 for clean shutdown, 2 for argument errors, 3 when collection cannot start or has no available source, and 4 for runtime failure. Stream overflow rejects new events; neither stream drops nor native-source loss can be recovered.
