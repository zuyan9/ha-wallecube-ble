import asyncio
import enum
import struct
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from functools import cached_property

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError

from .. import controls
from ..connection import (
    ADAPTER_CHARACTERISTIC_UUID,
    BUZZER_CHARACTERISTIC_UUID,
    INFO_CHARACTERISTIC_UUID,
    LANGUAGE_CHARACTERISTIC_UUID,
    STANDBY_CHARACTERISTIC_UUID,
    TEMPERATURE_UNIT_CHARACTERISTIC_UUID,
    UPS_SERVICE_UUID,
    config_source,
)
from ..devicebase import DeviceBase
from ..encryption import LOCAL_NAME_PREFIX
from ..exceptions import (
    PacketParseError,
    SettingNotConfirmed,
    SettingUnavailable,
    UnsupportedBluetoothProtocol,
)
from ..model import (
    AdapterSettings,
    InfoBlock,
    StandbySettings,
    UpsTelemetry,
    WakeOnLanTrigger,
    WifiStatus,
)
from ..model.wifi_status import ipv4
from ..packet import COMMAND_CONFIRMED, ConfigMessage, TelemetryFrame
from ..props import Field, dataclass_attr_mapper, raw_field
from ..props.raw_data_props import RawDataProps
from ..props.transforms import pdiv, prop_has_any_bit_on, prop_has_bit_on

tele = dataclass_attr_mapper(UpsTelemetry)
info = dataclass_attr_mapper(InfoBlock)

# set while the output is powered from the battery, the firmware only shows the
# remaining time in that state
_on_battery = prop_has_bit_on(8)
# status flag, set while the input is above the power-good threshold
_input_power = prop_has_bit_on(10)
# fault flag of the power board's over-voltage protection
_over_voltage = prop_has_bit_on(10)

# The power board latches its over-voltage flag only when its output comparator trips
# in the same interrupt as an input-power change, but it keeps the flag while the
# output is at least this many mV above the adapter voltage. On input power the output
# follows the input.
_OVER_VOLTAGE_MARGIN = 2000

# remaining time the power board reports when it has no load to estimate from or the
# estimate exceeds about 18 hours, and when its battery monitor missed a sample
_RUNTIME_UNKNOWN = (0xFFFF, 0)

# The power board estimates the remaining time until the battery is empty, but on
# battery it turns the output off at its reserve capacity, in 0.1 % of the whole
# charge. No client changes the reserve from its default, and reads of the standby
# settings do not return it.
_RESERVE_CAPACITY = 180
# From this power-board firmware version on, the battery level counts only the charge
# above the reserve, and the output turns off at a level of 1 %, just above the
# reserve. One W180 build of 1.29 still turns it off at the reserve, at most 0.8 % of
# the charge later.
_LEVEL_ABOVE_RESERVE_FIRMWARE = 29
_CUTOFF_LEVEL_ABOVE_RESERVE = 10  # 0.1 %

# first power-board firmware version that accepts standby thresholds above 2000 mA
_STANDBY_CURRENT_3000MA_FIRMWARE = 29

# lowest values the power board accepts; the front panel reports zeros instead until
# it got the settings from the power board, see `Device.refresh_settings`
_MIN_ADAPTER_MA = 1000
_MIN_ADAPTER_MV = 5000
_MIN_STANDBY_VALUE = 20  # s and mA

# seconds to wait before each new read of the values the front panel gets from the
# power board, while it reports none
_POWER_BOARD_REREAD_DELAYS = (3, 10)

# models by power-board hardware version. The models share the front-panel firmware
# and differ only in the power board, which reports the model in the info block.
_MODELS = {3: "W150", 4: "W180"}


def _known_version(value: int | None) -> int | None:
    # 0 means the power board did not report its versions
    return value or None


def _battery_temperature(value: int | None) -> float | None:
    # the front panel leaves the temperature blank outside this range
    if value is None or not -400 <= value <= 850:
        return None
    return value / 10


# the front panel keeps its backlight pin high for about the stored level in percent
# of each PWM period, but the backlight is lit while the pin is low. A higher level
# dims the screen, and level 100 still leaves it faintly lit.
def _brightness(backlight_level: int) -> int:
    return 100 - backlight_level


