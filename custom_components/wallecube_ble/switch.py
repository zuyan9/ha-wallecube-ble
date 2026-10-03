"""WalleCube BLE switch"""

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DeviceConfigEntry
from .entity import WalleCubeEntity
from .wclib import DeviceBase, controls, get_controls


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: DeviceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    device = config_entry.runtime_data
    async_add_entities(
        WalleCubeSwitch(device, control)
        for control in get_controls(device, controls.switch)
    )


class WalleCubeSwitch(WalleCubeEntity, SwitchEntity):
    """On/off setting"""

    def __init__(self, device: DeviceBase, control: controls.switch) -> None:
        super().__init__(
            device,
            SwitchEntityDescription(
                key=control.key,
                entity_category=EntityCategory.CONFIG,
                entity_registry_enabled_default=control.enabled,
            ),
        )
        self._enable = control.enable_func

    @property
    def is_on(self) -> bool | None:
        return self._value

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._change_setting(self._enable, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._change_setting(self._enable, False)
