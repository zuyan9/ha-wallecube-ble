"""
Entity behavior that needs Home Assistant installed

Run with the Home Assistant dependencies available, e.g.
`uv run --with aiohasupervisor --with serialx pytest tests/ha`.
"""

import pytest

pytest.importorskip("homeassistant.components.bluetooth")

from unittest.mock import MagicMock

from homeassistant.components.number import NumberMode
from homeassistant.const import PERCENTAGE

from custom_components.wallecube_ble.binary_sensor import WalleCubeBinarySensor
from custom_components.wallecube_ble.event import EVENT_TYPES, WalleCubeEvent
from custom_components.wallecube_ble.number import WalleCubeNumber
from custom_components.wallecube_ble.number import _describe as describe_number
from custom_components.wallecube_ble.select import WalleCubeSelect
from custom_components.wallecube_ble.select import _describe as describe_select
from custom_components.wallecube_ble.sensor import WalleCubeSensor
from custom_components.wallecube_ble.switch import WalleCubeSwitch
from custom_components.wallecube_ble.switch import _describe as describe_switch
from custom_components.wallecube_ble.wclib import controls, get_controls
from custom_components.wallecube_ble.wclib.devices.w150 import (
    BuzzerMode,
    Device,
    PowerEvent,
)


@pytest.fixture
def device():
    adv = MagicMock(local_name="Walle-8856A600C4BC", service_uuids=[])
    ble_dev = MagicMock(address="88:56:A6:00:C4:BE")
    ble_dev.name = adv.local_name
    return Device(ble_dev, adv)


def control(device: Device, control_type: type[controls.ControlType], key: str):
    return next(c for c in get_controls(device, control_type) if c.key == key)


def publish(device: Device, field_name: str, value) -> None:
    setattr(device, field_name, value)
    device._publish_updates()


def test_entities_are_not_polled(device: Device):
    entities = [
        WalleCubeSensor(device, "battery_level"),
        WalleCubeBinarySensor(device, "charging"),
        WalleCubeSelect(
            device, describe_select(control(device, controls.select, "buzzer_mode"))
        ),
        WalleCubeNumber(
            device,
            describe_number(control(device, controls.NumberType, "standby_time")),
        ),
        WalleCubeSwitch(
            device,
            describe_switch(control(device, controls.switch, "screen_always_on")),
        ),
        WalleCubeEvent(device, "power_event"),
    ]

    assert [entity.should_poll for entity in entities] == [False] * len(entities)


async def test_value_published_before_subscription_is_not_lost(device: Device):
    select = WalleCubeSelect(
        device, describe_select(control(device, controls.select, "buzzer_mode"))
    )
    binary_sensor = WalleCubeBinarySensor(device, "charging")

    # the settings read after connecting can finish before HA adds the entities
    publish(device, "buzzer_mode", BuzzerMode.REPEAT)
    # status flag bit 7 (charging) in an otherwise empty telemetry frame
    await device.data_parse(b"\x51\x00" + bytes(36) + (1 << 7).to_bytes(2, "little"))
    await select.async_added_to_hass()
    await binary_sensor.async_added_to_hass()

    assert select.current_option == "repeat"
    assert binary_sensor.is_on is True


async def test_updates_after_subscription_reach_the_entity(device: Device):
    number = WalleCubeNumber(
        device, describe_number(control(device, controls.NumberType, "standby_time"))
    )
    number.async_write_ha_state = MagicMock()
    await number.async_added_to_hass()

    publish(device, "standby_time", 120)

    assert number.native_value == 120
    number.async_write_ha_state.assert_called_once()


def test_power_event_types_match_the_device_events():
    assert set(EVENT_TYPES["power_event"].event_types) == {
        controls.option_name(event) for event in PowerEvent
    }


async def test_power_event_fires_on_the_event_byte(device: Device):
    event = WalleCubeEvent(device, "power_event")
    event.async_write_ha_state = MagicMock()
    await event.async_added_to_hass()

    # event byte 2 in an otherwise empty telemetry frame
    await device.data_parse(b"\x51\x02" + bytes(38))
    await device.data_parse(b"\x51\x00" + bytes(38))

    assert event.state_attributes["event_type"] == "power_lost"
    event.async_write_ha_state.assert_called_once()


async def test_last_power_event_is_not_fired_again_when_added(device: Device):
    await device.data_parse(b"\x51\x01" + bytes(38))
    event = WalleCubeEvent(device, "power_event")
    event.async_write_ha_state = MagicMock()

    await event.async_added_to_hass()

    assert event.state is None
    event.async_write_ha_state.assert_not_called()


def test_device_info_carries_front_panel_versions(device: Device):
    device.info_parse(bytes.fromhex("00 0300 1d00 0300 1300") + bytes(6))

    info = WalleCubeSensor(device, "power_board_firmware_version").device_info

    assert info["sw_version"] == "19"
    assert info["hw_version"] == "3"


def test_brightness_is_a_percentage_slider(device: Device):
    number = WalleCubeNumber(
        device,
        describe_number(control(device, controls.NumberType, "screen_brightness")),
    )

    assert number.native_unit_of_measurement == PERCENTAGE
    assert number.mode is NumberMode.SLIDER
    assert (number.native_min_value, number.native_max_value) == (20, 100)


def test_cell_voltages_share_one_translation(device: Device):
    sensor = WalleCubeSensor(device, "cell_voltage_2")

    assert sensor.unique_id == f"wc_{device.identifier}_cell_voltage_2"
    assert sensor.translation_key == "cell_voltage"
    assert sensor.entity_description.translation_placeholders == {"n": "2"}
