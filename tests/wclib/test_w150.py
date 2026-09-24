import asyncio
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture

from custom_components.wallecube_ble.wclib import NewDevice
from custom_components.wallecube_ble.wclib.connection import (
    TELEMETRY_CHARACTERISTIC_UUID,
    UPS_SERVICE_UUID,
    ConnectionState,
)
from custom_components.wallecube_ble.wclib.devices.w150 import Device, PowerEvent
from custom_components.wallecube_ble.wclib.encryption import (
    SessionCipher,
    derive_session_key,
)
from custom_components.wallecube_ble.wclib.exceptions import PacketParseError

ON_BATTERY = 1 << 8


def telemetry_frame(
    *,
    input_mv: int = 12_150,
    input_ma: int = 2_210,
    output_mv: int = 12_020,
    output_ma: int = 1_530,
    battery_permille: int = 875,
    battery_mv: int = 12_480,
    battery_ma: int = -1_450,
    temperature_decidegrees: int = 253,
    remaining_seconds: int = 7_260,
    energy_raw: int = 1_234_567,
    status_flags: int = 0,
    event: int = 0,
) -> bytes:
    """Build a 40-byte telemetry notification: magic, event byte, 38-byte payload"""
    payload = struct.pack(
        "<HHHHHH8shhHHI4sH",
        input_mv,
        input_ma,
        output_mv,
        output_ma,
        battery_permille,
        battery_mv,
        bytes(8),
        battery_ma,
        temperature_decidegrees,
        0,
        remaining_seconds,
        energy_raw,
        bytes(4),
        status_flags,
    )
    assert len(payload) == 38
    return bytes([0x51, event]) + payload


@pytest.fixture
def adv_data(mocker: MockerFixture):
    adv = mocker.MagicMock()
    adv.local_name = "Walle-8856A600C4BC"
    adv.service_uuids = [UPS_SERVICE_UUID]
    return adv


@pytest.fixture
def device(mocker: MockerFixture, adv_data):
    ble_dev = mocker.Mock()
    ble_dev.address = "88:56:A6:00:C4:BE"
    ble_dev.name = adv_data.local_name
    return Device(ble_dev, adv_data)


def test_check_matches_advertised_name_or_service(mocker: MockerFixture):
    by_name = mocker.MagicMock(local_name="Walle-8856A600C4BC", service_uuids=[])
    by_service = mocker.MagicMock(local_name=None, service_uuids=[UPS_SERVICE_UUID])
    other = mocker.MagicMock(local_name="EF-R35ABCD", service_uuids=[])

    assert Device.check(by_name)
    assert Device.check(by_service)
    assert not Device.check(other)


def test_new_device_creates_w150(mocker: MockerFixture, adv_data):
    ble_dev = mocker.Mock(address="88:56:A6:00:C4:BE")

    device = NewDevice(ble_dev, adv_data)

    assert isinstance(device, Device)
    assert device.device == "W150"
    assert device.name == "W150-C4BE"
    assert device.base_mac_hint == bytes.fromhex("8856a600c4bc")


async def test_parses_telemetry_in_display_units(device: Device):
    processed = await device.data_parse(telemetry_frame())

    assert processed is True
    assert device.dc_input_voltage == 12.15
    assert device.dc_output_voltage == 12.02
    assert device.dc_output_current == 1.53
    assert device.output_power == round(12.02 * 1.53, 2)
    assert device.battery_level == 87.5
    assert device.battery_current == -1.45
    assert device.temperature == 25.3


async def test_parses_fields_named_by_vendor_app(device: Device):
    await device.data_parse(telemetry_frame())

    assert device.dc_input_current == 2.21
    assert device.battery_voltage == 12.48
    assert device.energy_total == 1.235


