"""Data models for the NativeRelay hardware contract (glb-viewer.hardware-profile v1.0.0).

All models are frozen dataclasses with deterministic JSON serialization.
Platform-neutral: no platform-specific imports appear in this module.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Availability(str, Enum):
    """Availability state for a hardware field or subsystem."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    PERMISSION_DENIED = "permission_denied"
    NOT_APPLICABLE = "not_applicable"
    ESTIMATED = "estimated"
    ERROR = "error"


class CpuVendor(str, Enum):
    INTEL = "intel"
    AMD = "amd"
    ARM = "arm"
    APPLE = "apple"
    QUALCOMM = "qualcomm"
    NVIDIA = "nvidia"
    OTHER = "other"
    UNKNOWN = "unknown"


class GpuVendor(str, Enum):
    NVIDIA = "nvidia"
    AMD = "amd"
    INTEL = "intel"
    APPLE = "apple"
    QUALCOMM = "qualcomm"
    OTHER = "other"
    UNKNOWN = "unknown"


class StorageType(str, Enum):
    HDD = "hdd"
    SSD = "ssd"
    NVME = "nvme"
    EMMC = "emmc"
    USB = "usb"
    OTHER = "other"
    UNKNOWN = "unknown"


class DisplayType(str, Enum):
    BUILTIN = "builtin"
    EXTERNAL = "external"
    PROJECTOR = "projector"
    VIRTUAL = "virtual"
    UNKNOWN = "unknown"


class PowerSource(str, Enum):
    AC = "ac"
    BATTERY = "battery"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CollectionMetadata:
    """Metadata about how and when the hardware profile was collected."""

    contract_id: str = "glb-viewer.hardware-profile"
    contract_version: str = "1.0.0"
    provider: str = "NativeRelay"
    collected_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    collector: str = "native"
    platform: str = ""
    schema_version: int = 1

    def __post_init__(self):
        if not self.contract_id or not isinstance(self.contract_id, str):
            raise ValueError("contract_id is required")
        if not self.contract_version or not isinstance(self.contract_version, str):
            raise ValueError("contract_version is required")
        if not self.provider or not isinstance(self.provider, str):
            raise ValueError("provider is required")
        try:
            parsed = datetime.fromisoformat(self.collected_at.replace("Z", "+00:00"))
            if parsed.utcoffset() is None:
                raise ValueError("collected_at must include a timezone offset")
        except (ValueError, AttributeError) as exc:
            raise ValueError("collected_at must be UTC ISO-8601 with timezone") from exc
        if not isinstance(self.schema_version, int) or self.schema_version < 1:
            raise ValueError("schema_version must be a positive integer")


@dataclass(frozen=True)
class OsInfo:
    """Operating system and machine identification."""

    name: str
    version: str
    architecture: str
    availability: Availability = Availability.AVAILABLE

    def __post_init__(self):
        if not self.name or not isinstance(self.name, str):
            raise ValueError("os name is required")
        if not self.version or not isinstance(self.version, str):
            raise ValueError("os version is required")
        if not self.architecture or not isinstance(self.architecture, str):
            raise ValueError("os architecture is required")
        if not isinstance(self.availability, Availability):
            object.__setattr__(self, "availability", Availability(self.availability))


@dataclass(frozen=True)
class CpuInfo:
    """CPU identification and static characteristics."""

    vendor: CpuVendor
    brand: str
    logical_count: int
    physical_count: int | None = None
    max_frequency_mhz: float | None = None
    availability: Availability = Availability.AVAILABLE

    def __post_init__(self):
        if not isinstance(self.vendor, CpuVendor):
            object.__setattr__(self, "vendor", CpuVendor(self.vendor))
        if not self.brand or not isinstance(self.brand, str):
            raise ValueError("cpu brand is required")
        if not isinstance(self.logical_count, int) or self.logical_count < 0:
            raise ValueError("cpu logical_count must be a non-negative integer (zero means unknown)")
        if self.physical_count is not None and (
            not isinstance(self.physical_count, int) or self.physical_count < 1
        ):
            raise ValueError("cpu physical_count must be a positive integer or None")
        if self.max_frequency_mhz is not None and (
            not isinstance(self.max_frequency_mhz, (int, float)) or self.max_frequency_mhz < 0
        ):
            raise ValueError("cpu max_frequency_mhz must be non-negative or None")
        if not isinstance(self.availability, Availability):
            object.__setattr__(self, "availability", Availability(self.availability))


