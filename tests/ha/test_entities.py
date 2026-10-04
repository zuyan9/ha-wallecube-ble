"""Entity behavior that needs Home Assistant installed"""

import pytest

# a Home Assistant that fails to import its dependencies skips these tests too
pytest.importorskip("homeassistant.components.bluetooth", exc_type=ImportError)

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock

from homeassistant.components.number import NumberMode
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from pytest_mock import MockerFixture

from custom_components.wallecube_ups_ble.binary_sensor import (
    BINARY_SENSOR_TYPES,
    WalleCubeBinarySensor,
)
from custom_components.wallecube_ups_ble.const import DOMAIN
from custom_components.wallecube_ups_ble.event import EVENT_TYPES, WalleCubeEvent
from custom_components.wallecube_ups_ble.number import WalleCubeNumber
from custom_components.wallecube_ups_ble.select import WalleCubeSelect
from custom_components.wallecube_ups_ble.sensor import SENSOR_TYPES, WalleCubeSensor
from custom_components.wallecube_ups_ble.switch import WalleCubeSwitch
from custom_components.wallecube_ups_ble.wclib import controls, get_controls
from custom_components.wallecube_ups_ble.wclib.devices.w150 import (
    BuzzerMode,
    Device,
    PowerEvent,
)
from custom_components.wallecube_ups_ble.wclib.exceptions import SettingNotConfirmed
from tests.fakes import W150_INFO, telemetry_frame


def control(device: Device, control_type: type[controls.ControlType], key: str):
    return next(c for c in get_controls(device, control_type) if c.key == key)


def publish(device: Device, field_name: str, value) -> None:
    setattr(device, field_name, value)
    device._publish_updates()


def added_number(device: Device, key: str) -> WalleCubeNumber:
    """Number entity as Home Assistant sets it up for a config entry"""
    number = WalleCubeNumber(device, control(device, controls.NumberType, key))
    number.hass = MagicMock()
    number.platform = MagicMock(config_entry=SimpleNamespace(entry_id="entry_id"))
    return number


@pytest.fixture
def create_issue(mocker: MockerFixture) -> MagicMock:
    return mocker.patch.object(ir, "async_create_issue")


def test_entities_are_not_polled(device: Device):
    entities = [
        WalleCubeSensor(device, SENSOR_TYPES["battery_level"]),
        WalleCubeBinarySensor(device, BINARY_SENSOR_TYPES["charging"]),
        WalleCubeSelect(device, control(device, controls.select, "buzzer_mode")),
        WalleCubeNumber(device, control(device, controls.NumberType, "standby_time")),
        WalleCubeSwitch(device, control(device, controls.switch, "screen_always_on")),
        WalleCubeEvent(device, EVENT_TYPES["power_event"]),
    ]

    assert [entity.should_poll for entity in entities] == [False] * len(entities)


async def test_value_published_before_subscription_is_not_lost(device: Device):
    select = WalleCubeSelect(device, control(device, controls.select, "buzzer_mode"))
    binary_sensor = WalleCubeBinarySensor(device, BINARY_SENSOR_TYPES["charging"])

    # the settings read after connecting can finish before HA adds the entities
    publish(device, "buzzer_mode", BuzzerMode.REPEAT)
    # status flag bit 7 (charging) in an otherwise empty telemetry frame
    await device.data_parse(telemetry_frame(status_flags=1 << 7))
    await select.async_added_to_hass()
    await binary_sensor.async_added_to_hass()

    assert select.current_option == "repeat"
    assert binary_sensor.is_on is True


async def test_updates_after_subscription_reach_the_entity(device: Device):
    number = WalleCubeNumber(
        device, control(device, controls.NumberType, "standby_time")
    )
    number.async_write_ha_state = MagicMock()
    await number.async_added_to_hass()

    publish(device, "standby_time", 120)

    assert number.native_value == 120
    number.async_write_ha_state.assert_called_once()


