"""Public hardware contract API (platform-neutral).

Operations:
  - get_hardware_profile() -> HardwareProfile
  - get_resource_snapshot() -> ResourceSnapshot
  - get_provider_info() -> ProviderInfo

Platform-specific collection lives behind adapters; this module imports no
platform-specific modules.
"""
from __future__ import annotations

from .models import (
    Availability,
    HardwareProfile,
    ProviderInfo,
    ResourceSnapshot,
)


class HardwareContract:
    """Facade over platform hardware adapters.

    Adapters are registered per platform and return platform-neutral models.
    Unknown platforms degrade gracefully with unsupported availability states.
    """

    def __init__(self, adapters: dict[str, "HardwareAdapter"] | None = None):
        self._adapters = dict(adapters or {})

    def register_adapter(self, platform: str, adapter: "HardwareAdapter") -> None:
        """Register a platform adapter (lowercase platform id)."""
        if not isinstance(platform, str) or not platform:
            raise ValueError("platform is required")
        self._adapters[platform.lower()] = adapter

    def get_provider_info(self) -> ProviderInfo:
        """Return provider and contract identification."""
        return ProviderInfo()

    def get_hardware_profile(self, platform: str | None = None) -> HardwareProfile:
        """Collect the static hardware profile for the given platform.

        platform defaults to the running platform. Unknown platforms return a
        profile with all subsystems marked unsupported.
        """
        import platform as _platform

        target = (platform or _platform.system().lower())
        adapter = self._adapters.get(target)
        if adapter is None:
            return _unsupported_profile(target)
        return adapter.get_hardware_profile()

    def get_resource_snapshot(self, platform: str | None = None) -> ResourceSnapshot:
        """Collect a dynamic resource snapshot for the given platform.

        platform defaults to the running platform. Unknown platforms return a
        snapshot with availability=unsupported.
        """
        import platform as _platform

        target = (platform or _platform.system().lower())
        adapter = self._adapters.get(target)
        if adapter is None:
            return _unsupported_snapshot()
        return adapter.get_resource_snapshot()


def _unsupported_profile(platform: str) -> HardwareProfile:
    from .models import (
        CollectionMetadata,
        CpuInfo,
        CpuVendor,
        DisplayInfo,
        DisplayType,
        GpuDevice,
        GpuVendor,
        MemoryInfo,
        OsInfo,
        PowerInfo,
        PowerSource,
        StorageDevice,
        StorageType,
        ThermalZone,
    )

    return HardwareProfile(
        metadata=CollectionMetadata(platform=platform),
        os=OsInfo(name="unknown", version="unknown", architecture="unknown",
                  availability=Availability.UNSUPPORTED),
        cpu=CpuInfo(vendor=CpuVendor.UNKNOWN, brand="unknown", logical_count=0,
                    availability=Availability.UNSUPPORTED),
        memory=MemoryInfo(total_bytes=0, availability=Availability.UNSUPPORTED),
        gpus=(),
        power=PowerInfo(source=PowerSource.UNKNOWN, availability=Availability.UNSUPPORTED),
        thermal=(),
        storage=(),
        displays=(),
    )


def _unsupported_snapshot() -> ResourceSnapshot:
    return ResourceSnapshot(availability=Availability.UNSUPPORTED)


class HardwareAdapter:
    """Interface for platform hardware adapters.

    Implementations live in platform-specific modules and must not be imported
    by the neutral contract layer.
    """

    def get_hardware_profile(self) -> HardwareProfile:
        raise NotImplementedError

    def get_resource_snapshot(self) -> ResourceSnapshot:
        raise NotImplementedError


# Default contract instance for module-level convenience functions.
_default_contract = HardwareContract()


def get_hardware_profile(platform: str | None = None) -> HardwareProfile:
    """Collect the static hardware profile for the given platform."""
    return _default_contract.get_hardware_profile(platform)


def get_resource_snapshot(platform: str | None = None) -> ResourceSnapshot:
    """Collect a dynamic resource snapshot for the given platform."""
    return _default_contract.get_resource_snapshot(platform)


def get_provider_info() -> ProviderInfo:
    """Return provider and contract identification."""
    return _default_contract.get_provider_info()