@pytest.mark.parametrize(
    ("bit", "field_name"),
    [
        (2, "overload"),
        (4, "shutdown_imminent"),
        (7, "charging"),
        (8, "discharging"),
        (10, "input_power_ok"),
    ],
)
async def test_maps_status_flag_bits(device: Device, bit: int, field_name: str):
    await device.data_parse(telemetry_frame(status_flags=1 << bit))

    flags = ("overload", "shutdown_imminent", "charging", "discharging")
    for name in (*flags, "input_power_ok"):
        assert getattr(device, name) is (name == field_name)


async def test_remaining_time_only_while_on_battery(device: Device):
    await device.data_parse(telemetry_frame(status_flags=0))
    assert device.remaining_time_discharging is None

    await device.data_parse(telemetry_frame(status_flags=ON_BATTERY))
    assert device.remaining_time_discharging == 121


async def test_notifies_only_changed_fields(device: Device, mocker: MockerFixture):
    await device.data_parse(telemetry_frame())

    voltage_callback = mocker.Mock()
    battery_callback = mocker.Mock()
    device.register_callback(voltage_callback, "dc_output_voltage")
    device.register_callback(battery_callback, "battery_level")

    await device.data_parse(telemetry_frame(output_mv=11_900))

    voltage_callback.assert_called_once()
    battery_callback.assert_not_called()


async def test_state_update_callback_receives_value(
    device: Device, mocker: MockerFixture
):
    state_callback = mocker.Mock()
    device.register_state_update_callback(state_callback, "battery_level")

    await device.data_parse(telemetry_frame(battery_permille=1000))

    state_callback.assert_called_once_with(100.0)


async def test_decodes_truncated_frame_partially(device: Device):
    # default ATT MTU limits notifications to 20 bytes
    await device.data_parse(telemetry_frame()[:20])

    assert device.dc_output_voltage == 12.02
    assert device.dc_output_current == 1.53
    assert device.battery_level == 87.5
    assert device.battery_current is None
    assert device.remaining_time_discharging is None
    assert device.charging is None


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (0, None),
        (1, PowerEvent.POWER_RESTORED),
        (2, PowerEvent.POWER_LOST),
        (7, None),
    ],
)
async def test_parses_event_byte(device: Device, event: int, expected):
    await device.data_parse(telemetry_frame(event=event))

    assert device.power_event is expected


async def test_each_power_event_reaches_state_callbacks(
    device: Device, mocker: MockerFixture
):
    state_callback = mocker.Mock()
    device.register_state_update_callback(state_callback, "power_event")

    for event in (2, 0, 1, 0, 2):
        await device.data_parse(telemetry_frame(event=event))

    assert [c.args[0] for c in state_callback.call_args_list] == [
        PowerEvent.POWER_LOST,
        None,
        PowerEvent.POWER_RESTORED,
        None,
        PowerEvent.POWER_LOST,
    ]


def test_parses_versions_from_info_block(device: Device):
    # payload after the magic byte: uninitialized byte, power-board hardware and
    # firmware, front-panel hardware and firmware, zero padding
    device.info_parse(bytes.fromhex("aa 0300 1d00 0300 1300") + bytes(6))

    assert device.power_board_hardware_version == 3
    assert device.power_board_firmware_version == 29
    assert device.hardware_version == 3
    assert device.firmware_version == 19


def test_power_board_versions_are_unknown_when_not_reported(device: Device):
    device.info_parse(bytes.fromhex("00 0000 0000 0300 1300") + bytes(6))

    assert device.power_board_firmware_version is None
    assert device.firmware_version == 19


async def test_rejects_frame_without_payload(device: Device):
    with pytest.raises(PacketParseError):
        await device.data_parse(b"\x51\x00")


@pytest.fixture
def client(mocker: MockerFixture):
    info = SessionCipher(derive_session_key(bytes.fromhex("8856a600c4bc"))).encrypt(
        bytes.fromhex("5100 0301 0200 0300 1300")
    )
    client = MagicMock(is_connected=True)
    client.read_gatt_char = AsyncMock(return_value=bytearray(info))
    client.write_gatt_char = AsyncMock()
    client.start_notify = AsyncMock()
    client.disconnect = AsyncMock()
    client.services.get_characteristic = MagicMock(
        side_effect=lambda uuid: SimpleNamespace(uuid=uuid)
    )
    mocker.patch(
        "custom_components.wallecube_ble.wclib.connection.establish_connection",
        new=AsyncMock(return_value=client),
    )
    return client


