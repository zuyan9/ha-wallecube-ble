"""WalleCube BLE sensor"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DeviceConfigEntry
from .entity import WalleCubeEntity, firmware_version


@dataclass(frozen=True, kw_only=True)
class WalleCubeSensorEntityDescription(SensorEntityDescription):
    # turns the device value into the state
    value_fn: Callable[[Any], Any] | None = None


def _measurement(
    key: str,
    device_class: SensorDeviceClass | None,
    unit: str,
    *,
    precision: int | None = None,
    **kwargs: Any,
) -> SensorEntityDescription:
    return SensorEntityDescription(
        key=key,
        device_class=device_class,
        native_unit_of_measurement=unit,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=precision,
        **kwargs,
    )


def _diagnostic(
    key: str,
    *,
    enabled: bool = True,
    value_fn: Callable[[Any], Any] | None = None,
) -> SensorEntityDescription:
    """Text value such as a version or a network name"""
    return WalleCubeSensorEntityDescription(
        key=key,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=enabled,
        value_fn=value_fn,
    )


_VOLTS = SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT
_AMPERES = SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE

SENSOR_TYPES: Final[dict[str, SensorEntityDescription]] = {
    description.key: description
    for description in (
        _measurement("battery_level", SensorDeviceClass.BATTERY, PERCENTAGE),
        _measurement("battery_voltage", *_VOLTS, precision=2),
        *(
            _measurement(
                f"cell_voltage_{n}",
                *_VOLTS,
                precision=3,
                translation_key="cell_voltage",
                translation_placeholders={"n": str(n)},
            )
            for n in range(1, 5)
        ),
        _measurement(
            "cell_voltage_difference",
            SensorDeviceClass.VOLTAGE,
            UnitOfElectricPotential.MILLIVOLT,
            precision=0,
        ),
        _measurement("battery_current", *_AMPERES, precision=2),
        _measurement(
            "battery_power", SensorDeviceClass.POWER, UnitOfPower.WATT, precision=1
        ),
        _measurement("dc_input_voltage", *_VOLTS, precision=2),
        _measurement("dc_input_current", *_AMPERES, precision=2),
        _measurement("dc_output_voltage", *_VOLTS, precision=2),
        _measurement("dc_output_current", *_AMPERES, precision=2),
        _measurement(
            "output_power", SensorDeviceClass.POWER, UnitOfPower.WATT, precision=1
        ),
        _measurement(
            "temperature", SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS
        ),
        _measurement(
            "remaining_time_discharging", SensorDeviceClass.DURATION, UnitOfTime.MINUTES
        ),
        SensorEntityDescription(
            key="energy_total",
            device_class=SensorDeviceClass.ENERGY,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            state_class=SensorStateClass.TOTAL_INCREASING,
            suggested_display_precision=3,
        ),
        # estimated from the cycle count like the vendor cloud does
        _measurement("battery_health", None, PERCENTAGE, precision=0),
        SensorEntityDescription(
            key="battery_cycles", state_class=SensorStateClass.TOTAL_INCREASING
        ),
        _diagnostic("power_board_firmware_version", value_fn=firmware_version),
        _diagnostic("power_board_hardware_version", enabled=False),
        _measurement(
            "wifi_rssi",
            SensorDeviceClass.SIGNAL_STRENGTH,
            SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
        _diagnostic("wifi_ssid"),
        _diagnostic("wifi_ip_address"),
    )
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: DeviceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    device = config_entry.runtime_data
    async_add_entities(
        WalleCubeSensor(device, description)
        for description in SENSOR_TYPES.values()
        if hasattr(device, description.key)
    )


class WalleCubeSensor(WalleCubeEntity, SensorEntity):
    """Measured or reported value, throttled by the update period"""

    _throttled = True

    @property
    def native_value(self) -> Any:
        description = self.entity_description
        if (
            isinstance(description, WalleCubeSensorEntityDescription)
            and description.value_fn is not None
        ):
            return description.value_fn(self._value)
        return self._value
