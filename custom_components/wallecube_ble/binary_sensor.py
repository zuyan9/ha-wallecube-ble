"""WalleCube BLE binary sensor"""

from dataclasses import dataclass
from typing import Final, TypedDict, Unpack

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DeviceConfigEntry
from .entity import WalleCubeEntity, resolve_entity_description_keys
from .wclib import DeviceBase


@dataclass(frozen=True, kw_only=True)
class WalleCubeBinarySensorEntityDescription(BinarySensorEntityDescription):
    indexed_range: range | None = None


class _BinarySensorKwargs(TypedDict, total=False):
    translation_key: str
    translation_placeholders: dict[str, str]
    indexed_range: range
    entity_category: EntityCategory


def _make_desc(
    device_class: BinarySensorDeviceClass | None,
    key: str = "",
    *,
    enabled: bool = True,
    **kwargs: Unpack[_BinarySensorKwargs],
) -> WalleCubeBinarySensorEntityDescription:
    return WalleCubeBinarySensorEntityDescription(
        key=key,
        device_class=device_class,
        entity_registry_enabled_default=enabled,
        **kwargs,
    )


def power(
    key: str = "", *, enabled: bool = True, **kwargs: Unpack[_BinarySensorKwargs]
) -> WalleCubeBinarySensorEntityDescription:
    return _make_desc(BinarySensorDeviceClass.POWER, key, enabled=enabled, **kwargs)


def battery_charging(
    key: str = "", *, enabled: bool = True, **kwargs: Unpack[_BinarySensorKwargs]
) -> WalleCubeBinarySensorEntityDescription:
    return _make_desc(
        BinarySensorDeviceClass.BATTERY_CHARGING, key, enabled=enabled, **kwargs
    )


def problem(
    key: str = "", *, enabled: bool = True, **kwargs: Unpack[_BinarySensorKwargs]
) -> WalleCubeBinarySensorEntityDescription:
    return _make_desc(BinarySensorDeviceClass.PROBLEM, key, enabled=enabled, **kwargs)


_BINARY_SENSORS: Final[dict[str, BinarySensorEntityDescription]] = {
    "input_power_ok": power(),
    "charging": battery_charging(),
    "discharging": _make_desc(None),
    "overload": problem(entity_category=EntityCategory.DIAGNOSTIC),
    "shutdown_imminent": problem(entity_category=EntityCategory.DIAGNOSTIC),
    "wifi_connected": _make_desc(
        BinarySensorDeviceClass.CONNECTIVITY, entity_category=EntityCategory.DIAGNOSTIC
    ),
}

BINARY_SENSOR_TYPES: Final[dict[str, BinarySensorEntityDescription]] = (
    resolve_entity_description_keys(_BINARY_SENSORS)
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: DeviceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add binary sensors for passed config_entry in HA"""
    device = config_entry.runtime_data

    new_sensors = [
        WalleCubeBinarySensor(device, sensor)
        for sensor in BINARY_SENSOR_TYPES
        if hasattr(device, sensor)
    ]

    if new_sensors:
        async_add_entities(new_sensors)


class WalleCubeBinarySensor(WalleCubeEntity, BinarySensorEntity):
    """Binary sensor backed by a boolean device field"""

    def __init__(self, device: DeviceBase, sensor: str):
        super().__init__(device)

        self._attr_unique_id = f"wc_{device.identifier}_{sensor}"
        self.entity_description = BINARY_SENSOR_TYPES[sensor]
        if self.entity_description.translation_key is None:
            self._attr_translation_key = self.entity_description.key
        self._register_update_callback("_attr_is_on", sensor)
