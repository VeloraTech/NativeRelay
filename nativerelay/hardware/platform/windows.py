"""Windows hardware adapter for the NativeRelay hardware contract.

Uses only the Python standard library (ctypes, platform, subprocess, winreg).
No third-party dependencies. Admin is not required for ordinary fields.
"""
from __future__ import annotations

import ctypes
import json
import os
import platform
import subprocess
import winreg
from ctypes import wintypes

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

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
powrprof = ctypes.WinDLL("powrprof", use_last_error=True)


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", wintypes.BYTE),
        ("BatteryFlag", wintypes.BYTE),
        ("BatteryLifePercent", wintypes.BYTE),
        ("Reserved1", wintypes.BYTE),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("DeviceName", wintypes.WCHAR * 32),
        ("DeviceString", wintypes.WCHAR * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", wintypes.WCHAR * 128),
        ("DeviceKey", wintypes.WCHAR * 128),
    ]


class WindowsHardwareAdapter(HardwareAdapter):
    platform = "windows"

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

    # -- internal helpers --

    def _metadata(self):
        from ..models import CollectionMetadata
        return CollectionMetadata(platform=self.platform)

    def _wmi(self, query: str):
        """Run a WMI query via PowerShell and return parsed JSON records."""
        ps = (
            "$console = [Console]::OutputEncoding;"
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false);"
            f"Get-WmiObject -Query '{query}' | Select-Object * | ConvertTo-Json -Depth 3;"
            "[Console]::OutputEncoding = $console;"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
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
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return data
        return []

    def _registry_value(self, hive, path, value_name):
        try:
            with winreg.OpenKey(hive, path, 0, winreg.KEY_READ) as key:
                value, _ = winreg.QueryValueEx(key, value_name)
                return value
        except OSError:
            return None

    def _collect_os(self) -> OsInfo:
        name = self._registry_value(winreg.HKEY_LOCAL_MACHINE,
                                    r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
                                    "ProductName") or platform.system()
        version = self._registry_value(winreg.HKEY_LOCAL_MACHINE,
                                       r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
                                       "DisplayVersion") or platform.release()
        architecture = platform.machine().lower()
        if architecture in ("amd64", "x86_64"):
            architecture = "x86_64"
        elif architecture in ("i386", "i686", "x86"):
            architecture = "x86"
        elif architecture == "arm64":
            architecture = "arm64"
        else:
            architecture = "unknown"
        return OsInfo(name=name, version=version, architecture=architecture,
                      availability=Availability.AVAILABLE)

    def _collect_cpu(self) -> CpuInfo:
        records = self._wmi("SELECT Name, Manufacturer, NumberOfLogicalProcessors, NumberOfCores, MaxClockSpeed FROM Win32_Processor")
        if records:
            rec = records[0]
            brand = rec.get("Name") or "unknown"
            vendor_raw = (rec.get("Manufacturer") or "").lower()
            vendor = self._map_cpu_vendor(vendor_raw)
            logical = rec.get("NumberOfLogicalProcessors")
            physical = rec.get("NumberOfCores")
            max_mhz = rec.get("MaxClockSpeed")
            return CpuInfo(
                vendor=vendor,
                brand=brand,
                logical_count=int(logical) if isinstance(logical, (int, float)) else 1,
                physical_count=int(physical) if isinstance(physical, (int, float)) else None,
                max_frequency_mhz=float(max_mhz) if isinstance(max_mhz, (int, float)) else None,
                availability=Availability.AVAILABLE,
            )
        return CpuInfo(vendor=CpuVendor.UNKNOWN, brand=platform.processor() or "unknown",
                       logical_count=1, availability=Availability.UNAVAILABLE)

    @staticmethod
    def _map_cpu_vendor(raw: str) -> CpuVendor:
        if "intel" in raw:
            return CpuVendor.INTEL
        if "amd" in raw:
            return CpuVendor.AMD
        if "arm" in raw:
            return CpuVendor.ARM
        if "apple" in raw:
            return CpuVendor.APPLE
        if "qualcomm" in raw:
            return CpuVendor.QUALCOMM
        if "nvidia" in raw:
            return CpuVendor.NVIDIA
        return CpuVendor.OTHER

    def _collect_memory(self) -> MemoryInfo:
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return MemoryInfo(
                total_bytes=int(status.ullTotalPhys),
                available_bytes=int(status.ullAvailPhys),
                usage_percent=float(status.dwMemoryLoad),
                availability=Availability.AVAILABLE,
            )
        return MemoryInfo(total_bytes=0, availability=Availability.UNAVAILABLE)

    def _collect_gpus(self):
        records = self._wmi("SELECT Name, AdapterRAM, VideoProcessor, DriverVersion FROM Win32_VideoController")
        devices = []
        for rec in records:
            model = rec.get("Name") or "unknown"
            vendor_raw = (rec.get("VideoProcessor") or "").lower()
            vendor = self._map_gpu_vendor(vendor_raw)
            vram = rec.get("AdapterRAM")
            driver = rec.get("DriverVersion")
            devices.append(GpuDevice(
                vendor=vendor,
                model=model,
                vram_bytes=int(vram) if isinstance(vram, (int, float)) else None,
                driver_version=driver if isinstance(driver, str) else None,
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

    def _collect_power(self) -> PowerInfo:
        status = _SYSTEM_POWER_STATUS()
        if powrprof.GetSystemPowerStatus(ctypes.byref(status)):
            ac_line = status.ACLineStatus
            source = PowerSource.AC if ac_line == 1 else (PowerSource.BATTERY if ac_line == 255 else PowerSource.UNKNOWN)
            battery_flag = status.BatteryFlag
            present = battery_flag not in (255, 0)
            charging = None
            if present:
                charging = bool(battery_flag & 8)
            charge_pct = status.BatteryLifePercent
            if charge_pct == 255:
                charge_pct = None
            return PowerInfo(
                source=source,
                ac_present=(ac_line == 1),
                battery=BatteryInfo(
                    present=present,
                    charging=charging,
                    charge_percent=float(charge_pct) if charge_pct is not None else None,
                    availability=Availability.AVAILABLE,
                ),
                availability=Availability.AVAILABLE,
            )
        return PowerInfo(source=PowerSource.UNKNOWN, availability=Availability.UNAVAILABLE)

    def _collect_thermal(self):
        records = self._wmi("SELECT CurrentTemperature FROM MSAcpi_ThermalZoneTemperature")
        zones = []
        for i, rec in enumerate(records):
            temp = rec.get("CurrentTemperature")
            celsius = None
            if isinstance(temp, (int, float)):
                celsius = (temp / 10.0) - 273.15
            zones.append(ThermalZone(
                id=f"thermal-{i}",
                temperature_celsius=celsius,
                availability=Availability.AVAILABLE if celsius is not None else Availability.UNAVAILABLE,
            ))
        return tuple(zones)

    def _collect_storage(self):
        records = self._wmi("SELECT Model, Size, MediaType FROM Win32_DiskDrive")
        devices = []
        for rec in records:
            model = rec.get("Model") or "unknown"
            size = rec.get("Size")
            media = (rec.get("MediaType") or "").lower()
            storage_type = self._map_storage_type(media)
            devices.append(StorageDevice(
                device_id=model,
                type=storage_type,
                size_bytes=int(size) if isinstance(size, (int, float)) else 0,
                model=model,
                availability=Availability.AVAILABLE,
            ))
        return tuple(devices)

    @staticmethod
    def _map_storage_type(raw: str) -> StorageType:
        if "ssd" in raw:
            return StorageType.SSD
        if "nvme" in raw:
            return StorageType.NVME
        if "hdd" in raw or "hard disk" in raw:
            return StorageType.HDD
        if "emmc" in raw:
            return StorageType.EMMC
        if "usb" in raw:
            return StorageType.USB
        return StorageType.UNKNOWN

    def _collect_displays(self):
        devices = []
        index = 0
        while True:
            dd = _DISPLAY_DEVICEW()
            dd.cb = ctypes.sizeof(_DISPLAY_DEVICEW)
            if not user32.EnumDisplayDevicesW(None, index, ctypes.byref(dd), 0):
                break
            if dd.StateFlags & 0x00000001:  # DISPLAY_DEVICE_ACTIVE
                dev_type = DisplayType.BUILTIN if dd.StateFlags & 0x00000008 else DisplayType.EXTERNAL
                # Get resolution via EnumDisplaySettings
                width, height, refresh = self._display_resolution(dd.DeviceName)
                devices.append(DisplayInfo(
                    id=dd.DeviceName,
                    type=dev_type,
                    width_px=width,
                    height_px=height,
                    refresh_rate_hz=refresh,
                    availability=Availability.AVAILABLE,
                ))
            index += 1
        return tuple(devices)

    def _display_resolution(self, device_name: str):
        try:
            from ctypes import wintypes as _w

            class _DEVMODEW(ctypes.Structure):
                _fields_ = [
                    ("dmDeviceName", _w.WCHAR * 32),
                    ("dmSpecVersion", _w.WORD),
                    ("dmDriverVersion", _w.WORD),
                    ("dmSize", _w.WORD),
                    ("dmDriverExtra", _w.WORD),
                    ("dmFields", _w.DWORD),
                    ("dmPosition", _w.POINTL),
                    ("dmDisplayOrientation", _w.DWORD),
                    ("dmDisplayFixedOutput", _w.DWORD),
                    ("dmColor", _w.SHORT),
                    ("dmDuplex", _w.SHORT),
                    ("dmYResolution", _w.SHORT),
                    ("dmTTOption", _w.SHORT),
                    ("dmCollate", _w.SHORT),
                    ("dmFormName", _w.WCHAR * 32),
                    ("dmLogPixels", _w.WORD),
                    ("dmBitsPerPel", _w.DWORD),
                    ("dmPelsWidth", _w.DWORD),
                    ("dmPelsHeight", _w.DWORD),
                    ("dmDisplayFlags", _w.DWORD),
                    ("dmDisplayFrequency", _w.DWORD),
                ]

            dm = _DEVMODEW()
            dm.dmSize = ctypes.sizeof(_DEVMODEW)
            if user32.EnumDisplaySettingsW(device_name, 0xFFFFFFFF, ctypes.byref(dm)):
                return int(dm.dmPelsWidth), int(dm.dmPelsHeight), float(dm.dmDisplayFrequency) or None
        except OSError:
            pass
        return 0, 0, None
