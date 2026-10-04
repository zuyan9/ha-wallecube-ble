import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock, PropertyMock, call

import pytest
from bleak.exc import BleakError
from pytest_mock import MockerFixture

from custom_components.wallecube_ups_ble.wclib import controls, get_controls
from custom_components.wallecube_ups_ble.wclib.connection import (
    ADAPTER_CHARACTERISTIC_UUID,
    BUZZER_CHARACTERISTIC_UUID,
    INFO_CHARACTERISTIC_UUID,
    LANGUAGE_CHARACTERISTIC_UUID,
    STANDBY_CHARACTERISTIC_UUID,
    TEMPERATURE_UNIT_CHARACTERISTIC_UUID,
)
from custom_components.wallecube_ups_ble.wclib.devices import w150
from custom_components.wallecube_ups_ble.wclib.devices.w150 import (
    SCREEN_ALWAYS_ON,
    BuzzerMode,
    Device,
    ScreenLanguage,
    TemperatureUnit,
)
from custom_components.wallecube_ups_ble.wclib.exceptions import (
    SettingNotConfirmed,
    SettingUnavailable,
    UnsupportedBluetoothProtocol,
)
from custom_components.wallecube_ups_ble.wclib.model import AdapterSettings
from custom_components.wallecube_ups_ble.wclib.packet import (
    COMMAND_CONFIRMED,
    ConfigMessage,
)
from tests.fakes import W150_INFO

# read responses carry the payload after the magic byte, zero padded to a block
ADAPTER_12V_3A = struct.pack("<5H", 3000, 2000, 12000, 11580, 11496) + bytes(5)
STANDBY_300S_100MA = struct.pack("<2H", 300, 100) + bytes(11)
# what the front panel reports until it got the answers of the power board
NO_POWER_BOARD_INFO = bytes.fromhex("00 0000 0000 0300 1300") + bytes(6)
NO_POWER_BOARD_BLOCK = bytes(15)


@pytest.fixture
def results() -> list[int]:
    """Statuses the fake device notifies for forwarded writes, confirmed when empty"""
    return []


@pytest.fixture
def settings(device: Device, mocker: MockerFixture, results: list[int]):
    """Fake device storage behind read_value/send_command"""
    values = {
        INFO_CHARACTERISTIC_UUID: W150_INFO + bytes(6),
        ADAPTER_CHARACTERISTIC_UUID: ADAPTER_12V_3A,
        STANDBY_CHARACTERISTIC_UUID: STANDBY_300S_100MA,
        BUZZER_CHARACTERISTIC_UUID: b"\x01" + bytes(14),
        LANGUAGE_CHARACTERISTIC_UUID: b"\x00" + bytes(14),
        TEMPERATURE_UNIT_CHARACTERISTIC_UUID: b"\x00" + bytes(14),
    }
    # read while connecting
    device.info_parse(values[INFO_CHARACTERISTIC_UUID])

    # every call yields like a real GATT round trip, so concurrent changes interleave
    async def read_value(characteristic: str) -> bytes:
        await asyncio.sleep(0)
        return values[characteristic]

    async def send_command(characteristic: str, payload: bytes = b"") -> None:
        await asyncio.sleep(0)
        values[characteristic] = payload + bytes(15 - len(payload))

    # like the front panel, keeps the written block whether the power board answers
    async def send_confirmed_command(
        characteristic: str, payload: bytes, timeout: float
    ) -> int:
        await send_command(characteristic, payload)
        return results.pop(0) if results else COMMAND_CONFIRMED

    mocker.patch.object(device, "read_value", side_effect=read_value)
    mocker.patch.object(device, "send_command", side_effect=send_command)
    mocker.patch.object(
        device, "send_confirmed_command", side_effect=send_confirmed_command
    )
    mocker.patch.object(device, "send_config", new=AsyncMock())
    return values


def screen_reply(timeout: int, idle_backlight: int = 70) -> ConfigMessage:
    return ConfigMessage(
        message_type=0x0C,
        token=0,
        payload=struct.pack("<IB", timeout, idle_backlight),
    )


def wifi_reply(
    *, rssi: int = -61, ip: str = "192.0.2.23", ssid: bytes = b"HomeAP"
) -> ConfigMessage:
    payload = struct.pack(
        "<Bb4s4s4sB",
        1,
        rssi,
        bytes(int(part) for part in ip.split(".")),
        bytes([192, 0, 2, 1]),
        bytes([255, 255, 255, 0]),
        len(ssid),
    )
    return ConfigMessage(message_type=0x01, token=0, payload=payload + ssid)


