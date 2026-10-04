"""Config entry setup that needs Home Assistant installed"""

import pytest

# a Home Assistant that fails to import its dependencies skips these tests too
pytest.importorskip("homeassistant.components.bluetooth", exc_type=ImportError)

from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.config_entries import SOURCE_BLUETOOTH, ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from pytest_mock import MockerFixture

from custom_components.wallecube_ble import (
    async_remove_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.wallecube_ble.const import DOMAIN
from custom_components.wallecube_ble.wclib.connection import (
    CONFIG_CHARACTERISTIC_UUID,
    INFO_CHARACTERISTIC_UUID,
    TELEMETRY_CHARACTERISTIC_UUID,
    ConnectionState,
)
from custom_components.wallecube_ble.wclib.devices import w150
from custom_components.wallecube_ble.wclib.encryption import (
    SessionCipher,
    derive_session_key,
)
from tests.fakes import (
    ADDRESS,
    W150_INFO,
    advertisement,
    ble_device,
    drop_link,
    encrypted,
    ups_client,
)


@pytest.fixture(autouse=True)
def discovered_device(mocker: MockerFixture) -> MagicMock:
    """HA's last advertisement of the UPS, return None while HA has forgotten it"""
    return mocker.patch.object(
        bluetooth,
        "async_last_service_info",
        return_value=SimpleNamespace(
            device=ble_device(), advertisement=advertisement()
        ),
    )


@pytest.fixture(autouse=True)
def delete_issue(mocker: MockerFixture) -> MagicMock:
    """The issue registry, which the mocked Home Assistant lacks"""
    return mocker.patch.object(ir, "async_delete_issue")


@pytest.fixture
def hass():
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    return hass


def make_entry(options: dict | None = None) -> ConfigEntry:
    return ConfigEntry(
        data={CONF_ADDRESS: ADDRESS},
        discovery_keys=MappingProxyType({}),
        domain=DOMAIN,
        minor_version=1,
        options=options or {},
        source=SOURCE_BLUETOOTH,
        subentries_data=None,
        title="WalleCube UPS",
        unique_id=ADDRESS,
        version=1,
    )


@pytest.fixture
def entry():
    # HA keeps runtime_data while it retries the setup, so calling the setup again
    # with this entry reuses the device like a retry does
    return make_entry()


@pytest.mark.parametrize(
    "missing", [INFO_CHARACTERISTIC_UUID, TELEMETRY_CHARACTERISTIC_UUID]
)
async def test_setup_recovers_once_the_characteristic_appears(
    hass, entry, establish, missing: str
):
    # retried like a failed connect, a service discovery can come back incomplete
    incomplete = ups_client(missing=missing)
    establish.side_effect = [incomplete, ups_client()]

    with pytest.raises(ConfigEntryNotReady) as err:
        await async_setup_entry(hass, entry)
    assert err.value.translation_key == "unsupported_protocol"
    incomplete.disconnect.assert_awaited_once()

    assert await async_setup_entry(hass, entry)
    assert entry.runtime_data.connection_state is ConnectionState.AUTHENTICATED
    hass.config_entries.async_forward_entry_setups.assert_awaited_once()
    await async_unload_entry(hass, entry)


@pytest.mark.parametrize("failure", ["timeout", "wrong key"])
async def test_failed_setup_names_the_failure(
    hass, entry, establish, client, failure: str
):
    if failure == "timeout":
        establish.side_effect = TimeoutError("timed out")
        translation_key = "could_not_connect"
    else:
        wrong_key = SessionCipher(derive_session_key(bytes.fromhex("aabbccddeeff")))
        client.read_gatt_char.return_value = encrypted(b"\x51" + W150_INFO, wrong_key)
        translation_key = "session_key_failed"

    with pytest.raises(ConfigEntryNotReady) as err:
        await async_setup_entry(hass, entry)

    assert err.value.translation_key == translation_key


async def test_options_reach_the_device(hass, establish, client):
    entry = make_entry({"advanced_connection_options": {"bluez_start_notify": True}})

    assert await async_setup_entry(hass, entry)

    for args in client.start_notify.await_args_list:
        assert args.kwargs == {"bluez": {"use_start_notify": True}}
    await async_unload_entry(hass, entry)


async def test_setup_connects_when_the_device_reappears(
    hass, entry, establish, discovered_device: MagicMock, mocker: MockerFixture
):
    # HA forgets the UPS while it is silent, the setup then waits for it
    service_info = discovered_device.return_value
    discovered_device.return_value = None
    register_callback = mocker.patch.object(bluetooth, "async_register_callback")
    with pytest.raises(ConfigEntryNotReady) as err:
        await async_setup_entry(hass, entry)
    assert err.value.translation_key == "device_not_present"

    on_reappear = register_callback.call_args.args[1]
    on_reappear(MagicMock(), bluetooth.BluetoothChange.ADVERTISEMENT)
    hass.config_entries.async_schedule_reload.assert_called_once_with(entry.entry_id)

    discovered_device.return_value = service_info
    assert await async_setup_entry(hass, entry)
    await async_unload_entry(hass, entry)


async def test_disconnect_during_platform_setup_schedules_a_reload(
    hass, entry, establish, client
):
    async def forward_entry_setups(*_):
        # the link drops while the platforms add their entities
        drop_link(establish, client)

    hass.config_entries.async_forward_entry_setups.side_effect = forward_entry_setups

    assert await async_setup_entry(hass, entry)

    hass.config_entries.async_schedule_reload.assert_called_once_with(entry.entry_id)
    await async_unload_entry(hass, entry)


async def test_disconnect_during_session_setup_retries_setup(
    hass, entry, establish, client
):
    async def start_notify(characteristic, handler, **kwargs):
        if characteristic.uuid == CONFIG_CHARACTERISTIC_UUID:
            drop_link(establish, client)
            raise BleakError("Not connected")

    client.start_notify.side_effect = start_notify

    with pytest.raises(ConfigEntryNotReady) as err:
        await async_setup_entry(hass, entry)

    assert err.value.translation_key == "could_not_connect"
    hass.config_entries.async_forward_entry_setups.assert_not_awaited()


async def test_unload_survives_a_disconnect_timeout(hass, entry, establish, client):
    assert await async_setup_entry(hass, entry)
    # bleak raises it when BlueZ does not confirm the end of the link in time
    client.disconnect.side_effect = TimeoutError

    assert await async_unload_entry(hass, entry)
    hass.config_entries.async_unload_platforms.assert_awaited_once()


async def reload(hass, entry: ConfigEntry) -> None:
    """Unload and set up the entry again, like Home Assistant reloads it"""
    assert await async_unload_entry(hass, entry)
    await entry._async_process_on_unload(hass)
    object.__delattr__(entry, "runtime_data")
    assert await async_setup_entry(hass, entry)


async def test_reload_after_a_disconnect_keeps_the_diagnostics(
    hass, entry, establish, client
):
    assert await async_setup_entry(hass, entry)
    device = entry.runtime_data
    drop_link(establish, client)
    hass.config_entries.async_schedule_reload.assert_called_once_with(entry.entry_id)

    establish.return_value = ups_client()
    await reload(hass, entry)

    # a new device, which continues the history of the one before
    assert entry.runtime_data is not device
    history = entry.runtime_data.diagnostics.build_diagnostics_dict()[
        "connection_history"
    ]
    states = [item["state"] for item in history if "state" in item]
    assert states.count("AUTHENTICATED") == 2
    assert "DISCONNECTED" in states[states.index("AUTHENTICATED") :]
    await async_unload_entry(hass, entry)


async def test_removed_entry_forgets_its_diagnostics(hass, entry, establish):
    assert await async_setup_entry(hass, entry)
    assert await async_unload_entry(hass, entry)
    await entry._async_process_on_unload(hass)
    object.__delattr__(entry, "runtime_data")

    await async_remove_entry(hass, entry)

    assert await async_setup_entry(hass, entry)
    history = entry.runtime_data.diagnostics.build_diagnostics_dict()[
        "connection_history"
    ]
    assert [item["state"] for item in history].count("AUTHENTICATED") == 1
    await async_unload_entry(hass, entry)


async def test_removed_entry_drops_its_restart_issue(
    hass, entry, establish, delete_issue: MagicMock
):
    assert await async_setup_entry(hass, entry)
    assert await async_unload_entry(hass, entry)

    # a reload keeps it, the UPS may still wait for its restart
    delete_issue.assert_not_called()

    await async_remove_entry(hass, entry)

    delete_issue.assert_called_once_with(
        hass, DOMAIN, f"restart_required_{entry.entry_id}"
    )


async def test_late_power_board_versions_update_the_device_entry(
    hass, entry, establish, client, mocker: MockerFixture
):
    mocker.patch.object(w150, "_POWER_BOARD_REREAD_DELAYS", (0,))
    registry = mocker.patch.object(dr, "async_get").return_value
    device_entry = MagicMock()
    entries = mocker.patch.object(
        dr, "async_entries_for_config_entry", return_value=[device_entry]
    )
    info = client.read_gatt_char.return_value
    # read before the UPS asked its power board, later reads get the answer
    reads = iter([encrypted(b"\x51" + bytes.fromhex("00 0000 0000 0300 1300"))])
    client.read_gatt_char.side_effect = lambda characteristic: next(reads, info)

    assert await async_setup_entry(hass, entry)
    assert entry.runtime_data.device == "WalleCube UPS"
    await entry.runtime_data._refresh_task

    entries.assert_called_once_with(registry, entry.entry_id)
    registry.async_update_device.assert_called_once_with(
        device_entry.id, model="W150", name="W150-4E52"
    )
    await async_unload_entry(hass, entry)


async def test_retry_reason_does_not_name_the_address(hass, entry, establish):
    establish.side_effect = BleakError(
        f"Walle-0A1B2C****** - {ADDRESS}: Failed to connect: "
        "/org/bluez/hci0/dev_0A_1B_2C_3D_4E_52 not found"
    )

    with pytest.raises(ConfigEntryNotReady) as err:
        await async_setup_entry(hass, entry)

    assert err.value.translation_placeholders["error_msg"] == (
        "Walle-0A1B2C****** - 0A:1B:2C:**:**:**: Failed to connect: "
        "/org/bluez/hci0/dev_0A_1B_2C_**_**_** not found"
    )
    # HA logs the traceback of a cause at debug level
    assert err.value.__cause__ is None