def telemetry_handler(client):
    for args in client.start_notify.await_args_list:
        if args.args[0].uuid == TELEMETRY_CHARACTERISTIC_UUID:
            return args.args[1]
    raise AssertionError("telemetry was not subscribed")


async def test_connect_and_notifications_update_fields(
    device: Device, client, mocker: MockerFixture
):
    # settings reads have their own tests, keep this one to the telemetry path
    refresh = mocker.patch.object(device, "refresh_settings", new=AsyncMock())
    battery_callback = mocker.Mock()
    device.register_callback(battery_callback, "battery_level")
    device.with_enabled_packet_diagnostics()

    await device.with_disabled_reconnect().connect()
    state = await device.wait_until_authenticated_or_error()
    await device._refresh_task
    await telemetry_handler(client)(
        None, bytearray(telemetry_frame(status_flags=ON_BATTERY))
    )

    assert state is ConnectionState.AUTHENTICATED
    refresh.assert_awaited_once()
    # versions come from the info block that keyed the session
    assert device.firmware_version == 19
    assert device.power_board_firmware_version == 2
    assert device.battery_level == 87.5
    assert device.remaining_time_discharging == 121
    battery_callback.assert_called_once()

    diagnostics = device.diagnostics.build_diagnostics_dict()
    assert diagnostics["address"] == "88:56:A6:**:**:**"
    assert diagnostics["session"]["from_advertised_name"] is True
    assert len(diagnostics["frames_received"]) == 2
    assert "8856a600c4bc" not in str(diagnostics).lower()


async def test_disconnect_cancels_running_settings_refresh(
    device: Device, client, mocker: MockerFixture
):
    started = asyncio.Event()

    async def slow_refresh():
        started.set()
        await asyncio.Event().wait()

    mocker.patch.object(device, "refresh_settings", side_effect=slow_refresh)

    await device.with_disabled_reconnect().connect()
    await started.wait()
    task = device._refresh_task
    await device.disconnect()

    assert task is not None
    assert task.cancelled() or task.cancelling()
    assert device._refresh_task is None
    assert device._poll_task is None


async def test_polls_wifi_status_while_connected(
    device: Device, client, mocker: MockerFixture
):
    mocker.patch.object(device, "refresh_settings", new=AsyncMock())
    mocker.patch.object(Device, "POLL_INTERVAL", 0)
    polled = asyncio.Event()
    mocker.patch.object(device, "poll", side_effect=lambda: polled.set())

    await device.with_disabled_reconnect().connect()
    await asyncio.wait_for(polled.wait(), 1)
    task = device._poll_task
    await device.disconnect()

    assert task is not None
    assert task.cancelled() or task.cancelling()
    assert device._poll_task is None


async def test_new_session_replaces_pending_settings_refresh(
    device: Device, client, mocker: MockerFixture
):
    started = asyncio.Event()

    async def slow_refresh():
        started.set()
        await asyncio.Event().wait()

    mocker.patch.object(device, "refresh_settings", side_effect=slow_refresh)
    await device.with_disabled_reconnect().connect()
    await started.wait()
    first = device._refresh_task

    device._on_connection_state(ConnectionState.AUTHENTICATED)
    await asyncio.sleep(0)

    assert first is not None
    assert first.cancelled()
    assert device._refresh_task is not first
    await device.disconnect()


async def test_failing_settings_refresh_does_not_break_the_connection(
    device: Device, client, mocker: MockerFixture
):
    mocker.patch.object(
        device, "refresh_settings", new=AsyncMock(side_effect=RuntimeError("boom"))
    )

    await device.with_disabled_reconnect().connect()
    await device._refresh_task

    assert device.connection_state is ConnectionState.AUTHENTICATED
