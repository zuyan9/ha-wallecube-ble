import asyncio
import json
import logging
import struct
from unittest.mock import ANY, AsyncMock

import pytest
from bleak.exc import BleakError
from pytest_mock import MockerFixture

from custom_components.wallecube_ble.wclib import NewDevice
from custom_components.wallecube_ble.wclib.connection import (
    ADAPTER_CHARACTERISTIC_UUID,
    CONFIG_CHARACTERISTIC_UUID,
    UPS_SERVICE_UUID,
    ConnectionState,
)
from custom_components.wallecube_ble.wclib.devices.w150 import Device, PowerEvent
from custom_components.wallecube_ble.wclib.exceptions import PacketParseError
from tests.fakes import (
    CIPHER,
    W150_INFO,
    advertisement,
    ble_device,
    config_frame,
    drop_link,
    encrypted,
    notify_handler,
    telemetry_frame,
    ups_client,
)

ON_BATTERY = 1 << 8
INPUT_POWER = 1 << 10


def test_check_matches_advertised_name_or_service(mocker: MockerFixture):
    by_name = mocker.MagicMock(local_name="Walle-0A1B2C3D4E50", service_uuids=[])
    by_service = mocker.MagicMock(local_name=None, service_uuids=[UPS_SERVICE_UUID])
    other = mocker.MagicMock(local_name="EF-R35ABCD", service_uuids=[])

    assert Device.check(by_name)
    assert Device.check(by_service)
    assert not Device.check(other)


def test_new_device_is_named_without_a_model():
    device = NewDevice(ble_device(), advertisement(UPS_SERVICE_UUID))

    # the advertisement does not tell the models apart
    assert isinstance(device, Device)
    assert device.device == "WalleCube UPS"
    assert device.name == "WalleCube-4E52"
    assert device.base_mac_hint == bytes.fromhex("0a1b2c3d4e50")


async def test_parses_telemetry_in_display_units(device: Device):
    processed = await device.data_parse(telemetry_frame())

    # field names follow the vendor app, the battery details the vendor cloud
    assert processed is True
    assert device.dc_input_voltage == 12.15
    assert device.dc_input_current == 2.21
    assert device.dc_output_voltage == 12.02
    assert device.dc_output_current == 1.53
    assert device.output_power == round(12.02 * 1.53, 2)
    assert device.battery_level == 87.5
    assert device.battery_voltage == 12.48
    assert device.battery_current == -1.45
    # negative while discharging, like the current
    assert device.battery_power == round(12.48 * -1.45, 2)
    assert device.temperature == 25.3
    assert device.energy_total == 1.235
    assert device.cell_voltage_1 == 3.122
    assert device.cell_voltage_4 == 3.117
    assert device.cell_voltage_difference == 5
    assert device.battery_cycles == 12


@pytest.mark.parametrize(
    ("cycles", "health"),
    [(12, 100.0), (100, 100.0), (500, 71.4), (1500, 0.0), (65535, 0.0)],
)
async def test_battery_health_is_estimated_from_cycles_like_the_vendor_cloud(
    device: Device, cycles: int, health: float
):
    await device.data_parse(telemetry_frame(cycles=cycles))

    assert device.battery_health == health


@pytest.mark.parametrize(
    ("bit", "field_name"),
    [
        (2, "overload"),
        (3, "over_temperature"),
        (4, "shutdown_imminent"),
        (7, "charging"),
        (8, "discharging"),
        (10, "input_power_ok"),
        # always set by the power board
        (0, None),
    ],
)
async def test_maps_status_flag_bits(device: Device, bit: int, field_name: str | None):
    await device.data_parse(telemetry_frame(status_flags=1 << bit))

    flags = ("overload", "over_temperature", "shutdown_imminent", "charging")
    for name in (*flags, "discharging", "input_power_ok"):
        assert getattr(device, name) is (name == field_name)