@dataclass(frozen=True)
class MemoryInfo:
    """System memory characteristics."""

    total_bytes: int
    available_bytes: int | None = None
    usage_percent: float | None = None
    availability: Availability = Availability.AVAILABLE

    def __post_init__(self):
        if not isinstance(self.total_bytes, int) or self.total_bytes < 0:
            raise ValueError("memory total_bytes must be a non-negative integer")
        if self.available_bytes is not None and (
            not isinstance(self.available_bytes, int) or self.available_bytes < 0
        ):
            raise ValueError("memory available_bytes must be a non-negative integer or None")
        if self.usage_percent is not None and (
            not isinstance(self.usage_percent, (int, float)) or not 0 <= self.usage_percent <= 100
        ):
            raise ValueError("memory usage_percent must be 0-100 or None")
        if not isinstance(self.availability, Availability):
            object.__setattr__(self, "availability", Availability(self.availability))


@dataclass(frozen=True)
class GpuDevice:
    """A single GPU device."""

    vendor: GpuVendor
    model: str
    vram_bytes: int | None = None
    driver_version: str | None = None
    availability: Availability = Availability.AVAILABLE

    def __post_init__(self):
        if not isinstance(self.vendor, GpuVendor):
            object.__setattr__(self, "vendor", GpuVendor(self.vendor))
        if not self.model or not isinstance(self.model, str):
            raise ValueError("gpu model is required")
        if self.vram_bytes is not None and (
            not isinstance(self.vram_bytes, int) or self.vram_bytes < 0
        ):
            raise ValueError("gpu vram_bytes must be a non-negative integer or None")
        if not isinstance(self.availability, Availability):
            object.__setattr__(self, "availability", Availability(self.availability))


@dataclass(frozen=True)
class BatteryInfo:
    """Battery characteristics and current state."""

    present: bool
    charging: bool | None = None
    charge_percent: float | None = None
    energy_full_mwh: int | None = None
    energy_now_mwh: int | None = None
    voltage_mv: int | None = None
    time_remaining_seconds: int | None = None
    availability: Availability = Availability.AVAILABLE

    def __post_init__(self):
        if not isinstance(self.present, bool):
            raise ValueError("battery present must be a boolean")
        if self.charging is not None and not isinstance(self.charging, bool):
            raise ValueError("battery charging must be a boolean or None")
        if self.charge_percent is not None and (
            not isinstance(self.charge_percent, (int, float)) or not 0 <= self.charge_percent <= 100
        ):
            raise ValueError("battery charge_percent must be 0-100 or None")
        if self.energy_full_mwh is not None and (
            not isinstance(self.energy_full_mwh, int) or self.energy_full_mwh < 0
        ):
            raise ValueError("battery energy_full_mwh must be a non-negative integer or None")
        if self.energy_now_mwh is not None and (
            not isinstance(self.energy_now_mwh, int) or self.energy_now_mwh < 0
        ):
            raise ValueError("battery energy_now_mwh must be a non-negative integer or None")
        if self.voltage_mv is not None and (
            not isinstance(self.voltage_mv, int) or self.voltage_mv < 0
        ):
            raise ValueError("battery voltage_mv must be a non-negative integer or None")
        if self.time_remaining_seconds is not None and (
            not isinstance(self.time_remaining_seconds, int) or self.time_remaining_seconds < 0
        ):
            raise ValueError("battery time_remaining_seconds must be a non-negative integer or None")
        if not isinstance(self.availability, Availability):
            object.__setattr__(self, "availability", Availability(self.availability))


@dataclass(frozen=True)
class PowerInfo:
    """Power management characteristics."""

    source: PowerSource
    ac_present: bool | None = None
    battery: BatteryInfo | None = None
    availability: Availability = Availability.AVAILABLE

    def __post_init__(self):
        if not isinstance(self.source, PowerSource):
            object.__setattr__(self, "source", PowerSource(self.source))
        if self.ac_present is not None and not isinstance(self.ac_present, bool):
            raise ValueError("power ac_present must be a boolean or None")
        if self.battery is not None and not isinstance(self.battery, BatteryInfo):
            raise ValueError("power battery must be a BatteryInfo or None")
        if not isinstance(self.availability, Availability):
            object.__setattr__(self, "availability", Availability(self.availability))