async def test_telemetry_entities_are_unavailable_while_telemetry_is_stale(
    device: Device, mocker: MockerFixture
):
    mocker.patch.object(
        Device, "is_connected", new_callable=PropertyMock, return_value=True
    )
    level = WalleCubeSensor(device, SENSOR_TYPES["battery_level"])
    charging = WalleCubeBinarySensor(device, BINARY_SENSOR_TYPES["charging"])
    standby = WalleCubeNumber(
        device, control(device, controls.NumberType, "standby_time")
    )
    event = WalleCubeEvent(device, EVENT_TYPES["power_event"])
    entities = (level, charging, standby, event)
    for entity in entities:
        entity.async_write_ha_state = MagicMock()
        await entity.async_added_to_hass()
    await device._on_data(telemetry_frame())
    level.async_write_ha_state.reset_mock()
    charging.async_write_ha_state.reset_mock()

    device._check_data(device._last_data + 61)

    # settings stay usable, and the event keeps its last state
    assert [entity.available for entity in entities] == [False, False, True, True]
    level.async_write_ha_state.assert_called_once()
    charging.async_write_ha_state.assert_called_once()

    await device._on_data(telemetry_frame())

    assert all(entity.available for entity in entities)
    assert level.async_write_ha_state.call_count == 2


@pytest.mark.parametrize(
    ("key", "category"),
    [
        ("shutdown_imminent", None),
        ("overload", None),
        ("battery_fault", EntityCategory.DIAGNOSTIC),
        ("input_over_voltage", EntityCategory.DIAGNOSTIC),
    ],
)
def test_shutdown_signals_are_primary_entities(
    device: Device, key: str, category: EntityCategory | None
):
    assert (
        WalleCubeBinarySensor(device, BINARY_SENSOR_TYPES[key]).entity_category
        is category
    )


def test_power_event_types_match_the_device_events():
    assert set(EVENT_TYPES["power_event"].event_types) == {
        controls.option_name(event) for event in PowerEvent
    }


async def test_power_event_fires_on_the_event_byte(device: Device):
    event = WalleCubeEvent(device, EVENT_TYPES["power_event"])
    event.async_write_ha_state = MagicMock()
    await event.async_added_to_hass()

    # event byte 2 in an otherwise empty telemetry frame
    await device.data_parse(telemetry_frame(event=2))
    await device.data_parse(telemetry_frame())

    assert event.state_attributes["event_type"] == "power_lost"
    event.async_write_ha_state.assert_called_once()


async def test_last_power_event_is_not_fired_again_when_added(device: Device):
    await device.data_parse(telemetry_frame(event=1))
    event = WalleCubeEvent(device, EVENT_TYPES["power_event"])
    event.async_write_ha_state = MagicMock()

    await event.async_added_to_hass()

    assert event.state is None
    event.async_write_ha_state.assert_not_called()


def test_device_info_names_the_model_and_the_front_panel_versions(device: Device):
    # a W180 power board, hardware 4 and firmware 29, behind front panel 3 and 20
    device.info_parse(bytes.fromhex("00 0400 1d00 0300 1400") + bytes(6))

    info = WalleCubeSensor(device, SENSOR_TYPES["battery_level"]).device_info

    assert info["model"] == "W180"
    assert info["name"] == "W180-4E52"
    # firmware as the vendor app shows it
    assert info["sw_version"] == "1.20"
    assert info["hw_version"] == "3"


def test_power_board_firmware_reads_like_the_vendor_app(device: Device):
    device.info_parse(bytes.fromhex("00 0300 1d00 0300 1300") + bytes(6))

    firmware = WalleCubeSensor(device, SENSOR_TYPES["power_board_firmware_version"])
    hardware = WalleCubeSensor(device, SENSOR_TYPES["power_board_hardware_version"])

    assert firmware.native_value == "1.29"
    assert hardware.native_value == 3


def test_brightness_is_a_percentage_slider(device: Device):
    number = WalleCubeNumber(
        device, control(device, controls.NumberType, "screen_brightness")
    )

    assert number.native_unit_of_measurement == PERCENTAGE
    assert number.mode is NumberMode.SLIDER
    assert (number.native_min_value, number.native_max_value) == (0, 80)


async def test_removed_entity_is_no_longer_written(device: Device):
    number = WalleCubeNumber(
        device, control(device, controls.NumberType, "standby_time")
    )
    number.async_write_ha_state = MagicMock()
    await number.async_added_to_hass()

    # what HA runs when it removes the entity
    number._call_on_remove_callbacks()
    publish(device, "standby_time", 120)

    number.async_write_ha_state.assert_not_called()