@pytest.mark.parametrize(
    ("bit", "field_name"),
    [
        (0, "battery_fault"),  # cell under-voltage
        (1, "battery_fault"),  # cell over-voltage
        (3, "battery_fault"),  # discharge over-current or short circuit
        (4, "battery_fault"),  # battery monitor not responding
        (5, "battery_fault"),  # battery monitor alert input
        (6, "battery_fault"),  # battery monitor chip fault
        (9, "under_temperature"),
        (10, "input_over_voltage"),
        (11, "output_over_current"),
        # over-temperature is also a status flag, a stalled USB request is no fault
        (8, None),
        (16, None),
    ],
)
async def test_maps_fault_flag_bits(device: Device, bit: int, field_name: str | None):
    await device.data_parse(telemetry_frame(fault_flags=1 << bit))

    faults = ("battery_fault", "under_temperature", "input_over_voltage")
    for name in (*faults, "output_over_current"):
        assert getattr(device, name) is (name == field_name)
    assert device.over_temperature is False


@pytest.mark.parametrize(
    ("adapter_voltage", "output_mv", "status_flags", "fault_flags", "expected"),
    [
        # on input power the output follows the input, which is 2 V above the setting
        (12.0, 14_000, INPUT_POWER, 0, True),
        (12.0, 13_999, INPUT_POWER, 0, False),
        # on battery the output follows the setting the power board started with
        (12.0, 14_000, ON_BATTERY, 0, False),
        # the power board's own flag
        (12.0, 12_020, INPUT_POWER, 1 << 10, True),
        # with the setting unknown only the flag counts
        (None, 12_020, INPUT_POWER, 1 << 10, True),
        (None, 19_000, INPUT_POWER, 0, False),
    ],
)
async def test_input_over_voltage_compares_the_output_with_the_adapter_voltage(
    device: Device,
    adapter_voltage: float | None,
    output_mv: int,
    status_flags: int,
    fault_flags: int,
    expected: bool,
):
    device.adapter_voltage = adapter_voltage

    await device.data_parse(
        telemetry_frame(
            output_mv=output_mv, status_flags=status_flags, fault_flags=fault_flags
        )
    )

    assert device.input_over_voltage is expected


@pytest.mark.parametrize(
    ("status_flags", "remaining_seconds", "expected"),
    [
        # 79 % of the time to empty at a battery level of 87.5 % with power-board
        # firmware 1.29, the rest is the reserve
        (ON_BATTERY, 7_260, 95),
        # the UPS shows the remaining time only while on battery
        (0, 7_260, None),
        # no load to estimate from, and a sample the battery monitor missed
        (ON_BATTERY, 0xFFFF, None),
        (ON_BATTERY, 0, None),
    ],
)
async def test_remaining_time_in_minutes_while_on_battery(
    device: Device, status_flags: int, remaining_seconds: int, expected: int | None
):
    device.info_parse(W150_INFO + bytes(6))

    await device.data_parse(
        telemetry_frame(status_flags=status_flags, remaining_seconds=remaining_seconds)
    )

    assert device.remaining_time_discharging == expected


@pytest.mark.parametrize(
    ("power_board_firmware", "battery_permille", "expected"),
    [
        # 1.29 counts the level above the 18 % reserve and turns the output off at a
        # level of 1 %. When Shutdown Imminent turns on, a quarter of the time to
        # empty is left.
        (29, 85, 3),
        (29, 10, 0),
        # older firmware counts the whole charge and turns the output off at 18 %
        (27, 250, 4),
        (27, 180, 0),
        # without the version, the level can't be related to the whole charge
        (0, 250, None),
    ],
)
async def test_remaining_time_counts_down_to_the_output_cutoff(
    device: Device,
    power_board_firmware: int,
    battery_permille: int,
    expected: int | None,
):
    hardware = 3 if power_board_firmware else 0
    device.info_parse(
        struct.pack("<xHHHH", hardware, power_board_firmware, 3, 19) + bytes(6)
    )

    await device.data_parse(
        telemetry_frame(
            status_flags=ON_BATTERY,
            battery_permille=battery_permille,
            remaining_seconds=923,
        )
    )

    assert device.remaining_time_discharging == expected


