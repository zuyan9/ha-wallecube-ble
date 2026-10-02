"""
Config entry setup that needs Home Assistant installed

Run with the Home Assistant dependencies available, e.g.
`uv run --with aiohasupervisor --with serialx pytest tests/ha`.
"""

import pytest

pytest.importorskip("homeassistant.components.bluetooth")

from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import homeassistant.helpers.issue_registry as ir
from bleak_retry_connector import MAX_CONNECT_ATTEMPTS
from homeassistant.components import bluetooth
from homeassistant.config_entries import SOURCE_BLUETOOTH, ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from pytest_mock import MockerFixture

from custom_components.wallecube_ble import async_setup_entry, async_unload_entry
from custom_components.wallecube_ble.const import DOMAIN
from custom_components.wallecube_ble.wclib.connection import (
    INFO_CHARACTERISTIC_UUID,
    TELEMETRY_CHARACTERISTIC_UUID,
    ConnectionState,
)
from custom_components.wallecube_ble.wclib.encryption import (
    SessionCipher,
    derive_session_key,
)

ADDRESS = "0A:1B:2C:3D:4E:52"
LOCAL_NAME = "Walle-0A1B2C3D4E50"


def make_client(missing: str | None = None) -> MagicMock:
    # info response of a W150: power board hardware 3 and firmware 29, front panel
    # hardware 3 and firmware 19
    info = SessionCipher(derive_session_key(bytes.fromhex("0a1b2c3d4e50"))).encrypt(
        bytes.fromhex("5100 0300 1d00 0300 1300")
    )
    client = MagicMock()
    client.is_connected = True
    client.read_gatt_char = AsyncMock(return_value=bytearray(info))
    client.write_gatt_char = AsyncMock()
    client.start_notify = AsyncMock()
    client.stop_notify = AsyncMock()
    client.disconnect = AsyncMock()
    client.services.characteristics = {}
    client.services.get_characteristic = MagicMock(
        side_effect=lambda uuid: None if uuid == missing else SimpleNamespace(uuid=uuid)
    )
    return client


@pytest.fixture(autouse=True)
def discovered_device(mocker: MockerFixture):
    ble_dev = MagicMock(address=ADDRESS)
    ble_dev.name = LOCAL_NAME
    mocker.patch.object(bluetooth, "async_address_present", return_value=True)
    mocker.patch.object(
        bluetooth,
        "async_last_service_info",
        return_value=SimpleNamespace(
            device=ble_dev,
            advertisement=MagicMock(local_name=LOCAL_NAME, service_uuids=[]),
        ),
    )


@pytest.fixture(autouse=True)
def create_issue(mocker: MockerFixture):
    mocker.patch.object(ir, "async_delete_issue")
    return mocker.patch.object(ir, "async_create_issue")


@pytest.fixture
def establish(mocker: MockerFixture):
    return mocker.patch(
        "custom_components.wallecube_ble.wclib.connection.establish_connection",
        new=AsyncMock(),
    )


@pytest.fixture
def hass():
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    return hass


@pytest.fixture
def entry():
    # HA keeps runtime_data while it retries the setup, so calling the setup again
    # with this entry reuses the device like a retry does
    return ConfigEntry(
        data={CONF_ADDRESS: ADDRESS},
        discovery_keys=MappingProxyType({}),
        domain=DOMAIN,
        minor_version=1,
        options={},
        source=SOURCE_BLUETOOTH,
        subentries_data=None,
        title="WalleCube UPS",
        unique_id=ADDRESS,
        version=1,
    )


@pytest.mark.parametrize(
    "missing", [INFO_CHARACTERISTIC_UUID, TELEMETRY_CHARACTERISTIC_UUID]
)
async def test_missing_characteristic_retries_setup(
    hass, entry, establish, create_issue, missing: str
):
    client = make_client(missing)
    establish.return_value = client

    with pytest.raises(ConfigEntryNotReady) as err:
        await async_setup_entry(hass, entry)

    assert err.value.translation_key == "unsupported_protocol"
    client.disconnect.assert_awaited_once()
    create_issue.assert_not_called()


async def test_setup_recovers_once_the_characteristic_appears(hass, entry, establish):
    establish.side_effect = [make_client(INFO_CHARACTERISTIC_UUID), make_client()]

    with pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(hass, entry)
    assert await async_setup_entry(hass, entry)

    assert entry.runtime_data.connection_state is ConnectionState.AUTHENTICATED
    hass.config_entries.async_forward_entry_setups.assert_awaited_once()
    await async_unload_entry(hass, entry)


async def test_missing_characteristic_stops_retrying_after_max_attempts(
    hass, entry, establish, create_issue
):
    establish.return_value = make_client(INFO_CHARACTERISTIC_UUID)

    for _ in range(MAX_CONNECT_ATTEMPTS):
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry)
    with pytest.raises(ConfigEntryError) as err:
        await async_setup_entry(hass, entry)

    assert err.value.translation_key == "could_not_connect_no_retry"
    assert establish.await_count == MAX_CONNECT_ATTEMPTS
    create_issue.assert_called_once()

    # reloading by hand starts a new round of attempts
    with pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(hass, entry)
    assert establish.await_count == MAX_CONNECT_ATTEMPTS + 1