def wake_on_lan_reply(
    min_outage: int = 30, delay: int = 30, min_battery: int = 35
) -> ConfigMessage:
    return ConfigMessage(
        message_type=0x0A,
        token=0,
        payload=struct.pack("<3H", min_outage, delay, min_battery),
    )


@pytest.fixture
def screen(device: Device, mocker: MockerFixture):
    """Fake screen settings behind the configuration channel"""
    values = {"timeout": 600, "idle": 70}

    async def send_config(message_type: int, payload: bytes = b"") -> None:
        await asyncio.sleep(0)
        if message_type == 0x0B:
            values["timeout"] = int.from_bytes(payload[:4], "little")
            if len(payload) > 4:
                values["idle"] = payload[4]
        elif message_type == 0x0C:
            # the reply arrives as a notification after the write returned
            reply = screen_reply(values["timeout"], values["idle"])
            asyncio.get_running_loop().create_task(device.config_parse(reply))

    mocker.patch.object(device, "send_config", side_effect=send_config)
    return values


@pytest.fixture
def wake_on_lan(device: Device, mocker: MockerFixture):
    """Fake Wake-on-LAN trigger behind the configuration channel"""
    values = {"min_outage": 30, "delay": 30, "min_battery": 35}

    async def send_config(message_type: int, payload: bytes = b"") -> None:
        await asyncio.sleep(0)
        if message_type == 0x09:
            values["min_outage"], values["delay"], values["min_battery"] = (
                struct.unpack("<3H", payload)
            )
        elif message_type == 0x0A:
            reply = wake_on_lan_reply(**values)
            asyncio.get_running_loop().create_task(device.config_parse(reply))

    mocker.patch.object(device, "send_config", side_effect=send_config)
    return values


def test_declares_controls_of_the_app_settings_page(device: Device):
    # options in the order of the enum values
    assert {c.key: c.options_str for c in get_controls(device, controls.select)} == {
        "buzzer_mode": ["mute", "once", "repeat"],
        "screen_language": ["english", "chinese"],
        "temperature_unit": ["celsius", "fahrenheit"],
    }
    # the adapter ratings and the Wake-on-LAN trigger are disabled by default
    assert {c.key: c.enabled for c in get_controls(device, controls.NumberType)} == {
        "adapter_voltage": False,
        "adapter_current": False,
        "standby_time": True,
        "standby_current_threshold": True,
        "screen_timeout": True,
        "screen_brightness": True,
        "screen_idle_brightness": True,
        "wake_on_lan_min_outage": False,
        "wake_on_lan_delay": False,
        "wake_on_lan_min_battery": False,
    }
    assert [c.key for c in get_controls(device, controls.switch)] == [
        "screen_always_on"
    ]


def test_only_the_adapter_settings_need_a_restart(device: Device):
    # the power board applies them when it starts
    assert [
        c.key
        for control_type in (controls.NumberType, controls.select, controls.switch)
        for c in get_controls(device, control_type)
        if c.restart_required
    ] == ["adapter_voltage", "adapter_current"]


async def test_refresh_reads_all_settings(device: Device, settings):
    await device.refresh_settings()

    assert device.adapter_voltage == 12.0
    assert device.adapter_current == 3.0
    assert device.power_good_voltage == 11.496
    assert device.standby_time == 300
    assert device.standby_current_threshold == 100
    assert device.buzzer_mode is BuzzerMode.ONCE
    assert device.screen_language is ScreenLanguage.ENGLISH
    assert device.temperature_unit is TemperatureUnit.CELSIUS
    # screen and Wake-on-LAN settings and Wi-Fi status are answered with
    # notifications
    assert device.send_config.await_args_list == [
        call(0x0C),
        call(0x0E),
        call(0x0A),
        call(0x01),
    ]


async def test_settings_missing_in_the_firmware_are_not_offered(
    device: Device, settings, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
):
    # front-panel firmware before 1.18 has no temperature unit characteristic
    mocker.patch.object(
        device,
        "has_characteristic",
        side_effect=lambda uuid: uuid != TEMPERATURE_UNIT_CHARACTERISTIC_UUID,
    )
    reads: list[str] = []
    read = device.read_value

    async def read_value(characteristic: str) -> bytes:
        reads.append(characteristic)
        return await read(characteristic)

    mocker.patch.object(device, "read_value", side_effect=read_value)

    await device.refresh_settings()

    selects = {c.key for c in get_controls(device, controls.select)}
    assert selects == {"buzzer_mode", "screen_language"}
    assert TEMPERATURE_UNIT_CHARACTERISTIC_UUID not in reads
    assert device.buzzer_mode is BuzzerMode.ONCE
    assert "Could not read setting" not in caplog.text


