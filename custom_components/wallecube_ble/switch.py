"""WalleCube BLE switch"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DeviceConfigEntry
from .entity import WalleCubeEntity
from .wclib import DeviceBase, controls, get_controls


@dataclass(frozen=True, kw_only=True)
class WalleCubeSwitchEntityDescription(SwitchEntityDescription):
    enable: Callable[[DeviceBase, bool], Awaitable[None]]


def _describe(control: controls.switch) -> WalleCubeSwitchEntityDescription:
    return WalleCubeSwitchEntityDescription(
        key=control.key,
        translation_key=control.translation_key or control.key,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=control.enabled,
        enable=control.enable_func,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: DeviceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add switch entities for passed config_entry in HA"""
    device = config_entry.runtime_data

    entities = [
        WalleCubeSwitch(device, _describe(control))
        for control in get_controls(device, controls.switch)
    ]
    if entities:
        async_add_entities(entities)


class WalleCubeSwitch(WalleCubeEntity, SwitchEntity):
    """On/off setting"""

    entity_description: WalleCubeSwitchEntityDescription

    def __init__(
        self, device: DeviceBase, description: WalleCubeSwitchEntityDescription
    ):
        super().__init__(device)

        self._attr_unique_id = f"wc_{device.identifier}_{description.key}"
        self.entity_description = description
        self._register_update_callback("_attr_is_on", description.key)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._change_setting(self.entity_description.enable, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._change_setting(self.entity_description.enable, False)
