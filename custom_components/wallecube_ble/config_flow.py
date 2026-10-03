"""Config flow for WalleCube BLE integration"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from bleak.exc import BleakError
from bleak_retry_connector import BleakNotFoundError
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.data_entry_flow import section

from . import wclib
from .const import (
    CONF_ADVANCED_CONNECTION_OPTIONS,
    CONF_BLUEZ_START_NOTIFY,
    CONF_UPDATE_PERIOD,
    DEFAULT_UPDATE_PERIOD,
    DOMAIN,
)
from .wclib.exceptions import SessionKeyError, UnsupportedBluetoothProtocol

_LOGGER = logging.getLogger(__name__)

# the most specific first, BleakNotFoundError is a BleakError
_ERRORS: tuple[tuple[type[Exception], str], ...] = (
    (SessionKeyError, "session_key_failed"),
    (UnsupportedBluetoothProtocol, "unsupported_protocol"),
    (TimeoutError, "bt_timeout"),
    (BleakNotFoundError, "bt_not_found"),
    (BleakError, "bt_general_error"),
)


class WalleCubeConfigFlow(ConfigFlow, domain=DOMAIN):
    """WalleCube BLE ConfigFlow"""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow"""
        self._discovered_device: wclib.DeviceBase | None = None
        self._discovered_devices: dict[str, wclib.DeviceBase] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle the bluetooth discovery step"""
        await self.async_set_unique_id(unique_id=discovery_info.address)
        self._abort_if_unique_id_configured()

        device = wclib.NewDevice(discovery_info.device, discovery_info.advertisement)
        if device is None:
            return self.async_abort(reason="not_supported")

        self._discovered_device = device
        _LOGGER.debug("Discovered device: %s", device.device)
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm discovery"""
        assert self._discovered_device is not None
        device = self._discovered_device
        self._set_confirm_only()
        name = f"{device.device} ({device.local_name or device.name})"
        return await self._async_confirm("bluetooth_confirm", name, user_input)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the user step to pick discovered device"""
        if user_input is not None:
            self._discovered_device = self._discovered_devices[user_input[CONF_ADDRESS]]
            return await self.async_step_device_confirm()

        current_addresses = self._async_current_ids()
        for discovery_info in async_discovered_service_info(self.hass):
            address = discovery_info.address
            if address in current_addresses or address in self._discovered_devices:
                continue
            device = wclib.NewDevice(
                discovery_info.device, discovery_info.advertisement
            )
            if device is not None:
                self._discovered_devices[address] = device

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        labels = {
            address: f"{device.local_name or device.name} - {device.device} ({address})"
            for address, device in self._discovered_devices.items()
        }
        return self.async_show_form(
            step_id="user",
            last_step=False,
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(labels)}),
        )

    async def async_step_device_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._discovered_device is not None
        device = self._discovered_device
        if user_input is not None:
            await self.async_set_unique_id(device.address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
        return await self._async_confirm("device_confirm", device.device, user_input)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry[wclib.DeviceBase],
    ) -> OptionsFlow:
        return OptionsFlowHandler()

    async def _async_confirm(
        self, step_id: str, name: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Connect to the picked device once the user confirmed the settings"""
        assert self._discovered_device is not None
        device = self._discovered_device
        placeholders = {"name": name}
        self.context["title_placeholders"] = placeholders

        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await _validate_device(device, user_input)
            if not errors:
                return self.async_create_entry(
                    title=device.name,
                    data=user_input | {CONF_ADDRESS: device.address},
                )

        return self.async_show_form(
            step_id=step_id,
            description_placeholders=placeholders,
            errors=errors,
            data_schema=_settings_schema({}),
        )


class OptionsFlowHandler(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        device: wclib.DeviceBase | None = getattr(
            self.config_entry, "runtime_data", None
        )
        values = self.config_entry.data | self.config_entry.options

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                _settings_schema(values),
                {
                    CONF_UPDATE_PERIOD: values.get(
                        CONF_UPDATE_PERIOD, DEFAULT_UPDATE_PERIOD
                    )
                },
            ),
            description_placeholders={
                "device_name": device.device if device is not None else "WalleCube"
            },
        )


async def _validate_device(
    device: wclib.DeviceBase, user_input: dict[str, Any]
) -> dict[str, str]:
    """Connect and establish the session to verify the device is reachable"""
    advanced = user_input.get(CONF_ADVANCED_CONNECTION_OPTIONS, {})
    device.with_bluez_start_notify(advanced.get(CONF_BLUEZ_START_NOTIFY, False))
    try:
        await device.connect()
    except Exception as e:
        error = next((key for kind, key in _ERRORS if isinstance(e, kind)), None)
        if error is None:
            _LOGGER.exception("Unexpected exception")
        return {"base": error or "unknown"}
    finally:
        await device.disconnect()
    return {}


def _settings_schema(values: Mapping[str, Any]) -> vol.Schema:
    """
    Settings asked during setup and in the options

    The connection options default to their stored `values`, the options flow suggests
    the stored update period itself.
    """
    advanced = values.get(CONF_ADVANCED_CONNECTION_OPTIONS, {})
    return vol.Schema(
        {
            vol.Optional(CONF_UPDATE_PERIOD, default=DEFAULT_UPDATE_PERIOD): vol.All(
                int, vol.Range(min=0)
            ),
            vol.Required(CONF_ADVANCED_CONNECTION_OPTIONS): section(
                vol.Schema(
                    {
                        vol.Optional(
                            CONF_BLUEZ_START_NOTIFY,
                            default=advanced.get(CONF_BLUEZ_START_NOTIFY, False),
                        ): bool
                    }
                ),
                {"collapsed": True},
            ),
        }
    )