async def test_select_writes_option_value(device: Device, settings):
    control = next(
        c for c in get_controls(device, controls.select) if c.key == "buzzer_mode"
    )
    state_callback = MagicMock()
    device.subscribe("buzzer_mode", state_callback)

    await control.set_value_func(device, "repeat")

    device.send_command.assert_awaited_once_with(BUZZER_CHARACTERISTIC_UUID, b"\x02")
    assert device.buzzer_mode is BuzzerMode.REPEAT
    state_callback.assert_called_once()


@pytest.mark.parametrize(
    ("read", "field_name"),
    [
        ("_read_adapter", "adapter_voltage"),
        ("_read_adapter", "adapter_current"),
        ("_read_adapter", "power_good_voltage"),
        ("_read_standby", "standby_time"),
        ("_read_standby", "standby_current_threshold"),
        ("_read_temperature_unit", "temperature_unit"),
        ("_read_screen_language", "screen_language"),
        ("_read_buzzer_mode", "buzzer_mode"),
    ],
)
async def test_each_setting_read_reaches_the_listeners(
    device: Device, settings, read: str, field_name: str
):
    listener = MagicMock()
    device.subscribe(field_name, listener)

    # on its own, like the read after a change
    await getattr(device, read)()

    listener.assert_called_once()


async def test_settings_the_power_board_did_not_report_are_unknown(
    device: Device, settings
):
    settings[ADAPTER_CHARACTERISTIC_UUID] = NO_POWER_BOARD_BLOCK
    settings[STANDBY_CHARACTERISTIC_UUID] = NO_POWER_BOARD_BLOCK

    await device.refresh_settings()

    assert (
        device.adapter_voltage,
        device.adapter_current,
        device.power_good_voltage,
    ) == (None, None, None)
    assert (device.standby_time, device.standby_current_threshold) == (None, None)
    # a block written from them would carry the front panel's minimums, e.g. 2 A
    for change, value in (
        (device.set_adapter_voltage, 12.0),
        (device.set_adapter_current, 5.0),
        (device.set_standby_time, 600),
        (device.set_standby_current_threshold, 150),
    ):
        with pytest.raises(SettingUnavailable):
            await change(value)
    device.send_confirmed_command.assert_not_awaited()
    device.send_command.assert_not_awaited()


async def test_power_board_values_are_read_again_once_the_front_panel_has_them(
    device: Device, settings, mocker: MockerFixture
):
    mocker.patch.object(w150, "_POWER_BOARD_REREAD_DELAYS", (0, 0))
    # connected right after the UPS started, before it asked its power board
    device.info_parse(NO_POWER_BOARD_INFO)
    settings[ADAPTER_CHARACTERISTIC_UUID] = NO_POWER_BOARD_BLOCK
    settings[STANDBY_CHARACTERISTIC_UUID] = NO_POWER_BOARD_BLOCK
    read = device.read_value

    async def read_value(characteristic: str) -> bytes:
        if characteristic == INFO_CHARACTERISTIC_UUID:
            # the power board answered meanwhile
            settings[ADAPTER_CHARACTERISTIC_UUID] = ADAPTER_12V_3A
            settings[STANDBY_CHARACTERISTIC_UUID] = STANDBY_300S_100MA
        return await read(characteristic)

    mocker.patch.object(device, "read_value", side_effect=read_value)
    model = MagicMock()
    device.subscribe("power_board_hardware_version", model)

    await device.refresh_settings()

    assert device.device == "W150"
    assert (device.adapter_voltage, device.standby_time) == (12.0, 300)
    model.assert_called_once()


async def test_missing_power_board_versions_are_reported_once(
    device: Device, settings, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
):
    mocker.patch.object(w150, "_POWER_BOARD_REREAD_DELAYS", (0, 0))
    # the power board answered the requests for its settings, not for its versions
    device.info_parse(NO_POWER_BOARD_INFO)
    settings[INFO_CHARACTERISTIC_UUID] = NO_POWER_BOARD_INFO

    await device.refresh_settings()
    await device.refresh_settings()

    reads = [args.args[0] for args in device.read_value.await_args_list]
    assert reads.count(INFO_CHARACTERISTIC_UUID) == 4
    warnings = [r for r in caplog.records if "versions of its power board" in r.message]
    assert len(warnings) == 1
    # the settings it reported can be changed
    await device.set_standby_time(600)
    device.send_command.assert_awaited_once()


