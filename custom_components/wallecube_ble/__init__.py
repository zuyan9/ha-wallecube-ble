"""The unofficial WalleCube BLE devices integration"""

import logging
from collections.abc import Callable
from functools import partial

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from . import wclib
from .const import (
    CONF_ADVANCED_CONNECTION_OPTIONS,
    CONF_BLUEZ_START_NOTIFY,
    CONF_UPDATE_PERIOD,
    DEFAULT_UPDATE_PERIOD,
    DOMAIN,
)
from .wclib.connection import BleakError
from .wclib.exceptions import SessionKeyError, UnsupportedBluetoothProtocol
from .wclib.logging_util import mask_identifiers

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.EVENT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type DeviceConfigEntry = ConfigEntry[wclib.DeviceBase]

_LOGGER = logging.getLogger(__name__)

ConfigEntryNotReady = partial(ConfigEntryNotReady, translation_domain=DOMAIN)

_REAPPEAR_CALLBACKS_KEY = f"{DOMAIN}_reappear_callbacks"


async def async_setup_entry(hass: HomeAssistant, entry: DeviceConfigEntry) -> bool:
    """Set up WalleCube BLE device from a config entry"""
    address = entry.data.get(CONF_ADDRESS)
    if address is None:
        return False

    discovery_info = bluetooth.async_last_service_info(hass, address, connectable=True)
    if discovery_info is None:
        _register_reappear_callback(hass, entry, address)
        raise ConfigEntryNotReady(translation_key="device_not_present")

    _cancel_reappear_callback(hass, entry)

    device: wclib.DeviceBase | None = getattr(entry, "runtime_data", None)
    if device is None:
        device = wclib.NewDevice(discovery_info.device, discovery_info.advertisement)
        if device is None:
            raise ConfigEntryNotReady(translation_key="unable_to_create_device")
        entry.runtime_data = device
    else:
        device.update_ble_device(discovery_info.device)

    _apply_options(device, entry)
    try:
        # HA retries a failed setup with its own backoff, however long the UPS stays
        # unreachable, e.g. while the vendor app holds its only connection or after it
        # shut down at the end of an outage
        await device.connect()
    except (BleakError, TimeoutError) as e:
        # bleak-retry-connector and BlueZ name the device by its addresses, and HA logs
        # the traceback of a cause at debug level
        error = mask_identifiers(str(e), device.address, device.base_mac_hint)
        raise ConfigEntryNotReady(
            translation_key="could_not_connect",
            translation_placeholders={"error_msg": error},
        ) from None
    except SessionKeyError as e:
        raise ConfigEntryNotReady(translation_key="session_key_failed") from e
    except UnsupportedBluetoothProtocol as e:
        # retried like a failed connect, a service discovery can come back incomplete
        raise ConfigEntryNotReady(
            translation_key="unsupported_protocol",
            translation_placeholders={"error_msg": str(e)},
        ) from e
    except Exception as e:
        _LOGGER.exception("Unknown error")
        await device.disconnect()
        raise ConfigEntryNotReady(
            translation_key="unknown_error", translation_placeholders={"error": str(e)}
        ) from e

    def _on_disconnect(exc: Exception | None):
        hass.config_entries.async_schedule_reload(entry.entry_id)

    # registered before the platforms are set up, otherwise a disconnect meanwhile
    # is lost and the entry stays loaded on a dead link. The reload waits for this
    # setup to finish.
    entry.async_on_unload(device.listeners.on_disconnect.add(_on_disconnect))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: DeviceConfigEntry) -> bool:
    """Unload a config entry"""
    _cancel_reappear_callback(hass, entry)
    device = entry.runtime_data
    await device.disconnect()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: DeviceConfigEntry):
    _cancel_reappear_callback(hass, entry)


def _register_reappear_callback(
    hass: HomeAssistant, entry: ConfigEntry, address: str
) -> None:
    callbacks: dict[str, Callable] = hass.data.setdefault(_REAPPEAR_CALLBACKS_KEY, {})

    if entry.entry_id in callbacks:
        return

    def _on_device_reappear(
        service_info: BluetoothServiceInfoBleak,
        change: BluetoothChange,
    ) -> None:
        _LOGGER.info("%s reappeared, scheduling reload", entry.title)
        _cancel_reappear_callback(hass, entry)
        hass.config_entries.async_schedule_reload(entry.entry_id)

    callbacks[entry.entry_id] = bluetooth.async_register_callback(
        hass,
        _on_device_reappear,
        BluetoothCallbackMatcher(address=address, connectable=True),
        BluetoothScanningMode.PASSIVE,
    )
    _LOGGER.debug("Registered BLE reappear callback for %s", entry.title)


def _cancel_reappear_callback(hass: HomeAssistant, entry: ConfigEntry) -> None:
    callbacks: dict[str, Callable] = hass.data.get(_REAPPEAR_CALLBACKS_KEY, {})
    if cancel := callbacks.pop(entry.entry_id, None):
        cancel()


async def _update_listener(hass: HomeAssistant, entry: DeviceConfigEntry):
    _apply_options(entry.runtime_data, entry)


def _apply_options(device: wclib.DeviceBase, entry: DeviceConfigEntry) -> None:
    """Pass the entry's settings to the device, at setup and when they change"""
    options = entry.data | entry.options
    advanced = options.get(CONF_ADVANCED_CONNECTION_OPTIONS, {})
    device.with_update_period(
        options.get(CONF_UPDATE_PERIOD, DEFAULT_UPDATE_PERIOD)
    ).with_bluez_start_notify(advanced.get(CONF_BLUEZ_START_NOTIFY, False))
