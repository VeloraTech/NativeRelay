"""Linux hardware adapter for the NativeRelay hardware contract.

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

libc = ctypes.CDLL(util.find_library("c") or "libc.so.6", use_errno=True)


class _SYSINFO(ctypes.Structure):
    _fields_ = [
        ("uptime", ctypes.c_long),
        ("loads", ctypes.c_ulong * 3),
        ("totalram", ctypes.c_ulonglong),
        ("freeram", ctypes.c_ulonglong),
        ("sharedram", ctypes.c_ulonglong),
        ("bufferram", ctypes.c_ulonglong),
        ("totalswap", ctypes.c_ulonglong),
        ("freeswap", ctypes.c_ulonglong),
        ("procs", ctypes.c_ushort),
        ("pad", ctypes.c_ushort),
        ("totalhigh", ctypes.c_ulonglong),
        ("freehigh", ctypes.c_ulonglong),
        ("mem_unit", ctypes.c_uint),
        ("_f", ctypes.c_char * 20),
    ]


class LinuxHardwareAdapter(HardwareAdapter):
    platform = "linux"

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

    def _read_file(self, path: str) -> str | None:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return None

    def _read_first_line(self, path: str) -> str | None:
        content = self._read_file(path)
        if content is None:
            return None
        return content.splitlines()[0] if content.strip() else None

    def _collect_os(self) -> OsInfo:
        name = self._read_first_line("/etc/os-release") or platform.system()
        # Parse PRETTY_NAME from os-release if present
        content = self._read_file("/etc/os-release") or ""
        pretty = None
        for line in content.splitlines():
            if line.startswith("PRETTY_NAME="):
                pretty = line.split("=", 1)[1].strip().strip('"')
                break
        if pretty:
            name = pretty
        version = platform.release()
        architecture = platform.machine().lower()
        return OsInfo(name=name, version=version, architecture=architecture,
                      availability=Availability.AVAILABLE)

    def _collect_cpu(self) -> CpuInfo:
        content = self._read_file("/proc/cpuinfo") or ""
        vendor_raw = ""
        brand = "unknown"
        logical_count = 0
        physical_count = None
        max_mhz = None
        for line in content.splitlines():
            if line.startswith("vendor_id"):
                vendor_raw = line.split(":", 1)[1].strip()
            elif line.startswith("model name"):
                brand = line.split(":", 1)[1].strip() or "unknown"
            elif line.startswith("processor"):
                logical_count += 1
            elif line.startswith("cpu cores") and physical_count is None:
                try:
                    physical_count = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith("cpu MHz") and max_mhz is None:
                try:
                    max_mhz = float(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
        if logical_count == 0:
            logical_count = 1
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
        if "arm" in lower:
            return CpuVendor.ARM
        if "apple" in lower:
            return CpuVendor.APPLE
        if "qualcomm" in lower:
            return CpuVendor.QUALCOMM
        if "nvidia" in lower:
            return CpuVendor.NVIDIA
        return CpuVendor.OTHER

    def _collect_memory(self) -> MemoryInfo:
        content = self._read_file("/proc/meminfo") or ""
        total_bytes = 0
        available_bytes = None
        for line in content.splitlines():
            if line.startswith("MemTotal:"):
                try:
                    total_bytes = int(line.split()[1]) * 1024
                except (IndexError, ValueError):
                    pass
            elif line.startswith("MemAvailable:"):
                try:
                    available_bytes = int(line.split()[1]) * 1024
                except (IndexError, ValueError):
                    pass
        usage_percent = None
        if total_bytes > 0 and available_bytes is not None:
            usage_percent = round(((total_bytes - available_bytes) / total_bytes) * 100, 2)
        return MemoryInfo(
            total_bytes=total_bytes,
            available_bytes=available_bytes,
            usage_percent=usage_percent,
            availability=Availability.AVAILABLE if total_bytes > 0 else Availability.UNAVAILABLE,
        )

    def _collect_gpus(self):
        records = self._lspci()
        devices = []
        for rec in records:
            class_code = rec.get("class", "")
            if "VGA compatible controller" not in class_code and "3D controller" not in class_code:
                continue
            model = rec.get("device", "unknown")
            vendor_raw = rec.get("vendor", "").lower()
            vendor = self._map_gpu_vendor(vendor_raw)
            devices.append(GpuDevice(
                vendor=vendor,
                model=model,
                vram_bytes=None,
                driver_version=None,
                availability=Availability.AVAILABLE,
            ))
        return tuple(devices)

    def _lspci(self):
        try:
            result = subprocess.run(["lspci", "-mm"], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return []
        if result.returncode != 0:
            return []
        devices = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            devices.append({
                "slot": parts[0],
                "vendor": parts[1],
                "device": parts[2],
                "class": parts[4] if len(parts) > 4 else "",
            })
        return devices

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

    def _collect_power(self) -> PowerInfo:
        # Read AC adapter and battery from sysfs (class/power_supply)
        ac_present = None
        battery = None
        try:
            ps_dir = "/sys/class/power_supply"
            entries = os.listdir(ps_dir)
        except OSError:
            entries = []
        for entry in entries:
            entry_path = os.path.join(ps_dir, entry)
            type_file = os.path.join(entry_path, "type")
            type_val = self._read_first_line(type_file)
            if type_val == "Mains":
                ac_file = os.path.join(entry_path, "online")
                ac_val = self._read_first_line(ac_file)
                ac_present = (ac_val == "1") if ac_val is not None else None
            elif type_val == "Battery":
                charge_file = os.path.join(entry_path, "charge_now")
                energy_file = os.path.join(entry_path, "energy_now")
                full_file = os.path.join(entry_path, "charge_full")
                full_energy_file = os.path.join(entry_path, "energy_full")
                voltage_file = os.path.join(entry_path, "voltage_now")
                status_file = os.path.join(entry_path, "status")
                capacity_file = os.path.join(entry_path, "capacity")

                charge_now = self._read_int(charge_file)
                energy_now = self._read_int(energy_file)
                charge_full = self._read_int(full_file)
                energy_full = self._read_int(full_energy_file)
                voltage = self._read_int(voltage_file)
                status = self._read_first_line(status_file)
                capacity = self._read_int(capacity_file)

                charging = None
                if status:
                    charging = status.strip().lower() in ("charging",)

                charge_pct = None
                if capacity is not None:
                    charge_pct = float(capacity)
                elif charge_now is not None and charge_full:
                    charge_pct = round((charge_now / charge_full) * 100, 2)
                elif energy_now is not None and energy_full:
                    charge_pct = round((energy_now / energy_full) * 100, 2)

                energy_full_mwh = energy_full if energy_full is not None else None
                energy_now_mwh = energy_now if energy_now is not None else None

                battery = BatteryInfo(
                    present=True,
                    charging=charging,
                    charge_percent=charge_pct,
                    energy_full_mwh=energy_full_mwh,
                    energy_now_mwh=energy_now_mwh,
                    voltage_mv=voltage,
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

    @staticmethod
    def _read_int(path: str) -> int | None:
        val = LinuxHardwareAdapter()._read_first_line(path)
        if val is None:
            return None
        try:
            return int(val.strip())
        except ValueError:
            return None

    def _collect_thermal(self):
        zones = []
        try:
            thermal_dir = "/sys/class/thermal"
            entries = os.listdir(thermal_dir)
        except OSError:
            return tuple(zones)
        for i, entry in enumerate(sorted(entries)):
            entry_path = os.path.join(thermal_dir, entry)
            type_file = os.path.join(entry_path, "type")
            temp_file = os.path.join(entry_path, "temp")
            crit_file = os.path.join(entry_path, "critical_temp")
            zone_type = self._read_first_line(type_file) or entry
            temp_raw = self._read_first_line(temp_file)
            temp_c = None
            if temp_raw is not None:
                try:
                    temp_c = round(int(temp_raw.strip()) / 1000.0, 2)
                except ValueError:
                    pass
            crit_raw = self._read_first_line(crit_file)
            crit_c = None
            if crit_raw is not None:
                try:
                    crit_c = round(int(crit_raw.strip()) / 1000.0, 2)
                except ValueError:
                    pass
            zones.append(ThermalZone(
                id=zone_type,
                temperature_celsius=temp_c,
                critical_celsius=crit_c,
                availability=Availability.AVAILABLE if temp_c is not None else Availability.UNAVAILABLE,
            ))
        return tuple(zones)

    def _collect_storage(self):
        devices = []
        try:
            block_dir = "/sys/class/block"
            entries = os.listdir(block_dir)
        except OSError:
            return tuple(devices)
        for entry in sorted(entries):
            if entry.startswith("loop") or entry.startswith("ram"):
                continue
            dev_path = os.path.join(block_dir, entry)
            size_file = os.path.join(dev_path, "size")
            model_file = os.path.join(dev_path, "device", "model")
            size_raw = self._read_first_line(size_file)
            model = self._read_first_line(model_file) or entry
            size_bytes = None
            if size_raw is not None:
                try:
                    size_bytes = int(size_raw.strip()) * 512
                except ValueError:
                    pass
            devices.append(StorageDevice(
                device_id=entry,
                type=StorageType.UNKNOWN,
                size_bytes=size_bytes if size_bytes is not None else 0,
                model=model,
                availability=Availability.AVAILABLE if size_bytes is not None else Availability.UNAVAILABLE,
            ))
        return tuple(devices)

    def _collect_displays(self):
        # Linux display info is best-eest effort; no serials or usernames.
        displays = []
        try:
            result = subprocess.run(["xrandr", "--query"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return tuple(displays)
        if result.returncode != 0:
            return tuple(displays)
        for line in result.stdout.splitlines():
            if " connected " in line:
                parts = line.split()
                name = parts[0]
                dev_type = DisplayType.BUILTIN if "eDP" in name or "LVDS" in name else DisplayType.EXTERNAL
                width, height, refresh = self._parse_xrandr_mode(line)
                displays.append(DisplayInfo(
                    id=name,
                    type=dev_type,
                    width_px=width,
                    height_px=height,
                    refresh_rate_hz=refresh,
                    availability=Availability.AVAILABLE,
                ))
        return tuple(displays)

    @staticmethod
    def _parse_xrandr_mode(line: str):
        # Look for a mode like "1920x1080" followed by optional refresh
        import re
        match = re.search(r"(\d+)x(\d+)", line)
        if not match:
            return 0, 0, None
        width = int(match.group(1))
        height = int(match.group(2))
        refresh = None
        refresh_match = re.search(r"(\d+\.?\d*)\s*\*", line)
        if refresh_match:
            try:
                refresh = float(refresh_match.group(1))
            except ValueError:
                pass
        return width, height, refresh