@pytest.mark.parametrize(
    ("decidegrees", "expected"),
    [(253, 25.3), (-400, -40.0), (850, 85.0), (-401, None), (851, None)],
)
async def test_battery_temperature_outside_the_displayed_range_is_unknown(
    device: Device, decidegrees: int, expected: float | None
):
    await device.data_parse(telemetry_frame(temperature_decidegrees=decidegrees))

    assert device.temperature == expected


async def test_notifies_only_changed_fields(device: Device, mocker: MockerFixture):
    await device.data_parse(telemetry_frame())

    voltage_callback = mocker.Mock()
    battery_callback = mocker.Mock()
    device.subscribe("dc_output_voltage", voltage_callback, throttled=True)
    device.subscribe("battery_level", battery_callback, throttled=True)

    await device.data_parse(telemetry_frame(output_mv=11_900))

    voltage_callback.assert_called_once()
    battery_callback.assert_not_called()


async def test_listeners_see_the_new_value(device: Device):
    values = []
    device.subscribe("battery_level", lambda: values.append(device.battery_level))

    await device.data_parse(telemetry_frame(battery_permille=1000))

    assert values == [100.0]


def test_data_fields_are_the_values_from_telemetry(device: Device):
    assert {
        "battery_level",
        "input_power_ok",
        "battery_fault",
        "input_over_voltage",
        "output_power",
        "battery_power",
        "remaining_time_discharging",
    } <= device.data_fields
    # settings, versions and Wi-Fi come from elsewhere, the event only lasts a frame
    assert not device.data_fields & {
        "standby_time",
        "firmware_version",
        "wifi_rssi",
        "power_event",
    }


async def test_telemetry_values_go_stale_without_frames(
    device: Device, mocker: MockerFixture
):
    await device._on_data(telemetry_frame())
    level = mocker.Mock()
    charging = mocker.Mock()
    standby = mocker.Mock()
    device.subscribe("battery_level", level, throttled=True)
    device.subscribe("charging", charging)
    device.subscribe("standby_time", standby)

    assert device._check_data(device._last_data + 59) == pytest.approx(1)
    assert device.data_current

    # past the timeout, as _last_data + 60 can round to just under it
    device._check_data(device._last_data + 61)

    assert not device.data_current
    level.assert_called_once()
    charging.assert_called_once()
    standby.assert_not_called()


async def test_each_frame_restarts_the_stale_timer(
    device: Device, mocker: MockerFixture
):
    clock = mocker.patch(
        "custom_components.wallecube_ble.wclib.devicebase.time.monotonic"
    )
    clock.return_value = 1000.0
    await device._on_data(telemetry_frame())
    clock.return_value = 1050.0
    await device._on_data(telemetry_frame())

    # 70 s after the first frame, but 20 s after the last
    assert device._check_data(1070.0) == pytest.approx(40)
    assert device.data_current


async def test_next_frame_makes_stale_values_current_again(
    device: Device, mocker: MockerFixture
):
    await device._on_data(telemetry_frame())
    device._check_data(device._last_data + 61)
    level = mocker.Mock()
    device.subscribe("battery_level", level, throttled=True)

    # the same values as before the gap, they are published nonetheless
    await device._on_data(telemetry_frame())

    assert device.data_current
    level.assert_called_once()


