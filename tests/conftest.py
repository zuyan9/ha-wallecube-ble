"""Fixtures shared by the library and the Home Assistant tests"""

import sys
import warnings
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

try:
    # Home Assistant's dependencies warn on import, as in pytest.importorskip
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import custom_components.wallecube_ble
except Exception:  # noqa: BLE001
    # without a working Home Assistant the integration's __init__.py cannot run, so
    # the packages are stubbed and the library is imported on its own
    custom_components = ModuleType("custom_components")
    custom_components.__path__ = []
    wallecube_ble = ModuleType("custom_components.wallecube_ble")
    wallecube_ble.__path__ = [
        str(Path(__file__).parents[1] / "custom_components" / "wallecube_ble")
    ]
    custom_components.wallecube_ble = wallecube_ble  # type: ignore[attr-defined]
    sys.modules["custom_components"] = custom_components
    sys.modules["custom_components.wallecube_ble"] = wallecube_ble

from custom_components.wallecube_ble.wclib.devices.w150 import Device

from .fakes import advertisement, ble_device, ups_client


@pytest.fixture
def device() -> Device:
    return Device(ble_device(), advertisement())


@pytest.fixture
def client():
    return ups_client()


@pytest.fixture
def establish(mocker, client):
    """Connections get `client` unless a test sets another result"""
    return mocker.patch(
        "custom_components.wallecube_ble.wclib.connection.establish_connection",
        new=AsyncMock(return_value=client),
    )
