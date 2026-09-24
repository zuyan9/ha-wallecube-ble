from homeassistant.core import HomeAssistant

from . import DeviceConfigEntry
from .wclib.logging_util import mask_local_name


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DeviceConfigEntry
):
    device = entry.runtime_data

    # the advertised name embeds the factory MAC, so it is masked like the address
    diagnostics: dict = {"local_name": mask_local_name(entry.data.get("local_name"))}
    diagnostics |= device.diagnostics.build_diagnostics_dict()
    return diagnostics