async def test_throttled_listeners_follow_the_update_period(
    device: Device, mocker: MockerFixture
):
    clock = mocker.patch("custom_components.wallecube_ble.wclib.devicebase.time.time")
    device.with_update_period(10)
    level = mocker.Mock()
    voltage = mocker.Mock()
    live = mocker.Mock()
    device.subscribe("battery_level", level, throttled=True)
    device.subscribe("dc_output_voltage", voltage, throttled=True)
    device.subscribe("battery_level", live)

    async def frame(at: float, battery_permille: int, output_mv: int = 12_020):
        clock.return_value = at
        await device._on_data(
            telemetry_frame(battery_permille=battery_permille, output_mv=output_mv)
        )

    # the first seconds pass through, the entities would stay unknown otherwise
    await frame(1000, 500)
    await frame(1001, 501)
    assert level.call_count == 2

    await frame(1007, 502)
    voltage.reset_mock()
    await frame(1008, 503)
    await frame(1009, 503, output_mv=11_900)

    # within the period only the unthrottled listener sees the changes
    assert level.call_count == 3
    assert live.call_count == 4
    voltage.assert_not_called()

    await frame(1017, 504, output_mv=11_900)

    # the voltage changed within the period is delivered with the next update
    assert level.call_count == 4
    voltage.assert_called_once()

    clock.return_value = 1018
    device._check_data(device._last_data + 61)

    # availability changes are not held back by the update period
    assert level.call_count == 5
    assert voltage.call_count == 2


async def test_decodes_truncated_frame_partially(device: Device):
    # default ATT MTU limits notifications to 20 bytes
    await device.data_parse(telemetry_frame()[:20])

    assert device.dc_output_voltage == 12.02
    assert device.dc_output_current == 1.53
    assert device.battery_level == 87.5
    assert device.battery_current is None
    assert device.remaining_time_discharging is None
    assert device.charging is None
    # the frame ends inside the cell voltages
    assert device.cell_voltage_3 == 3.122
    assert device.cell_voltage_4 is None
    assert device.cell_voltage_difference is None
    assert device.battery_health is None
    assert device.input_over_voltage is None


async def test_each_power_event_reaches_the_listeners(device: Device):
    events = []
    device.subscribe("power_event", lambda: events.append(device.power_event))

    # only 1 and 2 are events, 7 reads as none like 0
    for event in (2, 0, 1, 0, 7, 2):
        await device.data_parse(telemetry_frame(event=event))

    assert events == [
        PowerEvent.POWER_LOST,
        None,
        PowerEvent.POWER_RESTORED,
        None,
        PowerEvent.POWER_LOST,
    ]


@pytest.mark.parametrize(
    ("info", "model", "name", "power_board_firmware"),
    [
        ("aa 0300 1d00 0300 1300", "W150", "W150-4E52", 29),
        ("00 0400 1d00 0300 1300", "W180", "W180-4E52", 29),
        # the power board did not answer
        ("00 0000 0000 0300 1300", "WalleCube UPS", "WalleCube-4E52", None),
    ],
)
def test_info_block_names_the_model_and_versions(
    device: Device, info: str, model: str, name: str, power_board_firmware: int | None
):
    # payload after the magic byte: uninitialized byte, power-board hardware and
    # firmware, front-panel hardware and firmware, zero padding
    device.info_parse(bytes.fromhex(info) + bytes(6))

    assert device.device == model
    assert device.name == name
    assert device.power_board_firmware_version == power_board_firmware
    assert (device.hardware_version, device.firmware_version) == (3, 19)


async def test_rejects_frame_without_payload(device: Device):
    with pytest.raises(PacketParseError):
        await device.data_parse(b"\x51\x00")


async def test_connect_and_notifications_update_fields(
    device: Device, establish, client, mocker: MockerFixture
):
    # settings reads have their own tests, keep this one to the telemetry path
    refresh = mocker.patch.object(device, "refresh_settings", new=AsyncMock())
    battery_callback = mocker.Mock()
    device.subscribe("battery_level", battery_callback, throttled=True)

    await device.connect()
    await device._refresh_task
    await notify_handler(client)(
        None, bytearray(telemetry_frame(status_flags=ON_BATTERY))
    )

    assert device.connection_state is ConnectionState.AUTHENTICATED
    refresh.assert_awaited_once()
    # versions come from the info block that keyed the session
    assert device.firmware_version == 19
    assert device.power_board_firmware_version == 29
    assert device.battery_level == 87.5
    assert device.remaining_time_discharging == 95
    battery_callback.assert_called_once()


