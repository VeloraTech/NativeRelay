"""Base class for platform hardware adapters."""
from __future__ import annotations

from ..models import HardwareProfile, ResourceSnapshot


class HardwareAdapter:
    """Base adapter for collecting hardware information on a specific platform."""

    platform: str

    def get_hardware_profile(self) -> HardwareProfile:
        raise NotImplementedError

    def get_resource_snapshot(self) -> ResourceSnapshot:
        raise NotImplementedError