@dataclass(frozen=True)
class ThermalZone:
    """A single thermal zone/sensor."""

    id: str
    temperature_celsius: float | None = None
    critical_celsius: float | None = None
    availability: Availability = Availability.AVAILABLE

    def __post_init__(self):
        if not self.id or not isinstance(self.id, str):
            raise ValueError("thermal id is required")
        if self.temperature_celsius is not None and not isinstance(self.temperature_celsius, (int, float)):
            raise ValueError("thermal temperature_celsius must be a number or None")
        if self.critical_celsius is not None and not isinstance(self.critical_celsius, (int, float)):
            raise ValueError("thermal critical_celsius must be a number or None")
        if not isinstance(self.availability, Availability):
            object.__setattr__(self, "availability", Availability(self.availability))


@dataclass(frozen=True)
class StorageDevice:
    """A single storage device."""

    device_id: str
    type: StorageType
    size_bytes: int
    model: str | None = None
    availability: Availability = Availability.AVAILABLE

    def __post_init__(self):
        if not self.device_id or not isinstance(self.device_id, str):
            raise ValueError("storage device_id is required")
        if not isinstance(self.type, StorageType):
            object.__setattr__(self, "type", StorageType(self.type))
        if not isinstance(self.size_bytes, int) or self.size_bytes < 0:
            raise ValueError("storage size_bytes must be a non-negative integer")
        if self.model is not None and not isinstance(self.model, str):
            raise ValueError("storage model must be a string or None")
        if not isinstance(self.availability, Availability):
            object.__setattr__(self, "availability", Availability(self.availability))


@dataclass(frozen=True)
class DisplayInfo:
    """A single display."""

    id: str
    type: DisplayType
    width_px: int
    height_px: int
    refresh_rate_hz: float | None = None
    availability: Availability = Availability.AVAILABLE

    def __post_init__(self):
        if not self.id or not isinstance(self.id, str):
            raise ValueError("display id is required")
        if not isinstance(self.type, DisplayType):
            object.__setattr__(self, "type", DisplayType(self.type))
        if not isinstance(self.width_px, int) or self.width_px < 1:
            raise ValueError("display width_px must be a positive integer")
        if not isinstance(self.height_px, int) or self.height_px < 1:
            raise ValueError("display height_px must be a positive integer")
        if self.refresh_rate_hz is not None and (
            not isinstance(self.refresh_rate_hz, (int, float)) or self.refresh_rate_hz <= 0
        ):
            raise ValueError("display refresh_rate_hz must be positive or None")
        if not isinstance(self.availability, Availability):
            object.__setattr__(self, "availability", Availability(self.availability))


@dataclass(frozen=True)
class HardwareProfile:
    """Complete static hardware profile (glb-viewer.hardware-profile v1.0.0).

    Collected once; not continuously polled. All fields use null for unavailable
    and empty arrays for none. Never zero for unknown.
    """

    metadata: CollectionMetadata
    os: OsInfo
    cpu: CpuInfo
    memory: MemoryInfo
    gpus: tuple[GpuDevice, ...] = field(default_factory=tuple)
    power: PowerInfo | None = None
    thermal: tuple[ThermalZone, ...] = field(default_factory=tuple)
    storage: tuple[StorageDevice, ...] = field(default_factory=tuple)
    displays: tuple[DisplayInfo, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": asdict(self.metadata),
            "os": asdict(self.os),
            "cpu": asdict(self.cpu),
            "memory": asdict(self.memory),
            "gpus": [asdict(g) for g in self.gpus],
            "power": asdict(self.power) if self.power else None,
            "thermal": [asdict(t) for t in self.thermal],
            "storage": [asdict(s) for s in self.storage],
            "displays": [asdict(d) for d in self.displays],
            "schema_version": 1,
        }

    def to_json(self) -> str:
        import json

        return json.dumps(self.to_dict(), separators=(",", ":"), ensure_ascii=True, sort_keys=True)