async def test_bluez_start_notify_reaches_every_subscription(
    device: Device, establish, client
):
    await device.with_bluez_start_notify().connect()

    assert client.start_notify.await_count == 2
    for args in client.start_notify.await_args_list:
        assert args.kwargs == {"bluez": {"use_start_notify": True}}


async def test_disconnect_stops_the_session_tasks(
    device: Device, establish, client, mocker: MockerFixture
):
    # while connected the settings are read, the Wi-Fi status is polled and the
    # telemetry is watched
    refreshing = asyncio.Event()

    async def slow_refresh():
        refreshing.set()
        await asyncio.Event().wait()

    mocker.patch.object(device, "refresh_settings", side_effect=slow_refresh)
    mocker.patch.object(Device, "POLL_INTERVAL", 0)
    mocker.patch.object(Device, "DATA_TIMEOUT", 0.01)
    stale = asyncio.Event()
    device.subscribe("charging", stale.set)

    # no telemetry arrives after connecting
    await device.connect()
    await asyncio.wait_for(asyncio.gather(refreshing.wait(), stale.wait()), 1)
    tasks = asyncio.all_tasks() - {asyncio.current_task()}
    await device.disconnect()
    await asyncio.sleep(0)

    assert not device.data_current
    # the poll asks for the Wi-Fi status, type 0x01 tagged with 0x40 and no payload
    assert CIPHER.decrypt(client.write_gatt_char.await_args.args[1])[:2] == b"\x41\x00"
    assert len(tasks) == 3
    assert all(task.cancelled() for task in tasks)


async def test_new_session_replaces_pending_settings_refresh(
    device: Device, establish, mocker: MockerFixture
):
    started = asyncio.Event()

    async def slow_refresh():
        started.set()
        await asyncio.Event().wait()

    mocker.patch.object(device, "refresh_settings", side_effect=slow_refresh)
    await device.connect()
    await asyncio.wait_for(started.wait(), 1)
    first = device._refresh_task

    device._on_connection_state(ConnectionState.AUTHENTICATED)
    await asyncio.sleep(0)

    assert first is not None
    assert first.cancelled()
    assert device._refresh_task is not first
    await device.disconnect()


async def test_diagnostics_do_not_identify_the_device(
    device: Device, establish, client, mocker: MockerFixture
):
    mocker.patch.object(device, "refresh_settings", new=AsyncMock())
    token = CIPHER.token.to_bytes(4, "little")
    info = client.read_gatt_char.return_value
    adapter = encrypted(b"\x51" + struct.pack("<5H", 3000, 2100, 12000, 0, 0))
    # connected at -60 dBm to "MyHome" with 192.168.1.23, gateway and netmask
    wifi = bytes.fromhex("01c4 c0a80117 c0a80101 ffffff00 06") + b"MyHome"
    wifi_frame = config_frame(0x01, wifi)

    await device.connect()
    client.read_gatt_char.return_value = adapter
    await device.read_value(ADAPTER_CHARACTERISTIC_UUID)
    await device.send_command(ADAPTER_CHARACTERISTIC_UUID, b"\x01\x02")
    await notify_handler(client, CONFIG_CHARACTERISTIC_UUID)(None, wifi_frame)
    await notify_handler(client)(None, bytearray(telemetry_frame()))

    diagnostics = device.diagnostics.build_diagnostics_dict()
    dump = json.dumps(diagnostics).lower()
    written = [bytes(c.args[1]) for c in client.write_gatt_char.await_args_list]
    for ciphertext in (info, adapter, wifi_frame, *written):
        assert bytes(ciphertext).hex() not in dump
    assert token.hex() not in dump
    assert "myhome" not in dump
    assert b"MyHome".hex() not in dump
    assert "c0a80117" not in dump
    for address_part in ("3d4e50", "3d4e52", "3d:4e", "3d_4e", "4e52", "4e50"):
        assert address_part not in dump
    assert diagnostics["name"] == "W150-****"
    assert diagnostics["local_name"] == "Walle-0A1B2C******"
    assert diagnostics["session"] == {"from_advertised_name": True, "info": ANY}
    # decrypted payloads stay readable, the Wi-Fi status keeps connected and signal
    assert [source for _, source, _ in diagnostics["frames_received"]] == [
        "F0BF",
        "F0B2",
        "F0C1/01",
        "F0B1",
    ]
    assert diagnostics["frames_received"][2][2] == "01c4" + "00" * 19
    assert diagnostics["frames_received"][3][2] == telemetry_frame().hex()
    assert diagnostics["frames_sent"] == [(ANY, "F0B2", "0102")]