async def test_only_sensors_follow_the_update_period(
    device: Device, mocker: MockerFixture
):
    clock = mocker.patch(
        "custom_components.wallecube_ups_ble.wclib.devicebase.time.time"
    )
    device.with_update_period(10)
    level = WalleCubeSensor(device, SENSOR_TYPES["battery_level"])
    input_power = WalleCubeBinarySensor(device, BINARY_SENSOR_TYPES["input_power_ok"])
    event = WalleCubeEvent(device, EVENT_TYPES["power_event"])
    for entity in (level, input_power, event):
        entity.async_write_ha_state = MagicMock()
        await entity.async_added_to_hass()

    # (time, input power, power event); the battery level changes with every frame
    # and alone in the one that ends the first seconds, which starts the update period
    frames = [(1000, 0, 0), (1001, 1, 0), (1007, 1, 0), (1008, 0, 2), (1009, 1, 0)]
    for i, (at, input_power_bit, event_byte) in enumerate(frames):
        clock.return_value = at
        await device.data_parse(
            telemetry_frame(
                battery_permille=500 + i,
                status_flags=input_power_bit << 10,
                event=event_byte,
            )
        )

    # the sensor holds the changes within the update period, the others do not
    assert level.async_write_ha_state.call_count == 3
    assert input_power.async_write_ha_state.call_count == 4
    # the power event lasts one frame, it must not wait for the next period
    event.async_write_ha_state.assert_called_once()


def test_entity_names_fall_back_to_the_key(device: Device):
    sensor = WalleCubeSensor(device, SENSOR_TYPES["battery_level"])
    number = WalleCubeNumber(
        device, control(device, controls.NumberType, "standby_time")
    )

    assert sensor.translation_key == "battery_level"
    assert number.translation_key == "standby_time"


def test_cell_voltages_share_one_translation(device: Device):
    sensor = WalleCubeSensor(device, SENSOR_TYPES["cell_voltage_2"])

    assert sensor.unique_id == f"wc_{device.identifier}_cell_voltage_2"
    assert sensor.translation_key == "cell_voltage"
    assert sensor.entity_description.translation_placeholders == {"n": "2"}


async def test_adapter_change_asks_for_a_restart(
    device: Device, create_issue: MagicMock
):
    device.info_parse(W150_INFO + bytes(6))
    publish(device, "adapter_voltage", 12.0)
    publish(device, "adapter_current", 3.0)
    publish(device, "power_good_voltage", 11.496)
    number = added_number(device, "adapter_voltage")

    async def write(device: Device, volts: float) -> None:
        # the values the UPS reports after a confirmed write
        publish(device, "adapter_voltage", volts)
        publish(device, "power_good_voltage", 18.681)

    number._set_value = AsyncMock(side_effect=write)
    await number.async_set_native_value(19.5)

    create_issue.assert_called_once_with(
        number.hass,
        DOMAIN,
        "restart_required_entry_id",
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="restart_required",
        # the diagnostics include the issue, so it names the model, not the device
        translation_placeholders={
            "model": "W150",
            "voltage": "19.5",
            "current": "3",
            "power_off": "18.7",
        },
    )


async def test_unchanged_adapter_setting_needs_no_restart(
    device: Device, create_issue: MagicMock
):
    publish(device, "adapter_voltage", 12.0)
    number = added_number(device, "adapter_voltage")
    number._set_value = AsyncMock()

    await number.async_set_native_value(12.0)

    create_issue.assert_not_called()


async def test_failed_adapter_change_needs_no_restart(
    device: Device, create_issue: MagicMock
):
    publish(device, "adapter_voltage", 12.0)
    number = added_number(device, "adapter_voltage")
    number._set_value = AsyncMock(side_effect=SettingNotConfirmed("no answer"))

    with pytest.raises(HomeAssistantError):
        await number.async_set_native_value(19.5)

    create_issue.assert_not_called()


async def test_other_settings_apply_without_a_restart(
    device: Device, create_issue: MagicMock
):
    publish(device, "standby_time", 300)
    number = added_number(device, "standby_time")
    number._set_value = AsyncMock(
        side_effect=lambda device, seconds: publish(device, "standby_time", seconds)
    )

    await number.async_set_native_value(600)

    assert number.native_value == 600
    create_issue.assert_not_called()
