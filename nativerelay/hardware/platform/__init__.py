"""Platform hardware adapters for the NativeRelay hardware contract.

Adapters are registered by platform id (windows, linux, darwin) and return
platform-neutral hardware models. Each adapter is lazy-imported only when its
platform is detected.
"""
from .base import HardwareAdapter

__all__ = ["HardwareAdapter"]