def _backlight_level(brightness: float) -> int:
    return 100 - round(brightness)


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
_SET_WAKE_ON_LAN_TRIGGER = 0x09
_GET_WAKE_ON_LAN_TRIGGER = 0x0A
_SET_SCREEN_BRIGHTNESS = 0x0D
_GET_SCREEN_BRIGHTNESS = 0x0E

# seconds to wait for the notification that answers a configuration request
_CONFIG_REPLY_TIMEOUT = 5
# seconds to wait for the result of a setting forwarded to the power board, the front
# panel itself waits at most 200 ms for the power board's answer
_COMMAND_RESULT_TIMEOUT = 5
# the front panel reports a missing answer as well when another power-board message
# arrives first while it waits, so an unconfirmed write is sent once more
_POWER_BOARD_WRITE_ATTEMPTS = 2

# screen timeout the vendor app writes for "keep screen on"
SCREEN_ALWAYS_ON = 0xFFFFFFFF
DEFAULT_SCREEN_TIMEOUT = 300


class Device(DeviceBase, RawDataProps):
    """WalleCube UPS"""

    # the device reports Wi-Fi changes only when asked
    POLL_INTERVAL = 60
    # the power board pushes telemetry about once a second while it runs, and none
    # while it sleeps with the output off, has shut down or updates its firmware
    DATA_TIMEOUT = 60

    battery_level = raw_field(tele.battery_level, pdiv(10, 1))
    battery_voltage = raw_field(tele.battery_voltage, pdiv(1000, 3))
    battery_current = raw_field(tele.battery_current, pdiv(1000, 3))
    dc_input_voltage = raw_field(tele.dc_input_voltage, pdiv(1000, 3))
    dc_input_current = raw_field(tele.dc_input_current, pdiv(1000, 3))
    dc_output_voltage = raw_field(tele.dc_output_voltage, pdiv(1000, 3))
    dc_output_current = raw_field(tele.dc_output_current, pdiv(1000, 3))
    temperature = raw_field(tele.temperature, _battery_temperature)
    energy_total = raw_field(tele.energy_total, pdiv(1_000_000, 3))
    status_flags = raw_field(tele.status_flags)
    fault_flags = raw_field(tele.fault_flags)

    cell_voltage_1 = raw_field(tele.cell_voltage_1, pdiv(1000, 3))
    cell_voltage_2 = raw_field(tele.cell_voltage_2, pdiv(1000, 3))
    cell_voltage_3 = raw_field(tele.cell_voltage_3, pdiv(1000, 3))
    cell_voltage_4 = raw_field(tele.cell_voltage_4, pdiv(1000, 3))
    battery_cycles = raw_field(tele.battery_cycles)
    # the frame has no health value, see `_battery_health`
    battery_health = Field[float]()

    # status flag names follow the vendor app
    overload = raw_field(tele.status_flags, prop_has_bit_on(2))
    shutdown_imminent = raw_field(tele.status_flags, prop_has_bit_on(4))
    charging = raw_field(tele.status_flags, prop_has_bit_on(7))
    discharging = raw_field(tele.status_flags, _on_battery)
    input_power_ok = raw_field(tele.status_flags, _input_power)
    # the vendor app ignores these conditions, they follow the power-board firmware
    over_temperature = raw_field(tele.status_flags, prop_has_bit_on(3))
    # cell under- or over-voltage, discharge over-current or short circuit, or a
    # failure of the battery monitor chip
    battery_fault = raw_field(tele.fault_flags, prop_has_any_bit_on(0, 1, 3, 4, 5, 6))
    under_temperature = raw_field(tele.fault_flags, prop_has_bit_on(9))
    # the power board rarely sets its flag, see `_input_over_voltage`
    input_over_voltage = Field[bool]()
    output_over_current = raw_field(tele.fault_flags, prop_has_bit_on(11))

    output_power = Field[float]()
    # positive while charging, like the battery current
    battery_power = Field[float]()
    remaining_time_discharging = Field[int]()
    # highest minus lowest cell voltage in mV, the vendor app's balance indicator
    cell_voltage_difference = Field[int]()
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
    # derived from the adapter voltage, not a control: below it the input counts as
    # lost. The vendor app calls it the power-off voltage.
    power_good_voltage = Field[float]()
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
    # when the vendor app's "Auto boot" wakes its targets after an outage
    wake_on_lan_min_outage = Field[int]()
    wake_on_lan_delay = Field[int]()
    wake_on_lan_min_battery = Field[int]()

    _warned_truncated = False
    _warned_power_board = False
    _last_screen_timeout = DEFAULT_SCREEN_TIMEOUT
    # as reported, including the value for "keep screen on"
    _raw_screen_timeout: int | None = None

    def __init__(self, ble_dev: BLEDevice, adv_data: AdvertisementData) -> None:
        super().__init__(ble_dev, adv_data)
        # adapter, standby and Wake-on-LAN values are written as whole blocks, so
        # changes of two values in one block must not interleave or one of them is lost
        self._adapter_lock = asyncio.Lock()
        self._standby_lock = asyncio.Lock()
        self._wake_on_lan_lock = asyncio.Lock()
        self._screen_lock = asyncio.Lock()
        self._config_replies: dict[int, list[asyncio.Future[None]]] = defaultdict(list)

    @classmethod
    def check(cls, adv_data: AdvertisementData) -> bool:
        local_name = adv_data.local_name or ""
        return (
            local_name.startswith(LOCAL_NAME_PREFIX)
            or UPS_SERVICE_UUID in adv_data.service_uuids
        )

    @cached_property
    def data_fields(self) -> frozenset[str]:
        # the derived values come from telemetry too; the power event is left out, it
        # only lasts for the frame that carries it
        derived = (
            Device.output_power,
            Device.battery_power,
            Device.remaining_time_discharging,
            Device.cell_voltage_difference,
            Device.battery_health,
            Device.input_over_voltage,
        )
        mapped = self._datatype_to_field.get(UpsTelemetry, [])
        return frozenset(field.public_name for field in (*mapped, *derived))

    @property
    def NAME_PREFIX(self) -> str:
        model = self._model
        return "WalleCube-" if model is None else f"{model}-"

    @property
    def device(self) -> str:
        return self._model or super().device

    @property
    def _model(self) -> str | None:
        # unknown until the info block was read while connecting, and when the power
        # board did not report its versions
        return _MODELS.get(self.power_board_hardware_version or 0)

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
        if self.battery_voltage is not None and self.battery_current is not None:
            self.battery_power = round(self.battery_voltage * self.battery_current, 2)

        cells = (
            telemetry.cell_voltage_1,
            telemetry.cell_voltage_2,
            telemetry.cell_voltage_3,
            telemetry.cell_voltage_4,
        )
        self.cell_voltage_difference = (
            max(cells) - min(cells) if None not in cells else None
        )
        self.battery_health = _battery_health(telemetry.battery_cycles)
        self.input_over_voltage = _input_over_voltage(telemetry, self.adapter_voltage)

        self.remaining_time_discharging = _minutes_until_output_off(
            telemetry, self.power_board_firmware_version
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
            _GET_WAKE_ON_LAN_TRIGGER: self._parse_wake_on_lan_trigger,
        }.get(message.message_type)
        if parse is None or not parse(message.payload):
            return False

        self._publish_updates()
        for reply in self._config_replies.get(message.message_type, []):
            if not reply.done():
                reply.set_result(None)
        return True

    def redact_payload(self, source: str, payload: bytes) -> bytes:
        # the Wi-Fi status names the network and its addresses; only whether the UPS
        # is connected and the signal strength are kept, and the length
        if source == config_source(_GET_WIFI_STATUS):
            return payload[:2] + bytes(len(payload[2:]))
        return payload

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
            self.screen_idle_brightness = _brightness(payload[4])
        return True

    def _parse_screen_brightness(self, payload: bytes) -> bool:
        if not payload:
            return False
        self.screen_brightness = _brightness(payload[0])
        return True

    def _parse_wake_on_lan_trigger(self, payload: bytes) -> bool:
        if len(payload) < WakeOnLanTrigger.SIZE:
            return False
        trigger = WakeOnLanTrigger.from_bytes(payload)
        self.wake_on_lan_min_outage = trigger.min_outage
        self.wake_on_lan_delay = trigger.delay
        self.wake_on_lan_min_battery = trigger.min_battery_level
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
            self._request_wake_on_lan_trigger,
            self._request_wifi_status,
        )
        if not await self._read_settings(readers):
            return

        # The front panel asks the power board for its versions and then for its
        # settings once, about a second after it starts advertising, and reports zeros
        # until it has the answers, or until it restarts if they did not come. A
        # connection right after the start can read too early.
        for delay in _POWER_BOARD_REREAD_DELAYS:
            if self.power_board_hardware_version is not None:
                return
            await asyncio.sleep(delay)
            readers = (self._read_info, self._read_adapter, self._read_standby)
            if not await self._read_settings(readers):
                return
        if self.power_board_hardware_version is None and not self._warned_power_board:
            self._warned_power_board = True
            self._logger.warning(
                "The UPS did not get the versions of its power board when it started, "
                "they stay unknown until it restarts"
            )

    async def _read_settings(
        self, readers: Iterable[Callable[[], Awaitable[None]]]
    ) -> bool:
        """Call each reader, return False if the connection was lost"""
        for read in readers:
            # a single read can fail, the others are still usable
            try:
                await read()
            except (UnsupportedBluetoothProtocol, PacketParseError) as e:
                self._logger.warning("Could not read setting: %s", e)
            except (BleakError, TimeoutError) as e:
                if not self.is_connected:
                    self._logger.debug("Connection lost while reading settings")
                    return False
                self._logger.warning("Could not read setting: %s", e)
        return True

    async def poll(self) -> None:
        await self._request_wifi_status()

    # The power board applies the adapter settings when it starts, so the vendor app
    # asks for a restart with the reset hole; only the charging-current limit follows
    # a new adapter current at once. The front panel accepts up to 20.2 V, but the
    # power board keeps its previous voltage for values above 20 V while the write is
    # still confirmed.
    @controls.voltage(
        adapter_voltage,
        min=5.0,
        max=20.0,
        step=0.1,
        enabled=False,
        restart_required=True,
    )
    async def set_adapter_voltage(self, volts: float) -> None:
        await self._write_adapter(voltage=volts)

    @controls.current(
        adapter_current,
        min=2.0,
        max=10.0,
        step=0.1,
        enabled=False,
        restart_required=True,
    )
    async def set_adapter_current(self, amperes: float) -> None:
        await self._write_adapter(current=amperes)

    @controls.duration(standby_time, min=20, max=7200)
    async def set_standby_time(self, seconds: float) -> None:
        await self._write_standby(time=round(seconds))

    @controls.current_ma(standby_current_threshold, min=20, max=3000)
    async def set_standby_current_threshold(self, milliamperes: float) -> None:
        # power-board firmware before 1.29 keeps its previous threshold above 2000 mA,
        # while the front panel still reports the written value
        version = self.power_board_firmware_version
        if version is not None and version < _STANDBY_CURRENT_3000MA_FIRMWARE:
            milliamperes = min(milliamperes, 2000)
        await self._write_standby(current_threshold=round(milliamperes))

    @controls.duration(screen_timeout, min=30, max=36000)
    async def set_screen_timeout(self, seconds: float) -> None:
        await self._write_screen_timeout(round(seconds))

    @controls.switch(screen_always_on)
    async def enable_screen_always_on(self, enabled: bool) -> None:
        await self._write_screen_timeout(
            SCREEN_ALWAYS_ON if enabled else self._last_screen_timeout
        )

    # the front panel raises an active backlight level below 20 to 20, so the screen
    # is at most 80 % bright
    @controls.percentage(screen_brightness, min=0, max=80)
    async def set_screen_brightness(self, percent: float) -> None:
        await self.send_config(
            _SET_SCREEN_BRIGHTNESS, bytes([_backlight_level(percent)])
        )
        await self._request_screen_brightness()

    # applies once the screen timeout expires
    @controls.percentage(screen_idle_brightness, min=0, max=100)
    async def set_screen_idle_brightness(self, percent: float) -> None:
        await self._write_idle_backlight(_backlight_level(percent))

    # The vendor app only sets the targets, so these are left at their defaults
    # unless changed here. The front panel raises the times below 10 s to 10 s and
    # limits the battery level to 20-80 %.
    @controls.duration(wake_on_lan_min_outage, min=10, max=0xFFFF, enabled=False)
    async def set_wake_on_lan_min_outage(self, seconds: float) -> None:
        await self._write_wake_on_lan_trigger(min_outage=round(seconds))

    @controls.duration(wake_on_lan_delay, min=10, max=0xFFFF, enabled=False)
    async def set_wake_on_lan_delay(self, seconds: float) -> None:
        await self._write_wake_on_lan_trigger(delay=round(seconds))

    @controls.percentage(wake_on_lan_min_battery, min=20, max=80, enabled=False)
    async def set_wake_on_lan_min_battery(self, percent: float) -> None:
        await self._write_wake_on_lan_trigger(min_battery_level=round(percent))

    # front-panel firmware before 1.18 has no temperature unit setting
    @controls.select(
        temperature_unit,
        options=TemperatureUnit,
        characteristic=TEMPERATURE_UNIT_CHARACTERISTIC_UUID,
    )
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
                raise SettingUnavailable(
                    "The UPS did not get the adapter settings from its power board"
                )

            settings = AdapterSettings.from_adapter(voltage, current)
            # the front panel keeps the written block and reports it on reads even when
            # the power board missed it, so only the result tells whether it arrived.
            # The values read before stay shown if it did not.
            await self._write_to_power_board(
                ADAPTER_CHARACTERISTIC_UUID, settings.to_bytes(), "adapter settings"
            )
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
                raise SettingUnavailable(
                    "The UPS did not get the sleep settings from its power board"
                )

            settings = StandbySettings(time=time, current_threshold=current_threshold)
            # unconfirmed: the device notifies the result like for the adapter
            # settings, but on a characteristic without the notify property, and
            # clients such as BlueZ do not deliver such notifications
            await self.send_command(STANDBY_CHARACTERISTIC_UUID, settings.to_bytes())
            await self._read_standby()

    async def _write_screen_timeout(self, seconds: int) -> None:
        # a 4-byte payload leaves the idle backlight level unchanged
        async with self._screen_lock:
            await self.send_config(_SET_SCREEN_TIMEOUT, struct.pack("<I", seconds))
            await self._request_screen_timeout()

    async def _write_idle_backlight(self, level: int) -> None:
        # the idle level is only written together with the timeout, so the current
        # timeout is read first to keep a change made on the device meanwhile
        async with self._screen_lock:
            await self._query_config(_GET_SCREEN_TIMEOUT)
            if self._raw_screen_timeout is None:
                raise SettingUnavailable("Current screen timeout could not be read")
            await self.send_config(
                _SET_SCREEN_TIMEOUT,
                struct.pack("<IB", self._raw_screen_timeout, level),
            )
            await self._request_screen_timeout()

    async def _write_wake_on_lan_trigger(
        self,
        *,
        min_outage: int | None = None,
        delay: int | None = None,
        min_battery_level: int | None = None,
    ) -> None:
        # the three values are only written together, so the current ones are read
        # first
        async with self._wake_on_lan_lock:
            await self._query_config(_GET_WAKE_ON_LAN_TRIGGER)
            min_outage = (
                min_outage if min_outage is not None else self.wake_on_lan_min_outage
            )
            delay = delay if delay is not None else self.wake_on_lan_delay
            min_battery_level = (
                min_battery_level
                if min_battery_level is not None
                else self.wake_on_lan_min_battery
            )
            if min_outage is None or delay is None or min_battery_level is None:
                raise SettingUnavailable("Current Wake-on-LAN settings are unknown")

            trigger = WakeOnLanTrigger(
                min_outage=min_outage, delay=delay, min_battery_level=min_battery_level
            )
            await self.send_config(_SET_WAKE_ON_LAN_TRIGGER, trigger.to_bytes())
            await self._request_wake_on_lan_trigger()

    async def _write_to_power_board(
        self, characteristic: str, payload: bytes, description: str
    ) -> None:
        """Write a setting the front panel forwards and wait for the power board"""
        for _ in range(_POWER_BOARD_WRITE_ATTEMPTS):
            try:
                status = await self.send_confirmed_command(
                    characteristic, payload, _COMMAND_RESULT_TIMEOUT
                )
            except TimeoutError as e:
                raise SettingNotConfirmed(
                    f"The UPS did not report whether its power board received the "
                    f"{description}"
                ) from e
            if status == COMMAND_CONFIRMED:
                return
            self._logger.debug("Power board did not answer, status %d", status)
        raise SettingNotConfirmed(
            f"The UPS power board did not confirm the {description}"
        )

    async def _read_info(self) -> None:
        self.info_parse(await self.read_value(INFO_CHARACTERISTIC_UUID))

    async def _read_adapter(self) -> None:
        settings = AdapterSettings.from_bytes(
            await self.read_value(ADAPTER_CHARACTERISTIC_UUID)
        )
        voltage, current = settings.adapter_voltage, settings.adapter_current
        power_good = settings.power_good_voltage
        # written as a block, so one missing value makes the block unknown
        if (voltage or 0) < _MIN_ADAPTER_MV or (current or 0) < _MIN_ADAPTER_MA:
            voltage = current = power_good = None
        self.adapter_voltage = _scaled(voltage, 1000)
        self.adapter_current = _scaled(current, 1000)
        self.power_good_voltage = _scaled(power_good, 1000)
        self._publish_updates()

    async def _read_standby(self) -> None:
        settings = StandbySettings.from_bytes(
            await self.read_value(STANDBY_CHARACTERISTIC_UUID)
        )
        time, threshold = settings.time, settings.current_threshold
        if (time or 0) < _MIN_STANDBY_VALUE or (threshold or 0) < _MIN_STANDBY_VALUE:
            time = threshold = None
        self.standby_time = time
        self.standby_current_threshold = threshold
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

    async def _request_wake_on_lan_trigger(self) -> None:
        await self.send_config(_GET_WAKE_ON_LAN_TRIGGER)

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
        # the setting does not exist in this firmware version
        if not self.has_characteristic(characteristic):
            return None
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
        self._publish(self.updated_fields)
        self.reset_updated()


