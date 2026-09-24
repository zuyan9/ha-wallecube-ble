"""The unofficial WalleCube BLE devices integration"""

import logging
from collections.abc import Callable
from functools import partial

import homeassistant.helpers.issue_registry as ir
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
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady

from . import wclib
from .config_flow import ConfLogOptions
from .const import (
    CONF_ADVANCED_CONNECTION_OPTIONS,
    CONF_BLUEZ_START_NOTIFY,
    CONF_COLLECT_PACKETS,
    CONF_COLLECT_PACKETS_AMOUNT,
    CONF_CONNECTION_TIMEOUT,
    CONF_DIAGNOSTICS_OPTIONS,
    CONF_UPDATE_PERIOD,
    DEFAULT_COLLECT_PACKETS_AMOUNT,
    DEFAULT_CONNECTION_TIMEOUT,
    DEFAULT_UPDATE_PERIOD,
    DOMAIN,
)
from .wclib.connection import BleakError, Connection
from .wclib.exceptions import (
    ConnectionTimeout,
    MaxConnectionAttemptsReached,
    SessionKeyError,
    UnsupportedBluetoothProtocol,
)
from .wclib.logging_util import LogOptions

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type DeviceConfigEntry = ConfigEntry[wclib.DeviceBase]

_LOGGER = logging.getLogger(__name__)

ConfigEntryNotReady = partial(ConfigEntryNotReady, translation_domain=DOMAIN)
ConfigEntryError = partial(ConfigEntryError, translation_domain=DOMAIN)

_REAPPEAR_CALLBACKS_KEY = f"{DOMAIN}_reappear_callbacks"


