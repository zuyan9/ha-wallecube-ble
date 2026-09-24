"""WalleCube BLE select"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DeviceConfigEntry
from .entity import WalleCubeEntity
from .wclib import DeviceBase, controls, get_controls


@dataclass(frozen=True, kw_only=True)
class WalleCubeSelectEntityDescription(SelectEntityDescription):
    set_value: Callable[[DeviceBase, str], Awaitable[None]]


def _describe(control: controls.select) -> WalleCubeSelectEntityDescription:
    return WalleCubeSelectEntityDescription(
        key=control.key,
        translation_key=control.translation_key or control.key,
        options=control.options_str,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=control.enabled,
        set_value=control.set_value_func,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: DeviceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add select entities for passed config_entry in HA"""
    device = config_entry.runtime_data

    entities = [
        WalleCubeSelect(device, _describe(control))
        for control in get_controls(device, controls.select)
    ]
    if entities:
        async_add_entities(entities)


class WalleCubeSelect(WalleCubeEntity, SelectEntity):
    """Setting with a fixed set of options"""

    entity_description: WalleCubeSelectEntityDescription

    def __init__(
        self, device: DeviceBase, description: WalleCubeSelectEntityDescription
    ):
        super().__init__(device)

        self._attr_unique_id = f"wc_{device.identifier}_{description.key}"
        self.entity_description = description
        self._register_update_callback(
            "_attr_current_option", description.key, _option_or_none
        )

    async def async_select_option(self, option: str) -> None:
        await self._change_setting(self.entity_description.set_value, option)


def _option_or_none(value: Any) -> str | None:
    return None if value is None else controls.option_name(value)