async def test_diagnostics_continue_those_of_an_earlier_object(
    device: Device, establish, client, mocker: MockerFixture
):
    mocker.patch.object(Device, "refresh_settings", new=AsyncMock())
    await device.connect()
    drop_link(establish, client)
    await device.disconnect()

    # Home Assistant creates the device anew when it reloads after the disconnect
    reloaded = Device(ble_device(), advertisement())
    reloaded.diagnostics.continue_from(device.diagnostics)
    establish.return_value = ups_client()
    await reloaded.connect()

    history = reloaded.diagnostics.build_diagnostics_dict()["connection_history"]
    states = [item["state"] for item in history]
    assert states[states.index("AUTHENTICATED") :] == [
        "AUTHENTICATED",
        "DISCONNECTED",
        "ESTABLISHING_CONNECTION",
        "CONNECTED",
        "ESTABLISHING_SESSION",
        "SESSION_ESTABLISHED",
        "SUBSCRIBING",
        "AUTHENTICATED",
    ]
    times = [item["time"] for item in history]
    assert times == sorted(times)
    frames = reloaded.diagnostics.build_diagnostics_dict()["frames_received"]
    assert [source for _, source, _ in frames] == ["F0BF", "F0BF"]
    await reloaded.disconnect()


def test_diagnostics_keep_the_recent_history(device: Device):
    for _ in range(300):
        device.listeners.on_data_received("F0B1", b"\x01")
        device.listeners.on_data_send("F0B2", b"\x02")
        device.listeners.on_state_change(ConnectionState.CONNECTED)

    diagnostics = device.diagnostics.build_diagnostics_dict()

    assert len(diagnostics["frames_received"]) == 200
    assert len(diagnostics["frames_sent"]) == 200
    assert len(diagnostics["connection_history"]) == 50


async def test_logs_do_not_name_the_address(device: Device, establish, caplog):
    caplog.set_level(logging.DEBUG)

    await device.connect()
    await device.disconnect()

    assert caplog.records
    for record in caplog.records:
        text = f"{record.name} {record.getMessage()}".upper()
        for hidden in ("3D:4E", "3D_4E", "3D4E"):
            assert hidden not in text


async def test_diagnostics_mask_addresses_in_errors(device: Device, establish):
    establish.side_effect = BleakError(
        "Walle-0A1B2C3D4E50 - 0A:1B:2C:3D:4E:52: Failed to connect: "
        "/org/bluez/hci0/dev_0A_1B_2C_3D_4E_52 not found"
    )

    with pytest.raises(BleakError):
        await device.connect()

    history = device.diagnostics.build_diagnostics_dict()["connection_history"]
    assert [{k: v for k, v in entry.items() if k != "time"} for entry in history] == [
        {"state": "ESTABLISHING_CONNECTION"},
        {"state": "ERROR_BLEAK"},
        {
            "error": "BleakError('Walle-0A1B2C****** - 0A:1B:2C:**:**:**: Failed to "
            "connect: /org/bluez/hci0/dev_0A_1B_2C_**_**_** not found')"
        },
    ]


async def test_failing_settings_refresh_does_not_break_the_connection(
    device: Device, establish, mocker: MockerFixture
):
    mocker.patch.object(
        device, "refresh_settings", new=AsyncMock(side_effect=RuntimeError("boom"))
    )

    await device.connect()
    await device._refresh_task

    assert device.connection_state is ConnectionState.AUTHENTICATED
