"""WalleCube BLE binary sensor"""

from typing import Final

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DeviceConfigEntry
from .entity import WalleCubeEntity


def _binary_sensor(
    key: str,
    device_class: BinarySensorDeviceClass | None = None,
    *,
    diagnostic: bool = False,
) -> BinarySensorEntityDescription:
    return BinarySensorEntityDescription(
        key=key,
        device_class=device_class,
        entity_category=EntityCategory.DIAGNOSTIC if diagnostic else None,
    )


_PROBLEM = BinarySensorDeviceClass.PROBLEM

BINARY_SENSOR_TYPES: Final[dict[str, BinarySensorEntityDescription]] = {
    description.key: description
    for description in (
        _binary_sensor("input_power_ok", BinarySensorDeviceClass.POWER),
        _binary_sensor("charging", BinarySensorDeviceClass.BATTERY_CHARGING),
        _binary_sensor("discharging"),
        # primary, they decide when to shut equipment down
        _binary_sensor("overload", _PROBLEM),
        _binary_sensor("shutdown_imminent", _PROBLEM),
        _binary_sensor("battery_fault", _PROBLEM, diagnostic=True),
        _binary_sensor(
            "over_temperature", BinarySensorDeviceClass.HEAT, diagnostic=True
        ),
        _binary_sensor(
            "under_temperature", BinarySensorDeviceClass.COLD, diagnostic=True
        ),
        _binary_sensor("input_over_voltage", _PROBLEM, diagnostic=True),
        _binary_sensor("output_over_current", _PROBLEM, diagnostic=True),
        _binary_sensor(
            "wifi_connected", BinarySensorDeviceClass.CONNECTIVITY, diagnostic=True
        ),
    )
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: DeviceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    device = config_entry.runtime_data
    async_add_entities(
        WalleCubeBinarySensor(device, description)
        for description in BINARY_SENSOR_TYPES.values()
        if hasattr(device, description.key)
    )


class WalleCubeBinarySensor(WalleCubeEntity, BinarySensorEntity):
    """Binary sensor backed by a boolean device field"""

    @property
    def is_on(self) -> bool | None:
        return self._value