def _scaled(value: int | None, divisor: int) -> float | None:
    return None if value is None else round(value / divisor, 3)


def _battery_health(cycles: int | None) -> float | None:
    # the vendor cloud estimates the health from the cycle count alone and the vendor
    # app shows its result. The cloud only caps it at 100 %, below 0 % it is limited
    # here.
    if cycles is None:
        return None
    return round(max(0.0, min(100.0, (1500 - cycles) / 14)), 1)


def _minutes_until_output_off(
    telemetry: UpsTelemetry, power_board_firmware: int | None
) -> int | None:
    """Time until the UPS turns its output off on battery, rounded down"""
    seconds, level = telemetry.remaining_time, telemetry.battery_level
    if (
        # the UPS shows the remaining time only while the output runs on battery
        not _on_battery(telemetry.status_flags)
        or seconds is None
        or seconds in _RUNTIME_UNKNOWN
        or level is None
        # without the version, the level can't be related to the whole charge
        or power_board_firmware is None
    ):
        return None

    # the estimate is proportional to the whole charge, in 0.1 %
    if power_board_firmware >= _LEVEL_ABOVE_RESERVE_FIRMWARE:
        above_reserve = (1000 - _RESERVE_CAPACITY) / 1000
        charge = _RESERVE_CAPACITY + level * above_reserve
        cutoff = _RESERVE_CAPACITY + _CUTOFF_LEVEL_ABOVE_RESERVE * above_reserve
    else:
        charge, cutoff = level, _RESERVE_CAPACITY
    if charge <= cutoff:
        return 0
    return int(seconds * (charge - cutoff) / charge // 60)


def _input_over_voltage(
    telemetry: UpsTelemetry, adapter_voltage: float | None
) -> bool | None:
    # the power board's flag, or the condition that keeps it set
    if telemetry.fault_flags is None or telemetry.status_flags is None:
        return None
    if _over_voltage(telemetry.fault_flags):
        return True
    output = telemetry.dc_output_voltage
    if adapter_voltage is None or output is None:
        return False
    threshold = round(adapter_voltage * 1000) + _OVER_VOLTAGE_MARGIN
    return bool(_input_power(telemetry.status_flags)) and output >= threshold


def _power_event(value: int) -> PowerEvent | None:
    try:
        return PowerEvent(value)
    except ValueError:
        return None
