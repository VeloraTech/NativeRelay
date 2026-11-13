# NativeRelay Hardware Contract

Contract ID: `glb-viewer.hardware-profile`
Version: `1.0.0`
Provider: `NativeRelay`
Status: implemented (branch `glb-nativerelay-contract`, not merged to `main`)

## Purpose

NativeRelay collects and normalizes static hardware facts and dynamic resource snapshots for consumption by VeloraTech/GLB Viewer. GLB Viewer remains responsible for browser/WebGL capability detection, actual rendering measurements, and adaptive feature decisions.

NativeRelay does not transmit hardware information externally. Collection is local-only.

## Public API

All operations live in `nativerelay.hardware` and are platform-neutral. No platform-specific imports appear in the neutral layer.

### `get_provider_info() -> ProviderInfo`

Returns provider and contract identification:

- `name`: `"NativeRelay"`
- `contract_id`: `"glb-viewer.hardware-profile"`
- `contract_version`: `"1.0.0"`
- `supported_platforms`: `("windows", "linux", "darwin")`
- `operations`: `("get_hardware_profile", "get_resource_snapshot", "get_provider_info")`
- `local_only`: `true`
- `requires_admin`: `false`
- `schema_version`: `1`

### `get_hardware_profile(platform=None) -> HardwareProfile`

Collects the static hardware profile for the given platform (defaults to the running platform). Collected once; not continuously polled.

Returns a `HardwareProfile` containing:

- `metadata` — collection metadata (contract ID, version, provider, collected_at UTC ISO-8601, collector, platform, schema_version)
- `os` — OS name, version, architecture, availability
- `cpu` — vendor, brand, logical/physical count, max frequency MHz, availability
- `memory` — total bytes, available bytes, usage percent, availability
- `gpus` — tuple of GPU devices (vendor, model, VRAM bytes, driver version, availability)
- `power` — power source, AC present, battery info, availability
- `thermal` — tuple of thermal zones (id, temperature Celsius, critical Celsius, availability)
- `storage` — tuple of storage devices (device ID, type, size bytes, model, availability)
- `displays` — tuple of displays (id, type, width/height px, refresh rate Hz, availability)

Unknown platforms degrade gracefully with all subsystems marked `unsupported`.

### `get_resource_snapshot(platform=None) -> ResourceSnapshot`

Collects a dynamic resource snapshot (separate from the static profile, not continuously polled). Contains CPU usage percent, memory usage bytes/percent, disk/network bytes per second, battery charge percent/charging, temperature Celsius, and availability.

## Data Semantics

- Bytes, MHz, Celsius, watts, mWh, mV, seconds
- Percentages are 0-100
- Timestamps are UTC ISO-8601 with timezone offset
- `null` for unavailable; empty arrays for none; never zero for unknown
- Availability states: `available`, `unavailable`, `unsupported`, `permission_denied`, `not_applicable`, `estimated`, `error`

## Privacy

- Local-only collection; no external transmission
- No serial numbers, MAC addresses, usernames, or product keys
- No admin required for ordinary fields
- No host power, thermal, or performance settings are changed

## Performance

- No continuous polling
- Static profile is cheap to collect
- Dynamic snapshot is separate from the profile
- Does not block the NativeRelay event stream

## Platform Adapters

| Platform | Adapter module | Status |
|----------|---------------|--------|
| Windows | `nativerelay.hardware.platform.windows` | Implemented (WMI, registry, syscalls) |
| Linux | `nativerelay.hardware.platform.linux` | Implemented (/proc, sysfs, lspci, xrandr) |
| macOS | `nativerelay.hardware.platform.darwin` | Implemented (sysctl, system_profiler) |

Adapters are registered by platform id (`windows`, `linux`, `darwin`) and are lazy-imported only when their platform is detected.

## JSON Schema

`schemas/hardware-profile.schema.json` — Draft 2020-12 schema for `glb-viewer.hardware-profile` v1.0.0.

## Example Fixtures

`tests/fixtures/` contains 7 deterministic scenarios:

1. `hardware_profile_windows_available.json` — full Windows profile
2. `hardware_profile_linux_available.json` — full Linux profile
3. `hardware_profile_darwin_available.json` — full macOS profile
4. `hardware_profile_partial_collection.json` — unavailable subsystems
5. `hardware_profile_permission_denied.json` — permission denied subsystems
6. `hardware_profile_unsupported_platform.json` — unknown platform
7. `resource_snapshot_example.json` — dynamic resource snapshot

## Tests

`tests/test_hardware_contract.py` — 32 tests covering model validation, serialization round-trips, nullability, enum values, contract operations, platform fallback, partial collection, and fixture validity.

## Constraints

- Preserves NativeRelay's existing process/file observability contracts and behavior
- Python 3.11+, standard-library only, no runtime dependencies
- Does not change the existing event schema or CLI
- Branch `glb-nativerelay-contract` is intentionally separate from `main`; do not merge until reviewed