async def async_setup_entry(hass: HomeAssistant, entry: DeviceConfigEntry) -> bool:
    """Set up WalleCube BLE device from a config entry"""
    address = entry.data.get(CONF_ADDRESS)
    merged_options = entry.data | entry.options
    update_period = merged_options.get(CONF_UPDATE_PERIOD, DEFAULT_UPDATE_PERIOD)

    if address is None:
        return False

    if not bluetooth.async_address_present(hass, address):
        _register_reappear_callback(hass, entry, address)
        raise ConfigEntryNotReady(translation_key="device_not_present")

    _cancel_reappear_callback(hass, entry)

    device: wclib.DeviceBase | None = getattr(entry, "runtime_data", None)
    discovery_info = bluetooth.async_last_service_info(hass, address, connectable=True)

    if device is None:
        if (
            discovery_info is None
            or (
                device := wclib.NewDevice(
                    discovery_info.device, discovery_info.advertisement
                )
            )
            is None
        ):
            raise ConfigEntryNotReady(translation_key="unable_to_create_device")
        entry.runtime_data = device
    elif discovery_info is not None:
        device.update_ble_device(discovery_info.device)

    diag_options = merged_options.get(CONF_DIAGNOSTICS_OPTIONS, {})
    advanced = merged_options.get(CONF_ADVANCED_CONNECTION_OPTIONS, {})
    timeout = advanced.get(CONF_CONNECTION_TIMEOUT, DEFAULT_CONNECTION_TIMEOUT)
    issue_id = f"{entry.entry_id}_max_connection_attempts"

    try:
        await (
            device.with_update_period(update_period)
            .with_logging_options(ConfLogOptions.from_config(merged_options))
            .with_disabled_reconnect()
            .with_enabled_packet_diagnostics(
                diag_options.get(CONF_COLLECT_PACKETS, False),
                diag_options.get(
                    CONF_COLLECT_PACKETS_AMOUNT, DEFAULT_COLLECT_PACKETS_AMOUNT
                ),
            )
            .with_connection_options(
                Connection.Options(
                    timeout=timeout,
                    bluez_start_notify=advanced.get(CONF_BLUEZ_START_NOTIFY, False),
                )
            )
            .connect()
        )
        state = await device.wait_until_authenticated_or_error(raise_on_error=True)
    except (ConnectionTimeout, BleakError, TimeoutError) as e:
        raise ConfigEntryNotReady(
            translation_key="could_not_connect",
            translation_placeholders={"time": str(timeout), "error_msg": str(e)},
        ) from e
    except SessionKeyError as e:
        raise ConfigEntryNotReady(translation_key="session_key_failed") from e
    except UnsupportedBluetoothProtocol as e:
        await device.disconnect()
        raise ConfigEntryError(
            translation_key="unsupported_protocol",
            translation_placeholders={"error_msg": str(e)},
        ) from e
    except MaxConnectionAttemptsReached as e:
        await device.disconnect()
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="max_connection_attempts_reached",
            translation_placeholders={
                "device_name": device.name,
                "attempts": str(e.attempts),
            },
        )
        raise ConfigEntryError(
            translation_key="could_not_connect_no_retry",
            translation_placeholders={"attempts": str(e.attempts)},
        ) from e
    except Exception as e:
        _LOGGER.exception("Unknown error")
        await device.disconnect()
        raise ConfigEntryNotReady(
            translation_key="unknown_error", translation_placeholders={"error": str(e)}
        ) from e
    else:
        if not state.authenticated:
            await device.disconnect()
            raise ConfigEntryNotReady(
                translation_key="failed_after_successful_connection",
                translation_placeholders={"last_state": state},
            )
    ir.async_delete_issue(hass, DOMAIN, issue_id)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_update_listener))

    def _on_disconnect(exc: Exception | type[Exception] | None):
        hass.config_entries.async_schedule_reload(entry.entry_id)

    entry.async_on_unload(device.on_disconnect(_on_disconnect))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: DeviceConfigEntry) -> bool:
    """Unload a config entry"""
    _cancel_reappear_callback(hass, entry)
    device = entry.runtime_data
    await device.disconnect()
    device.with_logging_options(LogOptions.no_options())
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
        _LOGGER.info("Device %s reappeared, scheduling reload", address)
        _cancel_reappear_callback(hass, entry)
        hass.config_entries.async_schedule_reload(entry.entry_id)

    callbacks[entry.entry_id] = bluetooth.async_register_callback(
        hass,
        _on_device_reappear,
        BluetoothCallbackMatcher(address=address, connectable=True),
        BluetoothScanningMode.PASSIVE,
    )
    _LOGGER.debug("Registered BLE reappear callback for %s", address)


def _cancel_reappear_callback(hass: HomeAssistant, entry: ConfigEntry) -> None:
    callbacks: dict[str, Callable] = hass.data.get(_REAPPEAR_CALLBACKS_KEY, {})
    if cancel := callbacks.pop(entry.entry_id, None):
        cancel()


async def _update_listener(hass: HomeAssistant, entry: DeviceConfigEntry):
    device = entry.runtime_data
    merged_options = entry.data | entry.options
    diag_options = merged_options.get(CONF_DIAGNOSTICS_OPTIONS, {})
    advanced = merged_options.get(CONF_ADVANCED_CONNECTION_OPTIONS, {})

    (
        device.with_update_period(
            merged_options.get(CONF_UPDATE_PERIOD, DEFAULT_UPDATE_PERIOD)
        )
        .with_logging_options(ConfLogOptions.from_config(merged_options))
        .with_enabled_packet_diagnostics(
            diag_options.get(CONF_COLLECT_PACKETS, False),
            diag_options.get(
                CONF_COLLECT_PACKETS_AMOUNT, DEFAULT_COLLECT_PACKETS_AMOUNT
            ),
        )
        .with_connection_options(
            Connection.Options(
                timeout=advanced.get(
                    CONF_CONNECTION_TIMEOUT, DEFAULT_CONNECTION_TIMEOUT
                ),
                bluez_start_notify=advanced.get(CONF_BLUEZ_START_NOTIFY, False),
            )
        )
    )
