"""WalleCube BLE event"""

from typing import Any, Final

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DeviceConfigEntry
from .entity import WalleCubeEntity, resolve_entity_description_keys
from .wclib import DeviceBase, controls

_EVENTS: Final[dict[str, EventEntityDescription]] = {
    # event types are the option names of `PowerEvent`
    "power_event": EventEntityDescription(
        key="", event_types=["power_lost", "power_restored"]
    ),
}

EVENT_TYPES: Final[dict[str, EventEntityDescription]] = resolve_entity_description_keys(
    _EVENTS
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: DeviceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add events for passed config_entry in HA"""
    device = config_entry.runtime_data

    entities = [
        WalleCubeEvent(device, event) for event in EVENT_TYPES if hasattr(device, event)
    ]
    if entities:
        async_add_entities(entities)


class WalleCubeEvent(WalleCubeEntity, EventEntity):
    """Event backed by a device field that is only set on the sample it happens"""

    def __init__(self, device: DeviceBase, event: str):
        super().__init__(device)

        self._attr_unique_id = f"wc_{device.identifier}_{event}"
        self.entity_description = EVENT_TYPES[event]
        if self.entity_description.translation_key is None:
            self._attr_translation_key = self.entity_description.key
        # not registered through `_register_update_callback`, loading the current
        # value when the entity is added would fire the last event again
        self._update_callbacks.append((event, self._on_event))

    @callback
    def _on_event(self, value: Any) -> None:
        if value is None:
            return
        self._trigger_event(controls.option_name(value))
        self.async_write_ha_state()
