"""Config flow for WalleCube BLE integration"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any, ClassVar

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    CONN_CLASS_LOCAL_PUSH,
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
    CONF_COLLECT_PACKETS,
    CONF_COLLECT_PACKETS_AMOUNT,
    CONF_CONNECTION_TIMEOUT,
    CONF_DIAGNOSTICS_OPTIONS,
    CONF_LOG_BLEAK,
    CONF_LOG_CONNECTION,
    CONF_LOG_ENCRYPTED_PAYLOADS,
    CONF_LOG_MASKED,
    CONF_LOG_MESSAGES,
    CONF_LOG_PACKETS,
    CONF_LOG_PAYLOADS,
    CONF_UPDATE_PERIOD,
    DEFAULT_COLLECT_PACKETS_AMOUNT,
    DEFAULT_CONNECTION_TIMEOUT,
    DEFAULT_UPDATE_PERIOD,
    DOMAIN,
)
from .wclib.connection import Connection, ConnectionState
from .wclib.logging_util import LogOptions

_LOGGER = logging.getLogger(__name__)

_STATE_ERRORS = {
    ConnectionState.ERROR_AUTH_FAILED: "session_key_failed",
    ConnectionState.ERROR_UNSUPPORTED_PROTOCOL: "unsupported_protocol",
    ConnectionState.ERROR_TIMEOUT: "bt_timeout",
    ConnectionState.ERROR_NOT_FOUND: "bt_not_found",
    ConnectionState.ERROR_BLEAK: "bt_general_error",
    ConnectionState.ERROR_UNKNOWN: "unknown",
}


class WalleCubeConfigFlow(ConfigFlow, domain=DOMAIN):
    """WalleCube BLE ConfigFlow"""

    VERSION = 1
    MINOR_VERSION = 1

    CONNECTION_CLASS = CONN_CLASS_LOCAL_PUSH

    def __init__(self) -> None:
        """Initialize the config flow"""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_device: wclib.DeviceBase | None = None
        self._discovered_devices: dict[str, wclib.DeviceBase] = {}
        self._device_by_display_name: dict[str, wclib.DeviceBase] = {}
        self._local_names: dict[str, str] = {}
        self._log_options = LogOptions.no_options()

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle the bluetooth discovery step"""
        await self.async_set_unique_id(unique_id=discovery_info.address)
        self._abort_if_unique_id_configured()

        device = wclib.NewDevice(discovery_info.device, discovery_info.advertisement)
        if device is None:
            return self.async_abort(reason="not_supported")

        self._discovery_info = discovery_info
        self._discovered_device = device
        self._set_name_from_discovery(discovery_info, device.name)

        _LOGGER.debug("Discovered device: %s", device.device)
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm discovery"""
        assert self._discovered_device is not None
        device = self._discovered_device

        self._set_confirm_only()
        placeholders = {
            "name": f"{device.device} ({self._local_names[device.address]})"
        }
        self.context["title_placeholders"] = placeholders

        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._validate_device(device, user_input)
            if not errors:
                return self._create_entry(user_input, device)
            self._log_options = ConfLogOptions.from_config(user_input)

        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders=placeholders,
            errors=errors,
            data_schema=(
                schema_builder()
                .update_period()
                .conf_log(self._log_options)
                .advanced_connection_options()
                .build()
            ),
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the user step to pick discovered device"""
        if user_input is not None:
            self._discovered_device = self._device_by_display_name[
                user_input[CONF_ADDRESS]
            ]
            return await self.async_step_device_confirm()

        current_addresses = self._async_current_ids()
        for discovery_info in async_discovered_service_info(self.hass):
            address = discovery_info.address
            if address in current_addresses or address in self._discovered_devices:
                continue

            device = wclib.NewDevice(
                discovery_info.device, discovery_info.advertisement
            )
            if device is None:
                continue

            self._discovered_devices[address] = device
            self._set_name_from_discovery(discovery_info, device.name)
            display_name = f"{self._local_names[address]} - {device.device}"
            self._device_by_display_name[f"{display_name} ({address})"] = device

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            last_step=False,
            data_schema=(
                schema_builder()
                .required(CONF_ADDRESS, vol.In(list(self._device_by_display_name)))
                .build()
            ),
        )

    async def async_step_device_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._discovered_device is not None
        device = self._discovered_device

        placeholders = {"name": device.device}
        self.context["title_placeholders"] = placeholders

        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(device.address, raise_on_progress=False)
            self._abort_if_unique_id_configured()

            errors = await self._validate_device(device, user_input)
            if not errors:
                return self._create_entry(user_input, device)
            self._log_options = ConfLogOptions.from_config(user_input)

        return self.async_show_form(
            step_id="device_confirm",
            errors=errors,
            description_placeholders=placeholders,
            data_schema=(
                schema_builder()
                .update_period()
                .conf_log(self._log_options)
                .advanced_connection_options()
                .build()
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry[wclib.DeviceBase],
    ) -> OptionsFlow:
        return OptionsFlowHandler()

    def _set_name_from_discovery(
        self, discovery_info: BluetoothServiceInfoBleak, default: str
    ):
        self._local_names[discovery_info.address] = (
            discovery_info.advertisement.local_name or default
        )

    async def _validate_device(
        self, device: wclib.DeviceBase, user_input: dict[str, Any]
    ) -> dict[str, str]:
        """Connect and establish the session to verify the device is reachable"""
        advanced = user_input.get(CONF_ADVANCED_CONNECTION_OPTIONS, {})
        timeout = advanced.get(CONF_CONNECTION_TIMEOUT, DEFAULT_CONNECTION_TIMEOUT)

        (
            device.with_logging_options(ConfLogOptions.from_config(user_input))
            .with_disabled_reconnect()
            .with_connection_options(
                Connection.Options(
                    timeout=timeout,
                    bluez_start_notify=advanced.get(CONF_BLUEZ_START_NOTIFY, False),
                )
            )
        )

        try:
            await device.connect()
            state = await asyncio.wait_for(
                device.wait_until_authenticated_or_error(), timeout
            )
        except TimeoutError as e:
            device.set_connection_state(ConnectionState.ERROR_TIMEOUT, e)
            state = device.connection_state
        except Exception:
            _LOGGER.exception("Unexpected exception")
            state = ConnectionState.ERROR_UNKNOWN
        finally:
            await device.disconnect()

        if state is ConnectionState.AUTHENTICATED:
            return {}
        return {"base": _STATE_ERRORS.get(state, "error_try_refresh")}

    def _create_entry(self, user_input: dict[str, Any], device: wclib.DeviceBase):
        entry_data = user_input.copy()
        entry_data[CONF_ADDRESS] = device.address
        entry_data["local_name"] = self._local_names.get(device.address)
        return self.async_create_entry(title=device.name, data=entry_data)


class OptionsFlowHandler(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        device: wclib.DeviceBase | None = getattr(
            self.config_entry, "runtime_data", None
        )
        merged_entry = self.config_entry.data | self.config_entry.options

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                (
                    schema_builder()
                    .update_period()
                    .diagnostics_options(merged_entry)
                    .update(ConfLogOptions.schema(merged_entry))
                    .advanced_connection_options(merged_entry)
                    .build()
                ),
                {
                    CONF_UPDATE_PERIOD: merged_entry.get(
                        CONF_UPDATE_PERIOD, DEFAULT_UPDATE_PERIOD
                    )
                },
            ),
            description_placeholders={
                "device_name": device.device if device is not None else "WalleCube"
            },
        )


