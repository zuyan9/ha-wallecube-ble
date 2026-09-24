"""WalleCube BLE sensor"""

from dataclasses import dataclass, field
from typing import Any, Final, TypedDict, Unpack

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DeviceConfigEntry
from .entity import WalleCubeEntity, resolve_entity_description_keys
from .wclib import DeviceBase


@dataclass(frozen=True, kw_only=True)
class WalleCubeSensorEntityDescription(SensorEntityDescription):
    state_attribute_fields: list[str] = field(default_factory=list)
    indexed_range: range | None = None


class _SensorKwargs(TypedDict, total=False):
    translation_key: str
    translation_placeholders: dict[str, str]
    indexed_range: range
    entity_category: EntityCategory
    state_attribute_fields: list[str]


def battery(
    key: str = "", enabled: bool = True, **kwargs: Unpack[_SensorKwargs]
) -> WalleCubeSensorEntityDescription:
    return WalleCubeSensorEntityDescription(
        key=key,
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=enabled,
        **kwargs,
    )


def power(
    key: str = "",
    *,
    enabled: bool = True,
    precision: int | None = None,
    **kwargs: Unpack[_SensorKwargs],
) -> WalleCubeSensorEntityDescription:
    return WalleCubeSensorEntityDescription(
        key=key,
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=precision,
        entity_registry_enabled_default=enabled,
        **kwargs,
    )


def voltage(
    key: str = "",
    *,
    enabled: bool = True,
    precision: int | None = None,
    **kwargs: Unpack[_SensorKwargs],
) -> WalleCubeSensorEntityDescription:
    return WalleCubeSensorEntityDescription(
        key=key,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=precision,
        entity_registry_enabled_default=enabled,
        **kwargs,
    )


def current(
    key: str = "",
    *,
    enabled: bool = True,
    precision: int | None = None,
    **kwargs: Unpack[_SensorKwargs],
) -> WalleCubeSensorEntityDescription:
    return WalleCubeSensorEntityDescription(
        key=key,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=precision,
        entity_registry_enabled_default=enabled,
        **kwargs,
    )


def temperature(
    key: str = "", enabled: bool = True, **kwargs: Unpack[_SensorKwargs]
) -> WalleCubeSensorEntityDescription:
    return WalleCubeSensorEntityDescription(
        key=key,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=enabled,
        **kwargs,
    )


def duration(
    key: str = "", enabled: bool = True, **kwargs: Unpack[_SensorKwargs]
) -> WalleCubeSensorEntityDescription:
    return WalleCubeSensorEntityDescription(
        key=key,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=enabled,
        **kwargs,
    )


_SENSORS: Final[dict[str, SensorEntityDescription]] = {
    "battery_level": battery(),
    "battery_current": current(precision=2),
    "dc_input_voltage": voltage(precision=2),
    "dc_output_voltage": voltage(precision=2),
    "dc_output_current": current(precision=2),
    "output_power": power(precision=1),
    "temperature": temperature(),
    "remaining_time_discharging": duration(),
}

SENSOR_TYPES: Final[dict[str, SensorEntityDescription]] = (
    resolve_entity_description_keys(_SENSORS)
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: DeviceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add sensors for passed config_entry in HA"""
    device = config_entry.runtime_data

    new_sensors = [
        WalleCubeSensor(device, sensor)
        for sensor in SENSOR_TYPES
        if hasattr(device, sensor)
    ]

    if new_sensors:
        async_add_entities(new_sensors)


class WalleCubeSensor(WalleCubeEntity, SensorEntity):
    """Base representation of a sensor"""

    def __init__(self, device: DeviceBase, sensor: str):
        """Initialize the sensor"""
        super().__init__(device)

        self._sensor = sensor
        self._attr_unique_id = f"wc_{device.identifier}_{sensor}"
        self.entity_description = SENSOR_TYPES[sensor]
        if self.entity_description.translation_key is None:
            self._attr_translation_key = self.entity_description.key

        self._attribute_fields = (
            self.entity_description.state_attribute_fields
            if isinstance(self.entity_description, WalleCubeSensorEntityDescription)
            else []
        )

    @property
    def native_value(self):
        """Return the value of the sensor"""
        return getattr(self._device, self._sensor, None)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self._attribute_fields:
            return {}

        return {
            field_name: getattr(self._device, field_name)
            for field_name in self._attribute_fields
            if hasattr(self._device, field_name)
        }

    async def async_added_to_hass(self):
        """Run when this Entity has been added to HA"""
        await super().async_added_to_hass()
        self._device.register_callback(self.async_write_ha_state, self._sensor)

    async def async_will_remove_from_hass(self):
        """Entity being removed from hass"""
        await super().async_will_remove_from_hass()
        self._device.remove_callback(self.async_write_ha_state, self._sensor)
