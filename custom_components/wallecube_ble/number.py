"""WalleCube BLE number"""

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import (
    PERCENTAGE,
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

_UNITS: dict[type[controls.NumberType], tuple[NumberDeviceClass | None, str]] = {
    controls.duration: (NumberDeviceClass.DURATION, UnitOfTime.SECONDS),
    controls.current: (NumberDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE),
    controls.current_ma: (
        NumberDeviceClass.CURRENT,
        UnitOfElectricCurrent.MILLIAMPERE,
    ),
    controls.voltage: (NumberDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT),
    controls.percentage: (None, PERCENTAGE),
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: DeviceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    device = config_entry.runtime_data
    async_add_entities(
        WalleCubeNumber(device, control)
        for control in get_controls(device, controls.NumberType)
    )


class WalleCubeNumber(WalleCubeEntity, NumberEntity):
    """Numeric setting"""

    def __init__(self, device: DeviceBase, control: controls.NumberType) -> None:
        device_class, unit = _UNITS[type(control)]
        super().__init__(
            device,
            NumberEntityDescription(
                key=control.key,
                device_class=device_class,
                native_unit_of_measurement=unit,
                native_min_value=control.min,
                native_max_value=control.max,
                native_step=control.step,
                # values are typed in like in the vendor app, a slider over hours of
                # seconds is not usable; percentages fit a slider
                mode=(
                    NumberMode.SLIDER
                    if isinstance(control, controls.percentage)
                    else NumberMode.BOX
                ),
                entity_category=EntityCategory.CONFIG,
                entity_registry_enabled_default=control.enabled,
            ),
        )
        self._set_value = control.set_value_func
        self._restart_required = control.restart_required

    @property
    def native_value(self) -> float | None:
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        await self._change_setting(self._set_value, value)