async def test_unknown_select_value_reads_as_none(device: Device, settings):
    await device.refresh_settings()
    assert device.screen_language is ScreenLanguage.ENGLISH

    settings[LANGUAGE_CHARACTERISTIC_UUID] = b"\x07" + bytes(14)
    await device.refresh_settings()

    assert device.screen_language is None


def test_adapter_limits_are_derived_like_the_vendor_app():
    settings = AdapterSettings.from_adapter(12.0, 3.0)

    # the app computes 12.0 * 0.958 * 1000 in floating point and truncates to 11495
    assert settings.to_bytes() == struct.pack("<5H", 3000, 2100, 12000, 11580, 11495)


async def test_adapter_voltage_rewrites_block_with_current_rating(
    device: Device, settings
):
    await device.set_adapter_voltage(19.5)

    characteristic, payload, _ = device.send_confirmed_command.await_args.args
    assert characteristic == ADAPTER_CHARACTERISTIC_UUID
    assert struct.unpack("<5H", payload) == (3000, 2100, 19500, 18817, 18681)
    assert device.adapter_voltage == 19.5
    assert device.adapter_current == 3.0
    assert device.power_good_voltage == 18.681
    device.send_command.assert_not_awaited()


async def test_unconfirmed_adapter_write_is_sent_once_more(
    device: Device, settings, results: list[int]
):
    results.append(1)

    await device.set_adapter_voltage(19.5)

    assert device.send_confirmed_command.await_count == 2
    assert device.adapter_voltage == 19.5


async def test_adapter_write_fails_when_the_power_board_does_not_answer(
    device: Device, settings, results: list[int]
):
    results.extend([1, 1])

    with pytest.raises(SettingNotConfirmed):
        await device.set_adapter_voltage(19.5)

    # the front panel would report the new block, the value read before stays
    assert device.send_confirmed_command.await_count == 2
    assert device.adapter_voltage == 12.0


async def test_adapter_write_fails_without_a_result(
    device: Device, settings, mocker: MockerFixture
):
    mocker.patch.object(device, "send_confirmed_command", side_effect=TimeoutError)

    with pytest.raises(SettingNotConfirmed):
        await device.set_adapter_current(5.0)

    assert device.adapter_current == 3.0


async def test_standby_change_fails_when_current_values_are_unreadable(
    device: Device, settings
):
    settings[STANDBY_CHARACTERISTIC_UUID] = b""

    with pytest.raises(SettingUnavailable):
        await device.set_standby_time(120)

    device.send_command.assert_not_awaited()


async def test_number_control_clamps_to_device_range(device: Device, settings):
    control = next(
        c for c in get_controls(device, controls.NumberType) if c.key == "standby_time"
    )

    await control.set_value_func(device, 5)
    assert device.send_command.await_args.args[1] == struct.pack("<2H", 20, 100)

    await control.set_value_func(device, 9000)
    assert device.send_command.await_args.args[1] == struct.pack("<2H", 7200, 100)


@pytest.mark.parametrize(
    ("info", "written"),
    [
        # power-board firmware 1.27 keeps its threshold above 2000 mA
        ("00 0300 1b00 0300 1300", 2000),
        ("00 0300 1d00 0300 1300", 2500),
        # versions not reported
        ("00 0000 0000 0300 1300", 2500),
    ],
)
async def test_standby_threshold_fits_the_power_board_firmware(
    device: Device, settings, info: str, written: int
):
    device.info_parse(bytes.fromhex(info) + bytes(6))

    await device.set_standby_current_threshold(2500)

    assert settings[STANDBY_CHARACTERISTIC_UUID][:4] == struct.pack("<2H", 300, written)


async def test_screen_timeout_is_set_over_the_config_channel(device: Device):
    device.send_config = AsyncMock()

    await device.set_screen_timeout(600)

    assert device.send_config.await_args_list == [
        call(0x0B, struct.pack("<I", 600)),
        call(0x0C),
    ]


async def test_screen_reply_updates_timeout_and_always_on(device: Device):
    assert await device.config_parse(screen_reply(600)) is True
    assert device.screen_timeout == 600
    assert device.screen_always_on is False

    await device.config_parse(screen_reply(SCREEN_ALWAYS_ON))
    assert device.screen_timeout is None
    assert device.screen_always_on is True


