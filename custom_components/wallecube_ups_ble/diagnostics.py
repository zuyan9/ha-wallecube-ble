from homeassistant.core import HomeAssistant

from . import DeviceConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DeviceConfigEntry
):
    return entry.runtime_data.diagnostics.build_diagnostics_dict()
