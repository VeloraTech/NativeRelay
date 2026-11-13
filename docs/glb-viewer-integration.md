# GLB Viewer Integration Note

NativeRelay exposes hardware information to GLB Viewer through the `glb-viewer.hardware-profile` contract v1.0.0.

## Integration Points

GLB Viewer consumes NativeRelay hardware data via the public Python API:

```python
from nativerelay.hardware import get_hardware_profile, get_resource_snapshot, get_provider_info

provider = get_provider_info()
profile = get_hardware_profile()
snapshot = get_resource_snapshot()
```

## Data Flow

1. NativeRelay collects static hardware facts once (profile) and dynamic resource snapshots on demand.
2. Data is serialized to deterministic JSON with `schema_version: 1`.
3. GLB Viewer reads the JSON (via subprocess, file, or embedded Python) and uses it for adaptive feature decisions.
4. GLB Viewer remains responsible for browser/WebGL capability detection and rendering measurements.

## Contract Guarantees

- Local-only: no hardware data leaves the host
- No admin required for ordinary fields
- No host settings are changed
- `null` for unavailable, empty arrays for none, never zero for unknown
- UTC ISO-8601 timestamps
- Backward-compatible within v1.0.0 (additive changes only)

## Versioning

Contract ID: `glb-viewer.hardware-profile`
Current version: `1.0.0`
Schema: `schemas/hardware-profile.schema.json` (Draft 2020-12)

Breaking changes will increment the contract version and be reflected in `contract_version` and `schema_version`.

## Testing

Run the hardware contract test suite:

```
python -m unittest tests.test_hardware_contract
```

Run the full NativeRelay suite (including regression):

```
python -m unittest discover -s tests
```

## Branch

This work lives on branch `glb-nativerelay-contract`. Do not merge to `main` until the contract, implementation, documentation, and regression tests have been reviewed.
