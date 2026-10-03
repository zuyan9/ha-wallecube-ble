"""WalleCube BLE event"""

from typing import Final

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DeviceConfigEntry
from .entity import WalleCubeEntity
from .wclib import controls

EVENT_TYPES: Final[dict[str, EventEntityDescription]] = {
    # event types are the option names of `PowerEvent`
    "power_event": EventEntityDescription(
        key="power_event", event_types=["power_lost", "power_restored"]
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: DeviceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    device = config_entry.runtime_data
    async_add_entities(
        WalleCubeEvent(device, description)
        for description in EVENT_TYPES.values()
        if hasattr(device, description.key)
    )


class WalleCubeEvent(WalleCubeEntity, EventEntity):
    """Event backed by a device field that is only set on the sample it happens"""

    @callback
    def _on_update(self) -> None:
        # only fired by an update, adding the entity must not fire the last event again
        if (value := self._value) is not None:
            self._trigger_event(controls.option_name(value))
            self.async_write_ha_state()
