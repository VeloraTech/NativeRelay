# GLB Viewer Hardware Contract Workstream

This branch, `glb-nativerelay-contract`, is dedicated to implementing the NativeRelay hardware-information contract consumed by VeloraTech/GLB-Viewer.

The implementation instructions are maintained in the GLB Viewer integration branch:

https://github.com/VeloraTech/GLB-Viewer/blob/integration/native-relay-hardware/docs/integrations/native-relay/NATIVERELAY_IMPLEMENTATION_PROMPT.md

## Scope

Implement a versioned, transport-independent, local-only hardware profile and dynamic resource snapshot API for GLB Viewer.

NativeRelay collects and normalizes host facts. GLB Viewer remains responsible for browser/WebGL capability detection, actual rendering measurements and adaptive feature decisions.

## Existing project constraints

- Preserve NativeRelay's existing process/file observability contracts and behavior.
- Follow the existing Python 3.11+ and standard-library-first architecture.
- Keep platform-specific collection behind a platform-neutral public contract.
- Do not claim a platform or field is supported unless implementation and tests demonstrate it.
- Do not transmit hardware information externally.
- Do not change host power, thermal or performance settings.

This branch is intentionally separate from `main`. Do not merge it until the contract, implementation, documentation and regression tests have been reviewed.