class ConfLogOptions:
    _CONF_OPTION_TO_LOG_OPTION: ClassVar = {
        CONF_LOG_MASKED: LogOptions.MASKED,
        CONF_LOG_CONNECTION: LogOptions.CONNECTION_DEBUG,
        CONF_LOG_MESSAGES: LogOptions.DESERIALIZED_MESSAGES,
        CONF_LOG_PACKETS: LogOptions.PACKETS,
        CONF_LOG_PAYLOADS: LogOptions.DECRYPTED_PAYLOADS,
        CONF_LOG_ENCRYPTED_PAYLOADS: LogOptions.ENCRYPTED_PAYLOADS,
        CONF_LOG_BLEAK: LogOptions.BLEAK_DEBUG,
    }

    CONF_KEY = "log_options"

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> LogOptions:
        config = config.get(cls.CONF_KEY, config)
        log_options = LogOptions.no_options()
        for conf_option, log_option in cls._CONF_OPTION_TO_LOG_OPTION.items():
            if config.get(conf_option, False):
                log_options |= log_option
        return log_options

    @classmethod
    def to_config(cls, options: LogOptions) -> dict[str, bool]:
        return {
            conf_option: True
            for conf_option, log_option in cls._CONF_OPTION_TO_LOG_OPTION.items()
            if log_option in options
        }

    @classmethod
    def schema(
        cls, defaults: Mapping[str, Any] | None = None, collapsed: bool = True
    ) -> dict:
        defaults = {} if defaults is None else defaults
        defaults = defaults.get(cls.CONF_KEY, defaults)
        return {
            vol.Required(cls.CONF_KEY): section(
                vol.Schema(
                    {
                        vol.Optional(option, default=defaults.get(option, False)): bool
                        for option in cls._CONF_OPTION_TO_LOG_OPTION
                    }
                ),
                {"collapsed": collapsed},
            ),
        }


