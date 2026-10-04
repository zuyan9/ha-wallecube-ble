"""WalleCube BLE select"""

from homeassistant.components.select import SelectEntity, SelectEntityDescription
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
        WalleCubeSelect(device, control)
        for control in get_controls(device, controls.select)
    )


class WalleCubeSelect(WalleCubeEntity, SelectEntity):
    """Setting with a fixed set of options"""

    def __init__(self, device: DeviceBase, control: controls.select) -> None:
        super().__init__(
            device,
            SelectEntityDescription(
                key=control.key,
                options=control.options_str,
                entity_category=EntityCategory.CONFIG,
                entity_registry_enabled_default=control.enabled,
            ),
        )
        self._set_value = control.set_value_func
        self._restart_required = control.restart_required

    @property
    def current_option(self) -> str | None:
        return None if (value := self._value) is None else controls.option_name(value)

    async def async_select_option(self, option: str) -> None:
        await self._change_setting(self._set_value, option)
