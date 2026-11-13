"""NativeRelay hardware contract for GLB Viewer consumption.

Provides a versioned, transport-independent, local-only hardware profile and
dynamic resource snapshot API. Platform-specific collection lives behind a
platform-neutral public contract; no platform imports appear in the neutral
layer.
"""
from .models import (
    Availability,
    BatteryInfo,
    CollectionMetadata,
    CpuInfo,
    CpuVendor,
    DisplayInfo,
    DisplayType,
    GpuDevice,
    GpuVendor,
    HardwareProfile,
    MemoryInfo,
    OsInfo,
    PowerInfo,
    PowerSource,
    ProviderInfo,
    ResourceSnapshot,
    StorageDevice,
    StorageType,
    ThermalZone,
)
from .contract import (
    HardwareContract,
    get_hardware_profile,
    get_resource_snapshot,
    get_provider_info,
)

__all__ = [
    "Availability",
    "BatteryInfo",
    "CollectionMetadata",
    "CpuInfo",
    "CpuVendor",
    "DisplayInfo",
    "DisplayType",
    "GpuDevice",
    "GpuVendor",
    "HardwareContract",
    "HardwareProfile",
    "MemoryInfo",
    "OsInfo",
    "PowerInfo",
    "PowerSource",
    "ProviderInfo",
    "ResourceSnapshot",
    "StorageDevice",
    "StorageType",
    "ThermalZone",
    "get_hardware_profile",
    "get_resource_snapshot",
    "get_provider_info",
]