class _SchemaBuilder:
    def __init__(self, schema: dict | None = None):
        self._schema = schema or {}

    def update_period(self, default: int = DEFAULT_UPDATE_PERIOD):
        return self.optional(
            CONF_UPDATE_PERIOD, vol.All(int, vol.Range(min=0)), default=default
        )

    def conf_log(self, options: LogOptions):
        return self.update(ConfLogOptions.schema(ConfLogOptions.to_config(options)))

    def diagnostics_options(
        self, defaults: Mapping[str, Any] | None = None, collapsed: bool = True
    ):
        defaults = {} if defaults is None else defaults
        diag = defaults.get(CONF_DIAGNOSTICS_OPTIONS, defaults)
        return self.update(
            {
                vol.Required(CONF_DIAGNOSTICS_OPTIONS): section(
                    (
                        schema_builder()
                        .optional(
                            CONF_COLLECT_PACKETS,
                            bool,
                            diag.get(CONF_COLLECT_PACKETS, False),
                        )
                        .optional(
                            CONF_COLLECT_PACKETS_AMOUNT,
                            vol.All(int, vol.Range(min=1)),
                            diag.get(
                                CONF_COLLECT_PACKETS_AMOUNT,
                                DEFAULT_COLLECT_PACKETS_AMOUNT,
                            ),
                        )
                        .build()
                    ),
                    {"collapsed": collapsed},
                ),
            }
        )

    def advanced_connection_options(
        self, defaults: Mapping[str, Any] | None = None, collapsed: bool = True
    ):
        defaults = {} if defaults is None else defaults
        advanced = defaults.get(CONF_ADVANCED_CONNECTION_OPTIONS, defaults)
        return self.update(
            {
                vol.Required(CONF_ADVANCED_CONNECTION_OPTIONS): section(
                    (
                        schema_builder()
                        .optional(
                            CONF_CONNECTION_TIMEOUT,
                            vol.All(int, vol.Range(min=1)),
                            advanced.get(
                                CONF_CONNECTION_TIMEOUT, DEFAULT_CONNECTION_TIMEOUT
                            ),
                        )
                        .optional(
                            CONF_BLUEZ_START_NOTIFY,
                            bool,
                            advanced.get(CONF_BLUEZ_START_NOTIFY, False),
                        )
                        .build()
                    ),
                    {"collapsed": collapsed},
                ),
            }
        )

    def build(self):
        return vol.Schema(self._schema)

    def update(self, entry: dict):
        return _SchemaBuilder(self._schema | entry)

    def required(self, key: str, selector: Any):
        return self.update({vol.Required(key): selector})

    def optional(self, key: str, selector: Any, default: Any = vol.UNDEFINED):
        return self.update({vol.Optional(key, default=default): selector})


def schema_builder():
    return _SchemaBuilder()
