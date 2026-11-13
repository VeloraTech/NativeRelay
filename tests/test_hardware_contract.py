"""Tests for the NativeRelay hardware contract (glb-viewer.hardware-profile v1.0.0)."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from nativerelay.hardware import (
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
    HardwareContract,
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
from nativerelay.hardware.contract import HardwareAdapter


class ModelValidationTests(unittest.TestCase):
    def test_collection_metadata_defaults(self):
        m = CollectionMetadata()
        self.assertEqual(m.contract_id, "glb-viewer.hardware-profile")
        self.assertEqual(m.contract_version, "1.0.0")
        self.assertEqual(m.provider, "NativeRelay")
        self.assertEqual(m.schema_version, 1)
        # collected_at must be parseable UTC ISO-8601
        datetime.fromisoformat(m.collected_at.replace("Z", "+00:00"))

    def test_cpu_validation(self):
        cpu = CpuInfo(vendor=CpuVendor.INTEL, brand="Intel Core i7", logical_count=8)
        self.assertEqual(cpu.logical_count, 8)
        with self.assertRaises(ValueError):
            CpuInfo(vendor=CpuVendor.INTEL, brand="x", logical_count=-1)
        with self.assertRaises(ValueError):
            CpuInfo(vendor=CpuVendor.INTEL, brand="x", logical_count=1, physical_count=0)
        with self.assertRaises(ValueError):
            CpuInfo(vendor=CpuVendor.INTEL, brand="x", logical_count=1, max_frequency_mhz=-1)

    def test_memory_validation(self):
        mem = MemoryInfo(total_bytes=16_000_000_000, available_bytes=8_000_000_000, usage_percent=50.0)
        self.assertEqual(mem.usage_percent, 50.0)
        with self.assertRaises(ValueError):
            MemoryInfo(total_bytes=-1)
        with self.assertRaises(ValueError):
            MemoryInfo(total_bytes=0, usage_percent=101.0)

    def test_battery_validation(self):
        bat = BatteryInfo(present=True, charging=True, charge_percent=78.0)
        self.assertTrue(bat.present)
        with self.assertRaises(ValueError):
            BatteryInfo(present=True, charge_percent=101.0)
        with self.assertRaises(ValueError):
            BatteryInfo(present=True, energy_full_mwh=-1)

    def test_gpu_validation(self):
        gpu = GpuDevice(vendor=GpuVendor.NVIDIA, model="RTX 3070", vram_bytes=8_000_000_000)
        self.assertEqual(gpu.vendor, GpuVendor.NVIDIA)
        with self.assertRaises(ValueError):
            GpuDevice(vendor=GpuVendor.NVIDIA, model="", vram_bytes=-1)

    def test_storage_validation(self):
        stor = StorageDevice(device_id="nvme0n1", type=StorageType.NVME, size_bytes=500_000_000_000)
        self.assertEqual(stor.type, StorageType.NVME)
        with self.assertRaises(ValueError):
            StorageDevice(device_id="", type=StorageType.NVME, size_bytes=0)
        with self.assertRaises(ValueError):
            StorageDevice(device_id="x", type=StorageType.NVME, size_bytes=-1)

    def test_display_validation(self):
        disp = DisplayInfo(id="eDP-1", type=DisplayType.BUILTIN, width_px=1920, height_px=1080)
        self.assertEqual(disp.width_px, 1920)
        with self.assertRaises(ValueError):
            DisplayInfo(id="", type=DisplayType.BUILTIN, width_px=0, height_px=0)
        with self.assertRaises(ValueError):
            DisplayInfo(id="x", type=DisplayType.BUILTIN, width_px=1920, height_px=1080, refresh_rate_hz=0)

    def test_thermal_validation(self):
        zone = ThermalZone(id="thermal-0", temperature_celsius=45.5, critical_celsius=100.0)
        self.assertEqual(zone.temperature_celsius, 45.5)
        with self.assertRaises(ValueError):
            ThermalZone(id="", temperature_celsius=None)

    def test_power_validation(self):
        power = PowerInfo(source=PowerSource.AC, ac_present=True)
        self.assertEqual(power.source, PowerSource.AC)
        with self.assertRaises(ValueError):
            PowerInfo(source=PowerSource.AC, ac_present="yes")

    def test_os_validation(self):
        os_info = OsInfo(name="Windows", version="11", architecture="x86_64")
        self.assertEqual(os_info.name, "Windows")
        with self.assertRaises(ValueError):
            OsInfo(name="", version="11", architecture="x86_64")


class SerializationTests(unittest.TestCase):
    def test_hardware_profile_round_trip(self):
        profile = HardwareProfile(
            metadata=CollectionMetadata(platform="windows"),
            os=OsInfo(name="Windows", version="11", architecture="x86_64"),
            cpu=CpuInfo(vendor=CpuVendor.INTEL, brand="Intel Core i7", logical_count=8),
            memory=MemoryInfo(total_bytes=16_000_000_000),
            gpus=(GpuDevice(vendor=GpuVendor.NVIDIA, model="RTX 3070"),),
            power=PowerInfo(source=PowerSource.AC, ac_present=True),
            thermal=(ThermalZone(id="t0", temperature_celsius=45.0),),
            storage=(StorageDevice(device_id="nvme0", type=StorageType.NVME, size_bytes=500_000_000_000),),
            displays=(DisplayInfo(id="d1", type=DisplayType.BUILTIN, width_px=1920, height_px=1080),),
        )
        data = json.loads(profile.to_json())
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["os"]["name"], "Windows")
        self.assertEqual(data["cpu"]["vendor"], "intel")
        self.assertEqual(data["gpus"][0]["vendor"], "nvidia")
        self.assertEqual(data["power"]["source"], "ac")
        self.assertEqual(data["thermal"][0]["id"], "t0")
        self.assertEqual(data["storage"][0]["type"], "nvme")
        self.assertEqual(data["displays"][0]["type"], "builtin")

    def test_resource_snapshot_round_trip(self):
        snap = ResourceSnapshot(
            cpu_usage_percent=23.5,
            memory_usage_bytes=8_000_000_000,
            memory_usage_percent=50.0,
            battery_charge_percent=78.0,
            battery_charging=False,
        )
        data = json.loads(snap.to_json())
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["cpu_usage_percent"], 23.5)
        self.assertEqual(data["battery_charging"], False)
        self.assertEqual(data["availability"], "available")

    def test_provider_info_round_trip(self):
        info = ProviderInfo()
        data = json.loads(info.to_json())
        self.assertEqual(data["name"], "NativeRelay")
        self.assertEqual(data["contract_id"], "glb-viewer.hardware-profile")
        self.assertEqual(data["contract_version"], "1.0.0")
        self.assertTrue(data["local_only"])
        self.assertFalse(data["requires_admin"])
        self.assertIn("get_hardware_profile", data["operations"])
        self.assertIn("get_resource_snapshot", data["operations"])
        self.assertIn("get_provider_info", data["operations"])

    def test_nullability_and_empty_arrays(self):
        profile = HardwareProfile(
            metadata=CollectionMetadata(platform="unknown"),
            os=OsInfo(name="unknown", version="unknown", architecture="unknown", availability=Availability.UNSUPPORTED),
            cpu=CpuInfo(vendor=CpuVendor.UNKNOWN, brand="unknown", logical_count=0, availability=Availability.UNSUPPORTED),
            memory=MemoryInfo(total_bytes=0, availability=Availability.UNSUPPORTED),
            gpus=(),
            power=None,
            thermal=(),
            storage=(),
            displays=(),
        )
        data = json.loads(profile.to_json())
        self.assertEqual(data["gpus"], [])
        self.assertEqual(data["thermal"], [])
        self.assertEqual(data["storage"], [])
        self.assertEqual(data["displays"], [])
        self.assertIsNone(data["power"])


class ContractOperationTests(unittest.TestCase):
    def test_get_provider_info(self):
        contract = HardwareContract()
        info = contract.get_provider_info()
        self.assertEqual(info.name, "NativeRelay")
        self.assertEqual(info.contract_id, "glb-viewer.hardware-profile")
        self.assertEqual(info.contract_version, "1.0.0")
        self.assertTrue(info.local_only)
        self.assertFalse(info.requires_admin)

    def test_unknown_platform_returns_unsupported(self):
        contract = HardwareContract()
        profile = contract.get_hardware_profile(platform="unknown")
        self.assertEqual(profile.os.availability, Availability.UNSUPPORTED)
        self.assertEqual(profile.cpu.availability, Availability.UNSUPPORTED)
        self.assertEqual(profile.memory.availability, Availability.UNSUPPORTED)
        self.assertEqual(profile.gpus, ())
        self.assertEqual(profile.storage, ())
        self.assertEqual(profile.displays, ())

    def test_unknown_platform_snapshot_unsupported(self):
        contract = HardwareContract()
        snap = contract.get_resource_snapshot(platform="unknown")
        self.assertEqual(snap.availability, Availability.UNSUPPORTED)

    def test_register_adapter(self):
        class FakeAdapter(HardwareAdapter):
            platform = "fake"
            def get_hardware_profile(self):
                from nativerelay.hardware.models import CollectionMetadata
                return HardwareProfile(
                    metadata=CollectionMetadata(platform="fake"),
                    os=OsInfo(name="FakeOS", version="1", architecture="fake"),
                    cpu=CpuInfo(vendor=CpuVendor.UNKNOWN, brand="fake", logical_count=1),
                    memory=MemoryInfo(total_bytes=0),
                )
            def get_resource_snapshot(self):
                return ResourceSnapshot()

        contract = HardwareContract()
        contract.register_adapter("fake", FakeAdapter())
        profile = contract.get_hardware_profile(platform="fake")
        self.assertEqual(profile.os.name, "FakeOS")
        self.assertEqual(profile.os.availability, Availability.AVAILABLE)

    def test_register_adapter_rejects_empty_platform(self):
        contract = HardwareContract()
        with self.assertRaises(ValueError):
            contract.register_adapter("", HardwareAdapter())


class FixtureTests(unittest.TestCase):
    FIXTURE_DIR = "tests/fixtures"

    def _load(self, name):
        import os
        path = os.path.join(self.FIXTURE_DIR, name)
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def test_windows_fixture_valid(self):
        data = self._load("hardware_profile_windows_available.json")
        self.assertEqual(data["metadata"]["contract_id"], "glb-viewer.hardware-profile")
        self.assertEqual(data["metadata"]["contract_version"], "1.0.0")
        self.assertEqual(data["os"]["availability"], "available")
        self.assertEqual(data["cpu"]["vendor"], "intel")
        self.assertEqual(data["power"]["source"], "ac")
        self.assertEqual(len(data["gpus"]), 1)
        self.assertEqual(len(data["displays"]), 1)

    def test_linux_fixture_valid(self):
        data = self._load("hardware_profile_linux_available.json")
        self.assertEqual(data["os"]["availability"], "available")
        self.assertEqual(data["cpu"]["vendor"], "amd")
        self.assertEqual(data["power"]["source"], "battery")
        self.assertTrue(data["power"]["battery"]["present"])

    def test_darwin_fixture_valid(self):
        data = self._load("hardware_profile_darwin_available.json")
        self.assertEqual(data["os"]["architecture"], "arm64")
        self.assertEqual(data["cpu"]["vendor"], "arm")
        self.assertEqual(data["power"]["source"], "battery")

    def test_partial_collection_fixture(self):
        data = self._load("hardware_profile_partial_collection.json")
        self.assertEqual(data["os"]["availability"], "unavailable")
        self.assertEqual(data["cpu"]["availability"], "unavailable")
        self.assertEqual(data["memory"]["availability"], "unavailable")
        self.assertEqual(data["gpus"], [])
        self.assertEqual(data["storage"], [])
        self.assertEqual(data["displays"], [])

    def test_permission_denied_fixture(self):
        data = self._load("hardware_profile_permission_denied.json")
        self.assertEqual(data["os"]["availability"], "permission_denied")
        self.assertEqual(data["cpu"]["availability"], "permission_denied")
        self.assertEqual(data["power"]["availability"], "permission_denied")

    def test_unsupported_platform_fixture(self):
        data = self._load("hardware_profile_unsupported_platform.json")
        self.assertEqual(data["os"]["availability"], "unsupported")
        self.assertEqual(data["cpu"]["availability"], "unsupported")
        self.assertEqual(data["cpu"]["logical_count"], 0)

    def test_resource_snapshot_fixture(self):
        data = self._load("resource_snapshot_example.json")
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["cpu_usage_percent"], 23.5)
        self.assertEqual(data["battery_charge_percent"], 78.0)
        self.assertEqual(data["battery_charging"], False)
        self.assertEqual(data["availability"], "available")


class EnumValueTests(unittest.TestCase):
    def test_availability_enum_values(self):
        self.assertEqual(Availability.AVAILABLE.value, "available")
        self.assertEqual(Availability.UNAVAILABLE.value, "unavailable")
        self.assertEqual(Availability.UNSUPPORTED.value, "unsupported")
        self.assertEqual(Availability.PERMISSION_DENIED.value, "permission_denied")
        self.assertEqual(Availability.NOT_APPLICABLE.value, "not_applicable")
        self.assertEqual(Availability.ESTIMATED.value, "estimated")
        self.assertEqual(Availability.ERROR.value, "error")

    def test_cpu_vendor_enum_values(self):
        self.assertEqual(CpuVendor.INTEL.value, "intel")
        self.assertEqual(CpuVendor.AMD.value, "amd")
        self.assertEqual(CpuVendor.ARM.value, "arm")
        self.assertEqual(CpuVendor.APPLE.value, "apple")
        self.assertEqual(CpuVendor.QUALCOMM.value, "qualcomm")
        self.assertEqual(CpuVendor.NVIDIA.value, "nvidia")
        self.assertEqual(CpuVendor.OTHER.value, "other")
        self.assertEqual(CpuVendor.UNKNOWN.value, "unknown")

    def test_gpu_vendor_enum_values(self):
        self.assertEqual(GpuVendor.NVIDIA.value, "nvidia")
        self.assertEqual(GpuVendor.AMD.value, "amd")
        self.assertEqual(GpuVendor.INTEL.value, "intel")
        self.assertEqual(GpuVendor.APPLE.value, "apple")
        self.assertEqual(GpuVendor.QUALCOMM.value, "qualcomm")
        self.assertEqual(GpuVendor.OTHER.value, "other")
        self.assertEqual(GpuVendor.UNKNOWN.value, "unknown")

    def test_storage_type_enum_values(self):
        self.assertEqual(StorageType.HDD.value, "hdd")
        self.assertEqual(StorageType.SSD.value, "ssd")
        self.assertEqual(StorageType.NVME.value, "nvme")
        self.assertEqual(StorageType.EMMC.value, "emmc")
        self.assertEqual(StorageType.USB.value, "usb")
        self.assertEqual(StorageType.OTHER.value, "other")
        self.assertEqual(StorageType.UNKNOWN.value, "unknown")

    def test_display_type_enum_values(self):
        self.assertEqual(DisplayType.BUILTIN.value, "builtin")
        self.assertEqual(DisplayType.EXTERNAL.value, "external")
        self.assertEqual(DisplayType.PROJECTOR.value, "projector")
        self.assertEqual(DisplayType.VIRTUAL.value, "virtual")
        self.assertEqual(DisplayType.UNKNOWN.value, "unknown")

    def test_power_source_enum_values(self):
        self.assertEqual(PowerSource.AC.value, "ac")
        self.assertEqual(PowerSource.BATTERY.value, "battery")
        self.assertEqual(PowerSource.UNKNOWN.value, "unknown")


if __name__ == "__main__":
    unittest.main()
