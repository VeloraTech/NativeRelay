# NativeRelay Hardware Support Matrix

Contract: `glb-viewer.hardware-profile` v1.0.0

## Field Support by Platform

| Field | Windows | Linux | macOS | Notes |
|-------|---------|-------|-------|-------|
| OS name/version/architecture | available | available | available | |
| CPU vendor/brand/count/frequency | available | available | available | |
| Memory total/available/usage | available | available | available | available_bytes via vm_stat on macOS |
| GPU vendor/model/VRAM/driver | available | available | available | VRAM best-effort |
| Power source/AC present | available | available | available | |
| Battery present/charging/percent | available | available | available | |
| Battery energy/voltage/time | partial | partial | partial | sysfs on Linux, limited on macOS |
| Thermal zones/temperature | available (WMI) | available (sysfs) | unsupported | macOS requires admin for temps |
| Storage device/type/size/model | available | available | available | type best-effort on Linux/macOS |
| Displays id/type/resolution/refresh | available | available (xrandr) | available (system_profiler) | |

## Availability States

- `available` — field collected successfully
- `unavailable` — field could not be collected at runtime
- `unsupported` — platform or field not supported on this platform
- `permission_denied` — collection requires elevated privileges
- `not_applicable` — field does not apply (e.g., battery on desktop)
- `estimated` — value is an estimate, not a direct measurement
- `error` — collection encountered an error

## Privilege Requirements

No admin is required for ordinary fields on any platform. Some fields (thermal on macOS, certain battery details) may be limited without elevated privileges and are reported as `unavailable` or `unsupported` rather than failing.

## Known Limitations

- macOS thermal temperatures require admin privileges; reported as empty with `unsupported` availability
- Linux storage type is best-effort; defaults to `unknown`
- Linux display info requires X11/xrandr; empty on headless or Wayland-only systems
- Windows GPU VRAM and driver version depend on WMI availability
