"""Pytest configuration for testing wclib without Home Assistant dependencies."""

import sys
from pathlib import Path
from types import ModuleType

# Create minimal stub modules for custom_components and wallecube_ble - This prevents
# their __init__.py files from being executed
custom_components = ModuleType("custom_components")
custom_components.__path__ = []
sys.modules["custom_components"] = custom_components

wallecube_ble = ModuleType("custom_components.wallecube_ble")
wallecube_ble.__path__ = [
    str(Path(__file__).parents[2] / "custom_components" / "wallecube_ble")
]
sys.modules["custom_components.wallecube_ble"] = wallecube_ble

setattr(custom_components, "wallecube_ble", wallecube_ble)
