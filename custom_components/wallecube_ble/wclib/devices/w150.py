import asyncio
import enum
import struct

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
from ..model import AdapterSettings, StandbySettings, UpsTelemetry
from ..packet import ConfigMessage, TelemetryFrame
from ..props import Field, dataclass_attr_mapper, raw_field
from ..props.raw_data_props import RawDataProps
from ..props.transforms import pdiv, prop_has_bit_on

tele = dataclass_attr_mapper(UpsTelemetry)

# set while the output is powered from the battery, the firmware only shows the
# remaining time in that state
_on_battery = prop_has_bit_on(8)


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


# configuration-channel message types
_SET_SCREEN_TIMEOUT = 0x0B
_GET_SCREEN_TIMEOUT = 0x0C

# screen timeout the vendor app writes for "keep screen on"
SCREEN_ALWAYS_ON = 0xFFFFFFFF
DEFAULT_SCREEN_TIMEOUT = 300


class Device(DeviceBase, RawDataProps):
    """W150"""

    NAME_PREFIX = "W150-"

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

    _warned_truncated = False
    _last_screen_timeout = DEFAULT_SCREEN_TIMEOUT

    def __init__(self, ble_dev: BLEDevice, adv_data: AdvertisementData) -> None:
        super().__init__(ble_dev, adv_data)
        # adapter and standby values are written as whole blocks, so changes of two
        # values in one block must not interleave or one of them is lost
        self._adapter_lock = asyncio.Lock()
        self._standby_lock = asyncio.Lock()

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

        self._publish_updates()
        return True

    async def config_parse(self, message: ConfigMessage) -> bool:
        if message.message_type != _GET_SCREEN_TIMEOUT or len(message.payload) < 4:
            return False

        timeout = int.from_bytes(message.payload[:4], "little")
        self.screen_always_on = timeout == SCREEN_ALWAYS_ON
        if timeout == SCREEN_ALWAYS_ON:
            self.screen_timeout = None
        else:
            self.screen_timeout = timeout
            self._last_screen_timeout = timeout
        self._publish_updates()
        return True

    async def refresh_settings(self) -> None:
        readers = (
            self._read_adapter,
            self._read_standby,
            self._read_temperature_unit,
            self._read_screen_language,
            self._read_buzzer_mode,
            self._request_screen_timeout,
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
        await self.send_config(_SET_SCREEN_TIMEOUT, struct.pack("<I", seconds))
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
