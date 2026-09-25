import dataclasses
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, runtime_checkable

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
    _attr_has_entity_name = True
    # state is pushed from device callbacks, polling would only bypass the update
    # period throttle
    _attr_should_poll = False

    def __init__(self, device: DeviceBase):
        self._device = device
        self._update_callbacks: list[tuple[str, Callable[[Any], None]]] = []
        self._initial_states: list[Callable[[], None]] = []

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
            sw_version=_version(getattr(self._device, "firmware_version", None)),
            hw_version=_version(getattr(self._device, "hardware_version", None)),
        )

    @property
    def available(self) -> bool:
        """Return True if device is connected"""
        return self._device.is_connected

    class SkipWrite:
        """Sentinel value for skipping write in update callback"""

    def _register_update_callback(
        self,
        entity_attr: str,
        prop_name: str | None,
        get_state: Callable[[Any], Any] = lambda x: x,
        default_state: Any = None,
    ):
        if prop_name is None or not hasattr(self._device, prop_name):
            return

        def load_current_state():
            if (state := getattr(self._device, prop_name, None)) is not None:
                state = get_state(state)
                if state is not WalleCubeEntity.SkipWrite:
                    setattr(self, entity_attr, state)
            else:
                setattr(self, entity_attr, default_state)

        @callback
        def state_updated(state: Any):
            if (state := get_state(state)) is WalleCubeEntity.SkipWrite:
                return
            setattr(self, entity_attr, state)
            self.async_write_ha_state()

        load_current_state()
        self._initial_states.append(load_current_state)
        self._update_callbacks.append((prop_name, state_updated))

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
        for prop, state_callback in self._update_callbacks:
            self._device.register_state_update_callback(state_callback, prop)
        # values published between construction and subscription, e.g. by the
        # settings read after connecting, would otherwise be missed until they change
        for load_current_state in self._initial_states:
            load_current_state()
        await super().async_added_to_hass()

    async def async_will_remove_from_hass(self) -> None:
        for prop, state_callback in self._update_callbacks:
            self._device.remove_state_update_callback(state_callback, prop)
        await super().async_will_remove_from_hass()


def _version(value: int | None) -> str | None:
    return None if value is None else str(value)


@runtime_checkable
class IndexableDescription(Protocol):
    """Entity description that supports indexed expansion via `{n}` in keys"""

    key: str
    indexed_range: range | None
    translation_placeholders: Mapping[str, str] | None


def resolve_entity_description_keys[D: EntityDescription](
    descriptions: dict[str, D],
) -> dict[str, D]:
    """
    Fill in description keys from dict keys and expand indexed descriptions

    Keys containing `{n}` whose description has `indexed_range` set are expanded over
    that range; `{n}` in translation placeholder values is replaced as well.
    """
    result: dict[str, D] = {}
    for key, description in descriptions.items():
        if not (
            "{n}" in key
            and isinstance(description, IndexableDescription)
            and description.indexed_range is not None
        ):
            result[key] = (
                dataclasses.replace(description, key=key)
                if not description.key
                else description
            )
            continue

        for i in description.indexed_range:
            actual_key = key.replace("{n}", str(i))
            placeholders = description.translation_placeholders
            if placeholders:
                placeholders = {k: v.format(n=i) for k, v in placeholders.items()}
            result[actual_key] = dataclasses.replace(
                description,
                key=actual_key,
                indexed_range=None,
                translation_placeholders=placeholders,
            )

    return result
