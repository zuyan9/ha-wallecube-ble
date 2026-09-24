import asyncio
import enum
import struct
from collections import defaultdict

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError

from .. import controls
from ..connection import (
    ADAPTER_CHARACTERISTIC_UUID,
    BUZZER_CHARACTERISTIC_UUID,
    LANGUAGE_CHARACTERISTIC_UUID,
    STANDBY_CHARACTERISTIC_UUID,
    TEMPERATURE_UNIT_CHARACTERISTIC_UUID,
    UPS_SERVICE_UUID,
)
from ..devicebase import DeviceBase
from ..encryption import LOCAL_NAME_PREFIX
from ..exceptions import (
    PacketParseError,
    SettingUnavailable,
    UnsupportedBluetoothProtocol,
)
from ..model import (
    AdapterSettings,
    InfoBlock,
    StandbySettings,
    UpsTelemetry,
    WifiStatus,
)
from ..model.wifi_status import ipv4
from ..packet import ConfigMessage, TelemetryFrame
from ..props import Field, dataclass_attr_mapper, raw_field
from ..props.raw_data_props import RawDataProps
from ..props.transforms import pdiv, prop_has_bit_on

tele = dataclass_attr_mapper(UpsTelemetry)
info = dataclass_attr_mapper(InfoBlock)

# set while the output is powered from the battery, the firmware only shows the
# remaining time in that state
_on_battery = prop_has_bit_on(8)


def _known_version(value: int | None) -> int | None:
    # 0 means the power board did not report its versions
    return value or None


class BuzzerMode(enum.IntEnum):
    MUTE = 0
    ONCE = 1
    REPEAT = 2


class ScreenLanguage(enum.IntEnum):
    ENGLISH = 0
    CHINESE = 1


class TemperatureUnit(enum.IntEnum):
    CELSIUS = 0
    FAHRENHEIT = 1


class PowerEvent(enum.IntEnum):
    """Event byte of a telemetry frame"""

    # the vendor app names the values the other way round, but the firmware sends 2
    # when it starts the outage timers and 1 when it starts the power-return delay
    POWER_RESTORED = 1
    POWER_LOST = 2


# configuration-channel message types
_GET_WIFI_STATUS = 0x01
_SET_SCREEN_TIMEOUT = 0x0B
_GET_SCREEN_TIMEOUT = 0x0C
_SET_SCREEN_BRIGHTNESS = 0x0D
_GET_SCREEN_BRIGHTNESS = 0x0E

# seconds to wait for the notification that answers a configuration request
_CONFIG_REPLY_TIMEOUT = 5

# screen timeout the vendor app writes for "keep screen on"
SCREEN_ALWAYS_ON = 0xFFFFFFFF
DEFAULT_SCREEN_TIMEOUT = 300


