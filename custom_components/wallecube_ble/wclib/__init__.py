"""Library for the WalleCube BLE protocol"""

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from . import devices
from .devicebase import DeviceBase


def NewDevice(ble_dev: BLEDevice, adv_data: AdvertisementData) -> DeviceBase | None:
    """Return Device if ble dev fits the requirements otherwise None"""
    for item in devices.devices:
        if (device := getattr(item, "Device", None)) is not None and device.check(
            adv_data
        ):
            return item.Device(ble_dev, adv_data)
    return None


__all__ = [
    "DeviceBase",
    "NewDevice",
]