async def test_turning_always_on_off_restores_last_timeout(device: Device):
    device.send_config = AsyncMock()
    await device.config_parse(screen_reply(900))
    await device.config_parse(screen_reply(SCREEN_ALWAYS_ON))

    await device.enable_screen_always_on(False)

    assert device.send_config.await_args_list[0] == call(0x0B, struct.pack("<I", 900))


async def test_screen_reply_updates_idle_brightness(device: Device):
    # a higher backlight level dims the screen
    await device.config_parse(screen_reply(600, idle_backlight=30))

    assert device.screen_idle_brightness == 70


async def test_screen_brightness_is_set_as_backlight_level(device: Device):
    device.send_config = AsyncMock()

    await device.set_screen_brightness(55)
    await device.set_screen_brightness(95)

    assert device.send_config.await_args_list == [
        call(0x0D, bytes([45])),
        call(0x0E),
        # the lowest level the front panel keeps
        call(0x0D, bytes([20])),
        call(0x0E),
    ]


async def test_brightness_reply_updates_brightness(device: Device):
    message = ConfigMessage(message_type=0x0E, token=0, payload=bytes([80]))

    assert await device.config_parse(message) is True
    assert device.screen_brightness == 20


async def test_idle_brightness_is_written_with_the_current_timeout(
    device: Device, screen
):
    await device.config_parse(screen_reply(600))
    # changed on the device while HA stays connected
    screen["timeout"] = 900

    await device.set_screen_idle_brightness(40)
    await asyncio.sleep(0)

    assert screen == {"timeout": 900, "idle": 60}
    assert device.screen_timeout == 900
    assert device.screen_idle_brightness == 40


async def test_idle_brightness_keeps_the_screen_always_on(device: Device, screen):
    screen["timeout"] = SCREEN_ALWAYS_ON

    await device.set_screen_idle_brightness(0)

    assert screen == {"timeout": SCREEN_ALWAYS_ON, "idle": 100}


async def test_idle_brightness_fails_when_the_timeout_is_not_answered(
    device: Device, mocker: MockerFixture
):
    mocker.patch.object(w150, "_CONFIG_REPLY_TIMEOUT", 0.01)
    device.send_config = AsyncMock()

    with pytest.raises(TimeoutError):
        await device.set_screen_idle_brightness(40)

    device.send_config.assert_awaited_once_with(0x0C)


async def test_wake_on_lan_reply_updates_the_trigger(device: Device):
    assert await device.config_parse(wake_on_lan_reply(60, 120, 50)) is True

    assert device.wake_on_lan_min_outage == 60
    assert device.wake_on_lan_delay == 120
    assert device.wake_on_lan_min_battery == 50


async def test_wake_on_lan_change_keeps_the_other_values(device: Device, wake_on_lan):
    await device.config_parse(wake_on_lan_reply())
    # changed by another client while HA stays connected
    wake_on_lan["min_outage"] = 60

    await device.set_wake_on_lan_delay(120)
    await asyncio.sleep(0)

    assert wake_on_lan == {"min_outage": 60, "delay": 120, "min_battery": 35}
    assert (device.wake_on_lan_min_outage, device.wake_on_lan_delay) == (60, 120)


@pytest.mark.parametrize(
    ("setter", "value", "written"),
    [
        ("set_wake_on_lan_min_outage", 5, {"min_outage": 10}),
        ("set_wake_on_lan_delay", 70_000, {"delay": 0xFFFF}),
        ("set_wake_on_lan_min_battery", 90, {"min_battery": 80}),
        ("set_wake_on_lan_min_battery", 10, {"min_battery": 20}),
    ],
)
async def test_wake_on_lan_values_fit_the_front_panel_limits(
    device: Device, wake_on_lan, setter: str, value: int, written: dict[str, int]
):
    defaults = dict(wake_on_lan)

    await getattr(device, setter)(value)

    assert wake_on_lan == defaults | written


async def test_wake_on_lan_change_fails_when_the_trigger_is_not_answered(
    device: Device, mocker: MockerFixture
):
    mocker.patch.object(w150, "_CONFIG_REPLY_TIMEOUT", 0.01)
    device.send_config = AsyncMock()

    with pytest.raises(TimeoutError):
        await device.set_wake_on_lan_min_battery(50)

    device.send_config.assert_awaited_once_with(0x0A)


