"""Config and options flows that need Home Assistant installed"""

import pytest

# a Home Assistant that fails to import its dependencies skips these tests too
pytest.importorskip("homeassistant.components.bluetooth", exc_type=ImportError)

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import voluptuous as vol
from bleak.exc import BleakError
from bleak_retry_connector import BleakNotFoundError
from homeassistant.data_entry_flow import FlowResultType, section
from pytest_mock import MockerFixture

from custom_components.wallecube_ups_ble import config_flow
from custom_components.wallecube_ups_ble.wclib.connection import UPS_SERVICE_UUID
from custom_components.wallecube_ups_ble.wclib.devices.w150 import Device
from custom_components.wallecube_ups_ble.wclib.exceptions import (
    SessionKeyError,
    UnsupportedBluetoothProtocol,
)
from tests.fakes import ADDRESS, LOCAL_NAME

SETTINGS = {
    "update_period": 10,
    "advanced_connection_options": {"bluez_start_notify": False},
}


def service_info(address: str, local_name: str, *service_uuids: str):
    ble_dev = MagicMock(address=address)
    ble_dev.name = local_name
    advertisement = MagicMock(local_name=local_name, service_uuids=list(service_uuids))
    return SimpleNamespace(address=address, device=ble_dev, advertisement=advertisement)


def form_values(schema: vol.Schema) -> dict:
    """What an untouched form submits: the suggested values, else the defaults"""
    values = {}
    for key, value in schema.schema.items():
        if isinstance(value, section):
            values[str(key)] = form_values(value.schema)
        elif (suggested := (key.description or {}).get("suggested_value")) is not None:
            values[str(key)] = suggested
        elif key.default is not vol.UNDEFINED:
            values[str(key)] = key.default()
    return values


@pytest.fixture
def flow() -> config_flow.WalleCubeConfigFlow:
    flow = config_flow.WalleCubeConfigFlow()
    flow.hass = MagicMock()
    flow.flow_id = "flow"
    flow.handler = "wallecube_ups_ble"
    flow.context = {"source": "bluetooth"}
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()
    flow._async_current_ids = MagicMock(return_value=set())
    return flow


@pytest.fixture
def validate(mocker: MockerFixture) -> AsyncMock:
    return mocker.patch.object(
        config_flow, "_validate_device", new=AsyncMock(return_value={})
    )


async def test_discovery_creates_the_entry_once_the_device_answers(
    flow, validate: AsyncMock
):
    result = await flow.async_step_bluetooth(service_info(ADDRESS, LOCAL_NAME))

    flow.async_set_unique_id.assert_awaited_once_with(unique_id=ADDRESS)
    flow._abort_if_unique_id_configured.assert_called_once()
    assert result["step_id"] == "bluetooth_confirm"
    assert result["description_placeholders"] == {
        "name": f"WalleCube UPS ({LOCAL_NAME})"
    }
    assert flow.context["confirm_only"]
    assert form_values(result["data_schema"]) == SETTINGS

    validate.return_value = {"base": "bt_timeout"}
    result = await flow.async_step_bluetooth_confirm(SETTINGS)
    assert result["errors"] == {"base": "bt_timeout"}

    validate.return_value = {}
    result = await flow.async_step_bluetooth_confirm(SETTINGS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "WalleCube-4E52"
    assert result["data"] == SETTINGS | {"address": ADDRESS}


async def test_manual_setup_offers_the_discovered_devices(
    flow, validate: AsyncMock, mocker: MockerFixture
):
    flow.context = {"source": "user"}
    mocker.patch.object(
        config_flow,
        "async_discovered_service_info",
        return_value=[
            service_info(ADDRESS, LOCAL_NAME, UPS_SERVICE_UUID),
            service_info("11:22:33:44:55:66", "Other"),
        ],
    )

    result = await flow.async_step_user()

    [devices] = result["data_schema"].schema.values()
    assert devices.container == {ADDRESS: f"{LOCAL_NAME} - WalleCube UPS ({ADDRESS})"}

    result = await flow.async_step_user({"address": ADDRESS})
    assert result["step_id"] == "device_confirm"
    result = await flow.async_step_device_confirm(SETTINGS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    flow.async_set_unique_id.assert_awaited_once_with(ADDRESS, raise_on_progress=False)


STORED = {
    "update_period": 30,
    "advanced_connection_options": {"bluez_start_notify": True},
}


@pytest.mark.parametrize(
    "options",
    [
        STORED,
        # saved before the logging, diagnostics and timeout options were removed
        {
            "update_period": 30,
            "advanced_connection_options": {
                "connection_timeout": 20,
                "bluez_start_notify": True,
            },
            "diagnostics_options": {"collect_packets": True},
            "log_options": {"log_connection": True},
        },
    ],
)
async def test_options_start_from_the_stored_settings(options: dict):
    entry = SimpleNamespace(
        data={"address": ADDRESS} | SETTINGS,
        options=options,
        runtime_data=SimpleNamespace(device="W150"),
    )
    handler = config_flow.OptionsFlowHandler()
    handler.hass = MagicMock()
    handler.hass.config_entries.async_get_known_entry.return_value = entry
    handler.handler = "entry"
    handler.flow_id = "flow"

    result = await handler.async_step_init()

    assert result["description_placeholders"] == {"device_name": "W150"}
    assert form_values(result["data_schema"]) == STORED
    result = await handler.async_step_init(STORED)
    assert result["data"] == STORED


@pytest.mark.parametrize(
    ("failure", "errors"),
    [
        (None, {}),
        (SessionKeyError(), {"base": "session_key_failed"}),
        (UnsupportedBluetoothProtocol("F0BF", []), {"base": "unsupported_protocol"}),
        (TimeoutError(), {"base": "bt_timeout"}),
        (BleakNotFoundError(), {"base": "bt_not_found"}),
        (BleakError(), {"base": "bt_general_error"}),
        (RuntimeError(), {"base": "unknown"}),
    ],
)
async def test_validation_names_why_the_device_did_not_answer(
    failure: Exception | None, errors: dict[str, str]
):
    device = MagicMock(spec=Device)
    device.connect = AsyncMock(side_effect=failure)
    device.disconnect = AsyncMock()

    assert await config_flow._validate_device(device, SETTINGS) == errors
    device.with_bluez_start_notify.assert_called_once_with(False)
    device.disconnect.assert_awaited_once()
