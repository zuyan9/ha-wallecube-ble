"""WalleCube BLE number"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DeviceConfigEntry
from .entity import WalleCubeEntity
from .wclib import DeviceBase, controls, get_controls

_UNITS: dict[type[controls.NumberType], tuple[NumberDeviceClass, str]] = {
    controls.duration: (NumberDeviceClass.DURATION, UnitOfTime.SECONDS),
    controls.current: (NumberDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE),
    controls.current_ma: (
        NumberDeviceClass.CURRENT,
        UnitOfElectricCurrent.MILLIAMPERE,
    ),
    controls.voltage: (NumberDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT),
}


@dataclass(frozen=True, kw_only=True)
class WalleCubeNumberEntityDescription(NumberEntityDescription):
    set_value: Callable[[DeviceBase, float], Awaitable[None]]


def _describe(control: controls.NumberType) -> WalleCubeNumberEntityDescription:
    device_class, unit = _UNITS[type(control)]
    return WalleCubeNumberEntityDescription(
        key=control.key,
        translation_key=control.translation_key or control.key,
        device_class=device_class,
        native_unit_of_measurement=unit,
        native_min_value=control.min,
        native_max_value=control.max,
        native_step=control.step,
        # values are typed in like in the vendor app, a slider over hours of
        # seconds is not usable
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=control.enabled,
        set_value=control.set_value_func,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: DeviceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add number entities for passed config_entry in HA"""
    device = config_entry.runtime_data

    entities = [
        WalleCubeNumber(device, _describe(control))
        for control in get_controls(device, controls.NumberType)
    ]
    if entities:
        async_add_entities(entities)


class WalleCubeNumber(WalleCubeEntity, NumberEntity):
    """Numeric setting"""

    entity_description: WalleCubeNumberEntityDescription

    def __init__(
        self, device: DeviceBase, description: WalleCubeNumberEntityDescription
    ):
        super().__init__(device)

        self._attr_unique_id = f"wc_{device.identifier}_{description.key}"
        self.entity_description = description
        self._register_update_callback("_attr_native_value", description.key)

    async def async_set_native_value(self, value: float) -> None:
        await self._change_setting(self.entity_description.set_value, value)
