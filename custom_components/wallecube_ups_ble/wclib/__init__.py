"""Library for the WalleCube BLE protocol"""

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from . import controls, devices
from .devicebase import DeviceBase
from .props import UpdatableProps


def NewDevice(ble_dev: BLEDevice, adv_data: AdvertisementData) -> DeviceBase | None:
    """Return Device if ble dev fits the requirements otherwise None"""
    for module in devices.devices:
        if module.Device.check(adv_data):
            return module.Device(ble_dev, adv_data)
    return None


def get_controls[C: controls.ControlType](
    device: DeviceBase, control_type: type[C]
) -> list[C]:
    """Return the controls of the given type the device declares and supports"""
    if not isinstance(device, UpdatableProps):
        return []
    return [
        control
        for control in device.get_controls(control_type)
        if control.characteristic is None
        or device.has_characteristic(control.characteristic)
    ]


__all__ = [
    "DeviceBase",
    "NewDevice",
    "controls",
    "get_controls",
]