async def test_wifi_status_reply_updates_network_fields(device: Device):
    assert await device.config_parse(wifi_reply()) is True

    assert device.wifi_connected is True
    assert device.wifi_rssi == -61
    assert device.wifi_ip_address == "192.0.2.23"
    assert device.wifi_ssid == "HomeAP"


async def test_disconnected_wifi_status_clears_network_fields(device: Device):
    await device.config_parse(wifi_reply())
    # the firmware leaves the SSID length uninitialized while disconnected
    message = ConfigMessage(message_type=0x01, token=0, payload=bytes(14) + b"\x9c")

    await device.config_parse(message)

    assert device.wifi_connected is False
    assert device.wifi_rssi is None
    assert device.wifi_ip_address is None
    assert device.wifi_ssid is None


@pytest.mark.parametrize(
    ("reply", "field_name"),
    [
        (screen_reply(600), "screen_timeout"),
        (screen_reply(SCREEN_ALWAYS_ON), "screen_always_on"),
        (screen_reply(600), "screen_idle_brightness"),
        (
            ConfigMessage(message_type=0x0E, token=0, payload=b"\x50"),
            "screen_brightness",
        ),
        (wake_on_lan_reply(), "wake_on_lan_min_outage"),
        (wake_on_lan_reply(), "wake_on_lan_delay"),
        (wake_on_lan_reply(), "wake_on_lan_min_battery"),
        (wifi_reply(), "wifi_connected"),
        (wifi_reply(), "wifi_rssi"),
        (wifi_reply(), "wifi_ssid"),
        (wifi_reply(), "wifi_ip_address"),
    ],
)
async def test_each_config_reply_reaches_the_listeners(
    device: Device, reply: ConfigMessage, field_name: str
):
    listener = MagicMock()
    device.subscribe(field_name, listener)

    await device.config_parse(reply)

    listener.assert_called_once()


async def test_ignores_other_config_messages(device: Device):
    # the Wake-on-LAN targets
    message = ConfigMessage(message_type=0x08, token=0, payload=b"\x00")

    assert await device.config_parse(message) is False


async def test_concurrent_changes_of_one_block_keep_both_values(
    device: Device, settings
):
    await asyncio.gather(
        device.set_standby_time(600), device.set_standby_current_threshold(250)
    )

    assert settings[STANDBY_CHARACTERISTIC_UUID][:4] == struct.pack("<2H", 600, 250)
    assert (device.standby_time, device.standby_current_threshold) == (600, 250)


async def test_block_change_keeps_values_changed_on_the_device(
    device: Device, settings
):
    await device.refresh_settings()
    # changed in the vendor app while HA stays connected
    settings[STANDBY_CHARACTERISTIC_UUID] = struct.pack("<2H", 300, 500) + bytes(11)

    await device.set_standby_time(120)

    # written once, with just the two values of the block
    device.send_command.assert_awaited_once_with(
        STANDBY_CHARACTERISTIC_UUID, struct.pack("<2H", 120, 500)
    )


def test_select_control_type_can_be_subscripted():
    assert controls.select[BuzzerMode] is not None


@pytest.mark.parametrize(
    "error",
    [
        # the characteristic is missing in this firmware version
        UnsupportedBluetoothProtocol(ADAPTER_CHARACTERISTIC_UUID, []),
        BleakError("read failed"),
    ],
)
async def test_refresh_continues_after_a_failed_read_while_connected(
    device: Device, settings, mocker: MockerFixture, error: Exception
):
    mocker.patch.object(
        Device, "is_connected", new_callable=PropertyMock, return_value=True
    )
    read = device.read_value

    async def read_value(characteristic: str) -> bytes:
        if characteristic == ADAPTER_CHARACTERISTIC_UUID:
            raise error
        return await read(characteristic)

    mocker.patch.object(device, "read_value", side_effect=read_value)

    await device.refresh_settings()

    assert device.adapter_voltage is None
    assert device.buzzer_mode is BuzzerMode.ONCE


async def test_refresh_stops_when_the_connection_is_lost(
    device: Device, settings, mocker: MockerFixture
):
    mocker.patch.object(
        Device, "is_connected", new_callable=PropertyMock, return_value=False
    )
    mocker.patch.object(device, "read_value", side_effect=BleakError("disconnected"))

    await device.refresh_settings()

    assert device.read_value.await_count == 1
    device.send_config.assert_not_awaited()
