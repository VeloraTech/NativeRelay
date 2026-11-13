"""macOS hardware adapter for the NativeRelay hardware contract.

Uses only the Python standard library (ctypes, platform, subprocess). No
third-party dependencies. Admin is not required for ordinary fields.
"""
from __future__ import annotations

import ctypes
import json
import os
import platform
import subprocess
from ctypes import util

from ..models import (
    Availability,
    BatteryInfo,
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
    StorageDevice,
    StorageType,
    ThermalZone,
)
from .base import HardwareAdapter

libc = ctypes.CDLL(util.find_library("c") or "libc.dylib", use_errno=True)


class DarwinHardwareAdapter(HardwareAdapter):
    platform = "darwin"

    def get_hardware_profile(self) -> HardwareProfile:
        os_info = self._collect_os()
        cpu = self._collect_cpu()
        memory = self._collect_memory()
        gpus = self._collect_gpus()
        power = self._collect_power()
        thermal = self._collect_thermal()
        storage = self._collect_storage()
        displays = self._collect_displays()

        return HardwareProfile(
            metadata=self._metadata(),
            os=os_info,
            cpu=cpu,
            memory=memory,
            gpus=gpus,
            power=power,
            thermal=thermal,
            storage=storage,
            displays=displays,
        )

    def get_resource_snapshot(self):
        from ..models import ResourceSnapshot

        memory = self._collect_memory()
        power = self._collect_power()
        return ResourceSnapshot(
            cpu_usage_percent=None,
            memory_usage_bytes=memory.available_bytes,
            memory_usage_percent=memory.usage_percent,
            disk_read_bytes_per_sec=None,
            disk_write_bytes_per_sec=None,
            network_sent_bytes_per_sec=None,
            network_recv_bytes_per_sec=None,
            battery_charge_percent=power.battery.charge_percent if power and power.battery else None,
            battery_charging=power.battery.charging if power and power.battery else None,
            temperature_celsius=None,
            availability=power.availability,
        )

    def _metadata(self):
        from ..models import CollectionMetadata
        return CollectionMetadata(platform=self.platform)

    def _sysctl(self, name: str) -> str | None:
        try:
            result = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def _system_profiler(self, datatype: str) -> list[dict]:
        try:
            result = subprocess.run(
                ["system_profiler", datatype, "-json"],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if result.returncode != 0 or not result.stdout.strip():
            return []
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
        return data

    def _collect_os(self) -> OsInfo:
        name = self._sysctl("kern.osproductname") or "macOS"
        version = self._sysctl("kern.osrelease") or platform.release()
        architecture = platform.machine().lower()
        if architecture in ("arm64", "aarch64"):
            architecture = "arm64"
        elif architecture in ("x86_64", "i386"):
            architecture = "x86_64"
        else:
            architecture = "unknown"
        return OsInfo(name=name, version=version, architecture=architecture,
                      availability=Availability.AVAILABLE)

    def _collect_cpu(self) -> CpuInfo:
        brand = self._sysctl("machdep.cpu.brand_string") or "unknown"
        vendor_raw = self._sysctl("machdep.cpu.vendor") or ""
        logical_raw = self._sysctl("hw.ncpu")
        physical_raw = self._sysctl("hw.physicalcpu")
        max_mhz_raw = self._sysctl("hw.cpufrequency")
        logical_count = 1
        if logical_raw is not None:
            try:
                logical_count = int(logical_raw)
            except ValueError:
                pass
        physical_count = None
        if physical_raw is not None:
            try:
                physical_count = int(physical_raw)
            except ValueError:
                pass
        max_mhz = None
        if max_mhz_raw is not None:
            try:
                max_mhz = float(max_mhz_raw) / 1_000_000.0
            except ValueError:
                pass
        vendor = self._map_cpu_vendor(vendor_raw)
        return CpuInfo(
            vendor=vendor,
            brand=brand,
            logical_count=logical_count,
            physical_count=physical_count,
            max_frequency_mhz=max_mhz,
            availability=Availability.AVAILABLE,
        )

    @staticmethod
    def _map_cpu_vendor(raw: str) -> CpuVendor:
        lower = raw.lower()
        if "intel" in lower or "genuineintel" in lower:
            return CpuVendor.INTEL
        if "amd" in lower or "authenticamd" in lower:
            return CpuVendor.AMD
        if "arm" in lower or "apple" in lower:
            return CpuVendor.ARM
        if "qualcomm" in lower:
            return CpuVendor.QUALCOMM
        if "nvidia" in lower:
            return CpuVendor.NVIDIA
        return CpuVendor.OTHER

    def _collect_memory(self) -> MemoryInfo:
        total_raw = self._sysctl("hw.memsize")
        total_bytes = 0
        if total_raw is not None:
            try:
                total_bytes = int(total_raw)
            except ValueError:
                pass
        # macOS doesn't expose available_bytes via sysctl directly; use vm_stat if available
        available_bytes = None
        usage_percent = None
        try:
            result = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            result = None
        if result and result.returncode == 0:
            pages_free = pages_inactive = pages_speculative = None
            page_size = 4096
            for line in result.stdout.splitlines():
                if "Pages free:" in line:
                    try:
                        pages_free = int(line.split(":")[1].strip().rstrip("."))
                    except (IndexError, ValueError):
                        pass
                elif "Pages inactive:" in line:
                    try:
                        pages_inactive = int(line.split(":")[1].strip().rstrip("."))
                    except (IndexError, ValueError):
                        pass
                elif "Pages speculative:" in line:
                    try:
                        pages_speculative = int(line.split(":")[1].strip().rstrip("."))
                    except (IndexError, ValueError):
                        pass
            if pages_free is not None:
                avail = pages_free
                if pages_inactive is not None:
                    avail += pages_inactive
                if pages_speculative is not None:
                    avail += pages_speculative
                available_bytes = avail * page_size
        if total_bytes > 0 and available_bytes is not None:
            usage_percent = round(((total_bytes - available_bytes) / total_bytes) * 100, 2)
        return MemoryInfo(
            total_bytes=total_bytes,
            available_bytes=available_bytes,
            usage_percent=usage_percent,
            availability=Availability.AVAILABLE if total_bytes > 0 else Availability.UNAVAILABLE,
        )

    def _collect_gpus(self):
        data = self._system_profiler("SPDisplaysDataType")
        devices = []
        for entry in data:
            items = entry.get("_items", [])
            for item in items:
                model = item.get("_name") or item.get("spdisplays_device") or "unknown"
                vendor_raw = item.get("spdisplays_vendor", "").lower()
                vendor = self._map_gpu_vendor(vendor_raw)
                vram = item.get("spdisplays_vram")
                devices.append(GpuDevice(
                    vendor=vendor,
                    model=model,
                    vram_bytes=self._parse_vram(vram),
                    driver_version=None,
                    availability=Availability.AVAILABLE,
                ))
        return tuple(devices)

    @staticmethod
    def _map_gpu_vendor(raw: str) -> GpuVendor:
        if "nvidia" in raw:
            return GpuVendor.NVIDIA
        if "amd" in raw or "ati" in raw:
            return GpuVendor.AMD
        if "intel" in raw:
            return GpuVendor.INTEL
        if "apple" in raw:
            return GpuVendor.APPLE
        if "qualcomm" in raw:
            return GpuVendor.QUALCOMM
        return GpuVendor.OTHER

    @staticmethod
    def _parse_vram(value) -> int | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            digits = "".join(ch for ch in value if ch.isdigit())
            if digits:
                return int(digits) * 1024 * 1024  # assume MB
        return None

    def _collect_power(self) -> PowerInfo:
        # Use pmset or ioreg for battery info
        ac_present = None
        battery = None
        try:
            result = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            result = None
        if result and result.returncode == 0:
            output = result.stdout
            # Parse "Now drawing from 'AC Power'" or "Now drawing from 'Battery Power'"
            if "AC Power" in output:
                ac_present = True
            elif "Battery Power" in output:
                ac_present = False
            # Parse charge percentage
            import re
            pct_match = re.search(r"(\d+)%;", output)
            charge_pct = float(pct_match.group(1)) if pct_match else None
            charging = "charging" in output.lower()
            battery = BatteryInfo(
                present=True,
                charging=charging,
                charge_percent=charge_pct,
                availability=Availability.AVAILABLE,
            )
        source = PowerSource.AC if ac_present is True else (
            PowerSource.BATTERY if ac_present is False else PowerSource.UNKNOWN)
        return PowerInfo(
            source=source,
            ac_present=ac_present,
            battery=battery,
            availability=Availability.AVAILABLE,
        )

    def _collect_thermal(self):
        # macOS thermal info via powermetrics requires sudo; use best-effort
        zones = []
        try:
            result = subprocess.run(["ioreg", "-r", "-n", "IOPlatformThermalProfile"],
                                    capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return tuple(zones)
        if result.returncode != 0:
            return tuple(zones)
        # Best-effort: no direct temperature reading without admin
        return tuple(zones)

    def _collect_storage(self):
        data = self._system_profiler("SPStorageDataType")
        devices = []
        for entry in data:
            items = entry.get("_items", [])
            for item in items:
                device_id = item.get("_name") or "unknown"
                model = item.get("spstorage_product_name") or device_id
                size_bytes = item.get("spstorage_size")
                if isinstance(size_bytes, str):
                    try:
                        size_bytes = int(size_bytes)
                    except ValueError:
                        size_bytes = 0
                devices.append(StorageDevice(
                    device_id=device_id,
                    type=StorageType.UNKNOWN,
                    size_bytes=size_bytes if isinstance(size_bytes, int) else 0,
                    model=model,
                    availability=Availability.AVAILABLE,
                ))
        return tuple(devices)

    def _collect_displays(self):
        data = self._system_profiler("SPDisplaysDataType")
        displays = []
        for entry in data:
            items = entry.get("_items", [])
            for item in items:
                display_id = item.get("_name") or "display"
                # Try to get resolution
                width = item.get("spdisplays_width")
                height = item.get("spdisplays_height")
                refresh = item.get("spdisplays_refreshrate")
                if isinstance(width, str):
                    try:
                        width = int(width)
                    except ValueError:
                        width = 0
                if isinstance(height, str):
                    try:
                        height = int(height)
                    except ValueError:
                        height = 0
                if isinstance(refresh, str):
                    try:
                        refresh = float(refresh)
                    except ValueError:
                        refresh = None
                displays.append(DisplayInfo(
                    id=display_id,
                    type=DisplayType.BUILTIN,
                    width_px=width if isinstance(width, int) else 0,
                    height_px=height if isinstance(height, int) else 0,
                    refresh_rate_hz=refresh if isinstance(refresh, (int, float)) else None,
                    availability=Availability.AVAILABLE,
                ))
        return tuple(displays)