class Device(DeviceBase, RawDataProps):
    """W150"""

    NAME_PREFIX = "W150-"
    # the device reports Wi-Fi changes only when asked
    POLL_INTERVAL = 60

    battery_level = raw_field(tele.battery_level, pdiv(10, 1))
    battery_voltage = raw_field(tele.battery_voltage, pdiv(1000, 3))
    battery_current = raw_field(tele.battery_current, pdiv(1000, 3))
    dc_input_voltage = raw_field(tele.dc_input_voltage, pdiv(1000, 3))
    dc_input_current = raw_field(tele.dc_input_current, pdiv(1000, 3))
    dc_output_voltage = raw_field(tele.dc_output_voltage, pdiv(1000, 3))
    dc_output_current = raw_field(tele.dc_output_current, pdiv(1000, 3))
    temperature = raw_field(tele.temperature, pdiv(10, 1))
    energy_total = raw_field(tele.energy_total, pdiv(1_000_000, 3))
    status_flags = raw_field(tele.status_flags)

    # status flag names follow the vendor app
    overload = raw_field(tele.status_flags, prop_has_bit_on(2))
    shutdown_imminent = raw_field(tele.status_flags, prop_has_bit_on(4))
    charging = raw_field(tele.status_flags, prop_has_bit_on(7))
    discharging = raw_field(tele.status_flags, _on_battery)
    input_power_ok = raw_field(tele.status_flags, prop_has_bit_on(10))

    output_power = Field[float]()
    remaining_time_discharging = Field[int]()
    power_event = Field[PowerEvent]()

    hardware_version = raw_field(info.hardware_version, _known_version)
    firmware_version = raw_field(info.firmware_version, _known_version)
    power_board_hardware_version = raw_field(
        info.power_board_hardware_version, _known_version
    )
    power_board_firmware_version = raw_field(
        info.power_board_firmware_version, _known_version
    )

    wifi_connected = Field[bool]()
    wifi_rssi = Field[int]()
    wifi_ssid = Field[str]()
    wifi_ip_address = Field[str]()

    # settings, in the order of the vendor app's advanced configuration page
    adapter_voltage = Field[float]()
    adapter_current = Field[float]()
    standby_time = Field[int]()
    standby_current_threshold = Field[int]()
    screen_timeout = Field[int]()
    screen_always_on = Field[bool]()
    temperature_unit = Field[TemperatureUnit]()
    screen_language = Field[ScreenLanguage]()
    buzzer_mode = Field[BuzzerMode]()
    # not in the vendor app
    screen_brightness = Field[int]()
    screen_idle_brightness = Field[int]()

    _warned_truncated = False
    _last_screen_timeout = DEFAULT_SCREEN_TIMEOUT
    # as reported, including the value for "keep screen on"
    _raw_screen_timeout: int | None = None

    def __init__(self, ble_dev: BLEDevice, adv_data: AdvertisementData) -> None:
        super().__init__(ble_dev, adv_data)
        # adapter and standby values are written as whole blocks, so changes of two
        # values in one block must not interleave or one of them is lost
        self._adapter_lock = asyncio.Lock()
        self._standby_lock = asyncio.Lock()
        self._screen_lock = asyncio.Lock()
        self._config_replies: dict[int, list[asyncio.Future[None]]] = defaultdict(list)

    @classmethod
    def check(cls, adv_data: AdvertisementData) -> bool:
        local_name = adv_data.local_name or ""
        return (
            local_name.startswith(LOCAL_NAME_PREFIX)
            or UPS_SERVICE_UUID in adv_data.service_uuids
        )

    async def data_parse(self, frame: bytes) -> bool:
        self.reset_updated()

        telemetry_frame = TelemetryFrame.from_bytes(frame)
        if telemetry_frame.truncated and not self._warned_truncated:
            self._warned_truncated = True
            self._logger.warning(
                "Telemetry frame truncated to %d bytes, the connection MTU is too "
                "small to receive all fields",
                len(frame),
            )
        telemetry = self.update_from_bytes(UpsTelemetry, telemetry_frame.payload)

        if self.dc_output_voltage is not None and self.dc_output_current is not None:
            self.output_power = round(
                self.dc_output_voltage * self.dc_output_current, 2
            )

        self.remaining_time_discharging = (
            round(telemetry.remaining_time / 60)
            if telemetry.remaining_time is not None
            and _on_battery(telemetry.status_flags)
            else None
        )
        self.power_event = _power_event(telemetry_frame.event)

        self._publish_updates()
        return True

    def info_parse(self, info: bytes) -> None:
        self.update_from_bytes(InfoBlock, info)
        self._publish_updates()

    async def config_parse(self, message: ConfigMessage) -> bool:
        parse = {
            _GET_WIFI_STATUS: self._parse_wifi_status,
            _GET_SCREEN_TIMEOUT: self._parse_screen_timeout,
            _GET_SCREEN_BRIGHTNESS: self._parse_screen_brightness,
        }.get(message.message_type)
        if parse is None or not parse(message.payload):
            return False

        self._publish_updates()
        for reply in self._config_replies.get(message.message_type, []):
            if not reply.done():
                reply.set_result(None)
        return True

    def _parse_wifi_status(self, payload: bytes) -> bool:
        status = WifiStatus.from_bytes(payload)
        if status.connected is None:
            return False

        self.wifi_connected = bool(status.connected)
        if not status.connected:
            self.wifi_rssi = None
            self.wifi_ssid = None
            self.wifi_ip_address = None
            return True

        self.wifi_rssi = status.rssi
        self.wifi_ip_address = ipv4(status.ip_address)
        ssid = payload[WifiStatus.SIZE : WifiStatus.SIZE + (status.ssid_length or 0)]
        self.wifi_ssid = ssid.decode("utf-8", "replace") if ssid else None
        return True

    def _parse_screen_timeout(self, payload: bytes) -> bool:
        if len(payload) < 4:
            return False

        timeout = int.from_bytes(payload[:4], "little")
        self._raw_screen_timeout = timeout
        self.screen_always_on = timeout == SCREEN_ALWAYS_ON
        if timeout == SCREEN_ALWAYS_ON:
            self.screen_timeout = None
        else:
            self.screen_timeout = timeout
            self._last_screen_timeout = timeout
        if len(payload) >= 5:
            self.screen_idle_brightness = payload[4]
        return True

    def _parse_screen_brightness(self, payload: bytes) -> bool:
        if not payload:
            return False
        self.screen_brightness = payload[0]
        return True

    async def refresh_settings(self) -> None:
        readers = (
            self._read_adapter,
            self._read_standby,
            self._read_temperature_unit,
            self._read_screen_language,
            self._read_buzzer_mode,
            self._request_screen_timeout,
            self._request_screen_brightness,
            self._request_wifi_status,
        )
        for read in readers:
            # older firmware lacks some settings and a single read can fail, the
            # others are still usable
            try:
                await read()
            except (UnsupportedBluetoothProtocol, PacketParseError) as e:
                self._logger.warning("Could not read setting: %s", e)
            except (BleakError, TimeoutError) as e:
                if not self.is_connected:
                    self._logger.debug("Connection lost while reading settings")
                    return
                self._logger.warning("Could not read setting: %s", e)

    async def poll(self) -> None:
        await self._request_wifi_status()

    @controls.voltage(adapter_voltage, min=5.0, max=20.2, step=0.1, enabled=False)
    async def set_adapter_voltage(self, volts: float) -> None:
        await self._write_adapter(voltage=volts)

    @controls.current(adapter_current, min=2.0, max=10.0, step=0.1, enabled=False)
    async def set_adapter_current(self, amperes: float) -> None:
        await self._write_adapter(current=amperes)

    @controls.duration(standby_time, min=20, max=7200)
    async def set_standby_time(self, seconds: float) -> None:
        await self._write_standby(time=round(seconds))

    @controls.current_ma(standby_current_threshold, min=20, max=3000)
    async def set_standby_current_threshold(self, milliamperes: float) -> None:
        await self._write_standby(current_threshold=round(milliamperes))

    @controls.duration(screen_timeout, min=30, max=36000)
    async def set_screen_timeout(self, seconds: float) -> None:
        await self._write_screen_timeout(round(seconds))

    @controls.switch(screen_always_on)
    async def enable_screen_always_on(self, enabled: bool) -> None:
        await self._write_screen_timeout(
            SCREEN_ALWAYS_ON if enabled else self._last_screen_timeout
        )

    @controls.percentage(screen_brightness, min=20, max=100)
    async def set_screen_brightness(self, percent: float) -> None:
        await self.send_config(_SET_SCREEN_BRIGHTNESS, bytes([round(percent)]))
        await self._request_screen_brightness()

    # 0 turns the screen dark once the timeout expires
    @controls.percentage(screen_idle_brightness, min=0, max=100)
    async def set_screen_idle_brightness(self, percent: float) -> None:
        await self._write_screen_idle_brightness(round(percent))

    @controls.select(temperature_unit, options=TemperatureUnit)
    async def set_temperature_unit(self, unit: TemperatureUnit) -> None:
        await self.send_command(TEMPERATURE_UNIT_CHARACTERISTIC_UUID, bytes([unit]))
        await self._read_temperature_unit()

    @controls.select(screen_language, options=ScreenLanguage)
    async def set_screen_language(self, language: ScreenLanguage) -> None:
        await self.send_command(LANGUAGE_CHARACTERISTIC_UUID, bytes([language]))
        await self._read_screen_language()

    @controls.select(buzzer_mode, options=BuzzerMode)
    async def set_buzzer_mode(self, mode: BuzzerMode) -> None:
        await self.send_command(BUZZER_CHARACTERISTIC_UUID, bytes([mode]))
        await self._read_buzzer_mode()

    async def _write_adapter(
        self, *, voltage: float | None = None, current: float | None = None
    ) -> None:
        # the device stores all limits at once and the vendor app derives three of
        # them, so a change of either rating rewrites the whole block. Reading first
        # keeps a change made on the device or in the app meanwhile.
        async with self._adapter_lock:
            await self._read_adapter()
            voltage = voltage if voltage is not None else self.adapter_voltage
            current = current if current is not None else self.adapter_current
            if voltage is None or current is None:
                raise SettingUnavailable("Current adapter settings could not be read")

            settings = AdapterSettings.from_adapter(voltage, current)
            await self.send_command(ADAPTER_CHARACTERISTIC_UUID, settings.to_bytes())
            await self._read_adapter()

    async def _write_standby(
        self, *, time: int | None = None, current_threshold: int | None = None
    ) -> None:
        async with self._standby_lock:
            await self._read_standby()
            time = time if time is not None else self.standby_time
            current_threshold = (
                current_threshold
                if current_threshold is not None
                else self.standby_current_threshold
            )
            if time is None or current_threshold is None:
                raise SettingUnavailable("Current standby settings could not be read")

            settings = StandbySettings(time=time, current_threshold=current_threshold)
            await self.send_command(STANDBY_CHARACTERISTIC_UUID, settings.to_bytes())
            await self._read_standby()

    async def _write_screen_timeout(self, seconds: int) -> None:
        # a 4-byte payload leaves the idle backlight level unchanged
        async with self._screen_lock:
            await self.send_config(_SET_SCREEN_TIMEOUT, struct.pack("<I", seconds))
            await self._request_screen_timeout()

    async def _write_screen_idle_brightness(self, percent: int) -> None:
        # the idle level is only written together with the timeout, so the current
        # timeout is read first to keep a change made on the device meanwhile
        async with self._screen_lock:
            await self._query_config(_GET_SCREEN_TIMEOUT)
            if self._raw_screen_timeout is None:
                raise SettingUnavailable("Current screen timeout could not be read")
            await self.send_config(
                _SET_SCREEN_TIMEOUT,
                struct.pack("<IB", self._raw_screen_timeout, percent),
            )
            await self._request_screen_timeout()

    async def _read_adapter(self) -> None:
        settings = AdapterSettings.from_bytes(
            await self.read_value(ADAPTER_CHARACTERISTIC_UUID)
        )
        self.adapter_voltage = _scaled(settings.adapter_voltage, 1000)
        self.adapter_current = _scaled(settings.adapter_current, 1000)
        self._publish_updates()

    async def _read_standby(self) -> None:
        settings = StandbySettings.from_bytes(
            await self.read_value(STANDBY_CHARACTERISTIC_UUID)
        )
        self.standby_time = settings.time
        self.standby_current_threshold = settings.current_threshold
        self._publish_updates()

    async def _read_temperature_unit(self) -> None:
        self.temperature_unit = await self._read_enum(
            TEMPERATURE_UNIT_CHARACTERISTIC_UUID, TemperatureUnit
        )
        self._publish_updates()

    async def _read_screen_language(self) -> None:
        self.screen_language = await self._read_enum(
            LANGUAGE_CHARACTERISTIC_UUID, ScreenLanguage
        )
        self._publish_updates()

    async def _read_buzzer_mode(self) -> None:
        self.buzzer_mode = await self._read_enum(BUZZER_CHARACTERISTIC_UUID, BuzzerMode)
        self._publish_updates()

    async def _request_screen_timeout(self) -> None:
        # answered with a notification, see `config_parse`
        await self.send_config(_GET_SCREEN_TIMEOUT)

    async def _request_screen_brightness(self) -> None:
        await self.send_config(_GET_SCREEN_BRIGHTNESS)

    async def _request_wifi_status(self) -> None:
        await self.send_config(_GET_WIFI_STATUS)

    async def _query_config(self, message_type: int) -> None:
        """Send a configuration request and wait until its reply is parsed"""
        reply = asyncio.get_running_loop().create_future()
        replies = self._config_replies[message_type]
        replies.append(reply)
        try:
            await self.send_config(message_type)
            await asyncio.wait_for(reply, _CONFIG_REPLY_TIMEOUT)
        finally:
            replies.remove(reply)

    async def _read_enum[E: enum.IntEnum](
        self, characteristic: str, enum_type: type[E]
    ):
        payload = await self.read_value(characteristic)
        if not payload:
            return None
        try:
            return enum_type(payload[0])
        except ValueError:
            self._logger.warning("Unknown %s value %d", enum_type.__name__, payload[0])
            return None

    def _publish_updates(self) -> None:
        # called right after assigning fields, before the next await, so updates from
        # interleaved telemetry and settings reads are not lost
        for field_name in self.updated_fields:
            self.update_callback(field_name)
            self.update_state(field_name, getattr(self, field_name, None))
        self.reset_updated()


def _scaled(value: int | None, divisor: int) -> float | None:
    return None if value is None else round(value / divisor, 3)


def _power_event(value: int) -> PowerEvent | None:
    try:
        return PowerEvent(value)
    except ValueError:
        return None