@dataclass(frozen=True)
class ResourceSnapshot:
    """Dynamic resource snapshot (separate from static profile, not continuously polled)."""

    collected_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    cpu_usage_percent: float | None = None
    memory_usage_bytes: int | None = None
    memory_usage_percent: float | None = None
    disk_read_bytes_per_sec: float | None = None
    disk_write_bytes_per_sec: float | None = None
    network_sent_bytes_per_sec: float | None = None
    network_recv_bytes_per_sec: float | None = None
    battery_charge_percent: float | None = None
    battery_charging: bool | None = None
    temperature_celsius: float | None = None
    availability: Availability = Availability.AVAILABLE

    def __post_init__(self):
        try:
            parsed = datetime.fromisoformat(self.collected_at.replace("Z", "+00:00"))
            if parsed.utcoffset() is None:
                raise ValueError("collected_at must include a timezone offset")
        except (ValueError, AttributeError) as exc:
            raise ValueError("collected_at must be UTC ISO-8601 with timezone") from exc
        for name, value in (
            ("cpu_usage_percent", self.cpu_usage_percent),
            ("memory_usage_percent", self.memory_usage_percent),
            ("battery_charge_percent", self.battery_charge_percent),
        ):
            if value is not None and not 0 <= value <= 100:
                raise ValueError(f"{name} must be 0-100 or None")
        for name, value in (
            ("memory_usage_bytes", self.memory_usage_bytes),
            ("disk_read_bytes_per_sec", self.disk_read_bytes_per_sec),
            ("disk_write_bytes_per_sec", self.disk_write_bytes_per_sec),
            ("network_sent_bytes_per_sec", self.network_sent_bytes_per_sec),
            ("network_recv_bytes_per_sec", self.network_recv_bytes_per_sec),
        ):
            if value is not None and (
                not isinstance(value, (int, float)) or value < 0
            ):
                raise ValueError(f"{name} must be non-negative or None")
        if self.temperature_celsius is not None and not isinstance(self.temperature_celsius, (int, float)):
            raise ValueError("temperature_celsius must be a number or None")
        if not isinstance(self.availability, Availability):
            object.__setattr__(self, "availability", Availability(self.availability))

    def to_dict(self) -> dict[str, Any]:
        return {
            "collected_at": self.collected_at,
            "cpu_usage_percent": self.cpu_usage_percent,
            "memory_usage_bytes": self.memory_usage_bytes,
            "memory_usage_percent": self.memory_usage_percent,
            "disk_read_bytes_per_sec": self.disk_read_bytes_per_sec,
            "disk_write_bytes_per_sec": self.disk_write_bytes_per_sec,
            "network_sent_bytes_per_sec": self.network_sent_bytes_per_sec,
            "network_recv_bytes_per_sec": self.network_recv_bytes_per_sec,
            "battery_charge_percent": self.battery_charge_percent,
            "battery_charging": self.battery_charging,
            "temperature_celsius": self.temperature_celsius,
            "availability": self.availability.value,
            "schema_version": 1,
        }

    def to_json(self) -> str:
        import json

        return json.dumps(self.to_dict(), separators=(",", ":"), ensure_ascii=True, sort_keys=True)


@dataclass(frozen=True)
class ProviderInfo:
    """Provider identification and contract support."""

    name: str = "NativeRelay"
    contract_id: str = "glb-viewer.hardware-profile"
    contract_version: str = "1.0.0"
    supported_platforms: tuple[str, ...] = field(default_factory=lambda: ("windows", "linux", "darwin"))
    operations: tuple[str, ...] = field(default_factory=lambda: ("get_hardware_profile", "get_resource_snapshot", "get_provider_info"))
    local_only: bool = True
    requires_admin: bool = False
    schema_version: int = 1

    def __post_init__(self):
        if not self.name or not isinstance(self.name, str):
            raise ValueError("provider name is required")
        if not self.contract_id or not isinstance(self.contract_id, str):
            raise ValueError("contract_id is required")
        if not self.contract_version or not isinstance(self.contract_version, str):
            raise ValueError("contract_version is required")
        if not all(isinstance(p, str) and p for p in self.supported_platforms):
            raise ValueError("supported_platforms must contain non-empty strings")
        if not all(isinstance(o, str) and o for o in self.operations):
            raise ValueError("operations must contain non-empty strings")
        if not isinstance(self.local_only, bool):
            raise ValueError("local_only must be a boolean")
        if not isinstance(self.requires_admin, bool):
            raise ValueError("requires_admin must be a boolean")
        if not isinstance(self.schema_version, int) or self.schema_version < 1:
            raise ValueError("schema_version must be a positive integer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "contract_id": self.contract_id,
            "contract_version": self.contract_version,
            "supported_platforms": list(self.supported_platforms),
            "operations": list(self.operations),
            "local_only": self.local_only,
            "requires_admin": self.requires_admin,
            "schema_version": self.schema_version,
        }

    def to_json(self) -> str:
        import json

        return json.dumps(self.to_dict(), separators=(",", ":"), ensure_ascii=True, sort_keys=True)
