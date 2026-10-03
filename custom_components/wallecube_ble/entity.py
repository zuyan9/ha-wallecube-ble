from collections.abc import Awaitable, Callable
from typing import Any

from bleak.exc import BleakError
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription

from .const import DOMAIN, MANUFACTURER
from .wclib import DeviceBase
from .wclib.exceptions import (
    PacketParseError,
    SessionKeyError,
    SettingNotConfirmed,
    SettingUnavailable,
    UnsupportedBluetoothProtocol,
)

_SETTING_ERRORS = (
    BleakError,
    ConnectionError,
    TimeoutError,
    PacketParseError,
    SessionKeyError,
    SettingNotConfirmed,
    SettingUnavailable,
    UnsupportedBluetoothProtocol,
)


class WalleCubeEntity(Entity):
    """Entity that shows the device field named by its description's key"""

    _attr_has_entity_name = True
    # state is pushed from device callbacks, polling would only bypass the update
    # period throttle
    _attr_should_poll = False
    # sensors follow the update period, the other entities are written at once
    _throttled = False

    def __init__(self, device: DeviceBase, description: EntityDescription) -> None:
        self._device = device
        self.entity_description = description
        self._attr_unique_id = f"wc_{device.identifier}_{description.key}"
        self._attr_translation_key = description.translation_key or description.key

    @property
    def device_info(self):
        """Return information to link this entity with the correct device"""
        return DeviceInfo(
            identifiers={(DOMAIN, self._device.address)},
            connections={(CONNECTION_BLUETOOTH, self._device.address)},
            name=self._device.name,
            manufacturer=MANUFACTURER,
            model=self._device.device,
            # versions of the front panel, the power board has its own sensors
            sw_version=firmware_version(
                getattr(self._device, "firmware_version", None)
            ),
            hw_version=_version(getattr(self._device, "hardware_version", None)),
        )

    @property
    def available(self) -> bool:
        """Return True if the device is connected and the value is current"""
        if not self._device.is_connected:
            return False
        return (
            self._device.data_current
            or self.entity_description.key not in self._device.data_fields
        )

    @property
    def _value(self) -> Any:
        """Current value of the device field"""
        return getattr(self._device, self.entity_description.key, None)

    async def _change_setting[*Ts](
        self, setter: Callable[[DeviceBase, *Ts], Awaitable[None]], *args: *Ts
    ) -> None:
        """Call a device setter, reporting communication failures to the user"""
        try:
            await setter(self._device, *args)
        except _SETTING_ERRORS as e:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="setting_failed",
                translation_placeholders={"error": str(e)},
            ) from e

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._device.subscribe(
                self.entity_description.key, self._on_update, throttled=self._throttled
            )
        )

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()


def firmware_version(value: int | None) -> str | None:
    """Firmware version as the vendor app shows it, 1.19 for the reported 19"""
    return None if value is None else f"1.{value:02d}"


def _version(value: int | None) -> str | None:
    return None if value is None else str(value)
