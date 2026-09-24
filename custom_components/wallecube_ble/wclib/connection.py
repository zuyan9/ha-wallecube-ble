import asyncio
import contextlib
import traceback
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Any

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    MAX_CONNECT_ATTEMPTS,
    BleakNotFoundError,
    establish_connection,
)

from . import packet
from .encryption import SessionCipher, candidate_base_macs, derive_session_key
from .exceptions import (
    ConnectionTimeout,
    MaxConnectionAttemptsReached,
    MaxReconnectAttemptsReached,
    PacketParseError,
    SessionKeyError,
    UnsupportedBluetoothProtocol,
)
from .listeners import ListenerGroup, ListenerRegistry
from .logging_util import ConnectionLogger, LogOptions

MAX_RECONNECT_ATTEMPTS = 2
MAX_ERRORS_BEFORE_RECONNECT = 5


def _uuid16(value: int) -> str:
    return f"0000{value:04x}-0000-1000-8000-00805f9b34fb"


UPS_SERVICE_UUID = _uuid16(0xF0A1)
TELEMETRY_CHARACTERISTIC_UUID = _uuid16(0xF0B1)
ADAPTER_CHARACTERISTIC_UUID = _uuid16(0xF0B2)
BUZZER_CHARACTERISTIC_UUID = _uuid16(0xF0B3)
STANDBY_CHARACTERISTIC_UUID = _uuid16(0xF0B4)
LANGUAGE_CHARACTERISTIC_UUID = _uuid16(0xF0B8)
TEMPERATURE_UNIT_CHARACTERISTIC_UUID = _uuid16(0xF0B9)
INFO_CHARACTERISTIC_UUID = _uuid16(0xF0BF)
CONFIG_CHARACTERISTIC_UUID = _uuid16(0xF0C1)


class ConnectionState(StrEnum):
    NOT_CONNECTED = auto()

    CREATED = auto()
    ESTABLISHING_CONNECTION = auto()
    CONNECTED = auto()
    ESTABLISHING_SESSION = auto()
    SESSION_ESTABLISHED = auto()
    SUBSCRIBING = auto()
    AUTHENTICATED = auto()

    ERROR_TIMEOUT = auto()
    ERROR_NOT_FOUND = auto()
    ERROR_BLEAK = auto()
    ERROR_UNSUPPORTED_PROTOCOL = auto()
    ERROR_AUTH_FAILED = auto()
    ERROR_UNKNOWN = auto()
    ERROR_TOO_MANY_ERRORS = auto()

    RECONNECTING = auto()
    ERROR_MAX_RECONNECT_ATTEMPTS_REACHED = auto()

    DISCONNECTING = auto()
    DISCONNECTED = auto()

    @property
    def connection_error(self) -> bool:
        return self in _CONNECTION_ERROR_STATES

    @property
    def is_error(self) -> bool:
        return self in _ERROR_STATES

    @property
    def is_connected(self) -> bool:
        return self in _CONNECTED_STATES

    @property
    def is_connecting(self) -> bool:
        return self.is_connected or self in _CONNECTING_STATES

    @property
    def authenticated(self) -> bool:
        return self is ConnectionState.AUTHENTICATED

    @property
    def is_terminal(self) -> bool:
        return self.is_error or self in _TERMINAL_STATES


_CONNECTION_ERROR_STATES = frozenset(
    {
        ConnectionState.ERROR_TIMEOUT,
        ConnectionState.ERROR_NOT_FOUND,
        ConnectionState.ERROR_BLEAK,
    }
)
_ERROR_STATES = _CONNECTION_ERROR_STATES | {
    ConnectionState.ERROR_UNSUPPORTED_PROTOCOL,
    ConnectionState.ERROR_AUTH_FAILED,
    ConnectionState.ERROR_UNKNOWN,
    ConnectionState.ERROR_TOO_MANY_ERRORS,
    ConnectionState.ERROR_MAX_RECONNECT_ATTEMPTS_REACHED,
}
_CONNECTED_STATES = frozenset(
    {
        ConnectionState.CONNECTED,
        ConnectionState.ESTABLISHING_SESSION,
        ConnectionState.SESSION_ESTABLISHED,
        ConnectionState.SUBSCRIBING,
    }
)
_CONNECTING_STATES = frozenset(
    {ConnectionState.ESTABLISHING_CONNECTION, ConnectionState.RECONNECTING}
)
_TERMINAL_STATES = frozenset(
    {
        ConnectionState.AUTHENTICATED,
        ConnectionState.DISCONNECTED,
        ConnectionState.NOT_CONNECTED,
    }
)


type DisconnectListener = Callable[[Exception | type[Exception] | None], None]
type ConnectionStateListener = Callable[[ConnectionState], None]
type DataReceivedListener = Callable[[bytes, ConnectionState], None]
type DataSendListener = Callable[[bytes], None]
type DataParser = Callable[[bytes], Awaitable[bool]]
type ConfigParser = Callable[[packet.ConfigMessage], Awaitable[bool]]


class _ConnectionListeners(ListenerRegistry):
    on_disconnect: ListenerGroup[DisconnectListener]
    on_connection_state_change: ListenerGroup[ConnectionStateListener]
    on_data_received: ListenerGroup[DataReceivedListener]
    on_data_send: ListenerGroup[DataSendListener]


class Connection:
    """Manages the BLE client and the session, and forwards telemetry for parsing"""

    @dataclass
    class Options:
        """Connection options configurable from HA"""

        timeout: int = 20
        bluez_start_notify: bool = False

    _listeners = _ConnectionListeners.create()

    def __init__(
        self,
        ble_dev: BLEDevice,
        data_parse: DataParser,
        base_mac_hint: bytes | None = None,
        config_parse: ConfigParser | None = None,
    ) -> None:
        self._ble_dev = ble_dev
        self._address = ble_dev.address
        self._data_parse = data_parse
        self._config_parse = config_parse
        self._base_mac_hint = base_mac_hint
        self._options = Connection.Options()
        self._logger = ConnectionLogger(self)

        self._client: BleakClient | None = None
        self._cipher: SessionCipher | None = None
        self._base_mac_offset: int | None = None
        self._base_mac_from_name = False
        self._info: bytes = b""

        self._errors = 0
        self._reconnect = True
        self._retry_on_disconnect = False
        self._retry_on_disconnect_delay = 10
        self._reconnect_task: asyncio.Task | None = None
        self._connection_attempt = 0
        self._reconnect_attempt = 0
        self._tasks: set[asyncio.Task] = set()

        self._state_changed = asyncio.Event()
        self._state_exception: Exception | type[Exception] | None = None
        self._last_exception: Exception | type[Exception] | None = None
        self._connection_state = ConnectionState.CREATED
        self._last_state = ConnectionState.CREATED

    @property
    def address(self) -> str:
        return self._address

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def state(self) -> ConnectionState:
        return self._connection_state

    @property
    def session_established(self) -> bool:
        return self._cipher is not None

    @property
    def base_mac_offset(self) -> int | None:
        """Offset between advertised address and factory MAC that keyed the session"""
        return self._base_mac_offset

    @property
    def base_mac_hint(self) -> bytes | None:
        return self._base_mac_hint

    @property
    def base_mac_from_name(self) -> bool:
        """True if the session key came from the MAC in the advertised name"""
        return self._base_mac_from_name

    @property
    def info(self) -> bytes:
        """Payload of the info characteristic read while establishing the session"""
        return self._info

    def on_disconnect(self, listener: DisconnectListener):
        """
        Add disconnect listener

        Parameters
        ----------
        listener
            Called on disconnect with the exception that caused it, if any

        Returns
        -------
        Function that removes the listener
        """
        return self._listeners.on_disconnect.add(listener)

    def on_state_change(self, listener: ConnectionStateListener):
        return self._listeners.on_connection_state_change.add(listener)

    def on_data_received(self, listener: DataReceivedListener):
        return self._listeners.on_data_received.add(listener)

    def on_data_send(self, listener: DataSendListener):
        return self._listeners.on_data_send.add(listener)

    def update_ble_device(self, ble_dev: BLEDevice):
        self._ble_dev = ble_dev

    def with_logging_options(self, options: LogOptions):
        self._logger.set_options(options)
        return self

    def with_disabled_reconnect(self, is_disabled: bool = True):
        self._reconnect = not is_disabled
        return self

    def with_options(self, options: "Connection.Options"):
        self._options = options
        return self

    async def connect(self, max_attempts: int | None = None) -> None:
        """Connect, derive the session key and subscribe to telemetry"""
        if self._connection_state.is_connected or self._connection_state in (
            ConnectionState.ESTABLISHING_CONNECTION,
            ConnectionState.AUTHENTICATED,
        ):
            return

        max_attempts = MAX_CONNECT_ATTEMPTS if max_attempts is None else max_attempts
        self._connection_attempt += 1
        if max_attempts != 0 and self._connection_attempt > max_attempts:
            self._connection_attempt = 0
            err = MaxConnectionAttemptsReached(
                last_error=self._last_exception, attempts=max_attempts
            )
            self._set_state(ConnectionState.ERROR_MAX_RECONNECT_ATTEMPTS_REACHED, err)
            raise err

        self._set_state(ConnectionState.ESTABLISHING_CONNECTION)
        self._logger.info("Connecting to device")
        try:
            self._client = await establish_connection(
                BleakClient,
                self._ble_dev,
                self._ble_dev.name or self._address,
                disconnected_callback=self.disconnected,
                ble_device_callback=lambda: self._ble_dev,
                # 0 means unlimited at connection level, but bleak needs a real count
                max_attempts=max_attempts or MAX_CONNECT_ATTEMPTS,
                timeout=self._options.timeout,
            )
        except TimeoutError as e:
            await self._fail(ConnectionState.ERROR_TIMEOUT, ConnectionTimeout(str(e)))
            return
        except BleakNotFoundError as e:
            await self._fail(ConnectionState.ERROR_NOT_FOUND, e)
            return
        except BleakError as e:
            await self._fail(ConnectionState.ERROR_BLEAK, e)
            return

        self._set_state(ConnectionState.CONNECTED)
        self._logger.info("Connected, establishing session")
        self._errors = 0

        try:
            await self._establish_session()
            await self._subscribe()
        except SessionKeyError as e:
            await self._fail(ConnectionState.ERROR_AUTH_FAILED, e)
            return
        except UnsupportedBluetoothProtocol as e:
            await self._fail(ConnectionState.ERROR_UNSUPPORTED_PROTOCOL, e)
            return
        except TimeoutError as e:
            await self._fail(ConnectionState.ERROR_TIMEOUT, ConnectionTimeout(str(e)))
            return
        except BleakError as e:
            await self._fail(ConnectionState.ERROR_BLEAK, e)
            return

        self._connection_attempt = 0
        self._reconnect_attempt = 0
        self._retry_on_disconnect = self._reconnect
        self._set_state(ConnectionState.AUTHENTICATED)
        self._logger.info("Session established, receiving telemetry")

    def disconnected(self, *args: Any) -> None:
        """Handle a disconnect reported by bleak"""
        self._client = None

        # bleak-retry-connector retries internally and raises on the final failure
        if self._connection_state is ConnectionState.ESTABLISHING_CONNECTION:
            return

        if self._connection_state is ConnectionState.DISCONNECTING:
            self._set_state(ConnectionState.DISCONNECTED)
            return

        # errors have already been reported to the listeners
        if self._connection_state.is_error:
            return

        self._logger.warning("Disconnected from device")
        if not self._retry_on_disconnect:
            self._set_state(ConnectionState.DISCONNECTED)
            self._notify_disconnect()
            return

        if self._reconnect_task is None:
            self._reconnect_task = self._add_task(self.reconnect())
            self._reconnect_task.add_done_callback(self._on_reconnect_done)

    async def reconnect(self) -> None:
        if self._reconnect_attempt == 0:
            self._retry_on_disconnect_delay = 10

        self._reconnect_attempt += 1
        if self._reconnect_attempt > MAX_RECONNECT_ATTEMPTS:
            self._logger.error(
                "Could not reconnect after %d attempts", MAX_RECONNECT_ATTEMPTS
            )
            self._reconnect_attempt = 0
            self._set_state(
                ConnectionState.ERROR_MAX_RECONNECT_ATTEMPTS_REACHED,
                MaxReconnectAttemptsReached(
                    last_error=self._last_exception, attempts=MAX_RECONNECT_ATTEMPTS
                ),
            )
            return

        self._logger.warning(
            "Reconnecting in %d seconds, attempt %d/%d",
            self._retry_on_disconnect_delay,
            self._reconnect_attempt,
            MAX_RECONNECT_ATTEMPTS,
        )
        await asyncio.sleep(self._retry_on_disconnect_delay)
        if not self._retry_on_disconnect:
            return

        self._retry_on_disconnect_delay += 10
        self._set_state(ConnectionState.RECONNECTING)
        await self.connect()

    async def disconnect(self) -> None:
        self._logger.info("Disconnecting from device")
        self._retry_on_disconnect = False
        self._reconnect_attempt = 0
        self._cancel_tasks()

        client, self._client = self._client, None
        if client is not None and client.is_connected:
            self._set_state(ConnectionState.DISCONNECTING)
            with contextlib.suppress(EOFError, BleakError):
                await client.disconnect()

        if self._connection_state is not ConnectionState.DISCONNECTED:
            self._set_state(ConnectionState.DISCONNECTED)

    async def wait_until_authenticated_or_error(
        self, raise_on_error: bool = False, return_exc: bool = False
    ):
        """Wait until the session is established or the connection failed"""
        while not self._connection_state.is_terminal:
            await self._state_changed.wait()

        state, exc = self._connection_state, self._state_exception
        if state is ConnectionState.DISCONNECTED:
            state = self._last_state

        if exc is not None and raise_on_error:
            if isinstance(exc, MaxReconnectAttemptsReached) and exc.last_error:
                raise exc.last_error
            raise exc

        return (state, exc) if return_exc else state

    async def observe_connection(self):
        while True:
            yield self._connection_state
            await self._state_changed.wait()

    async def read_value(self, characteristic: str) -> bytes:
        """Read an encrypted characteristic and return its decrypted payload"""
        client, cipher = self._require_session()
        data = bytes(await client.read_gatt_char(self._characteristic(characteristic)))
        self._listeners.on_data_received(data, self._connection_state)
        plaintext = cipher.decrypt(data)
        self._logger.log_filtered(
            LogOptions.DECRYPTED_PAYLOADS, "Read %s: %r", characteristic, plaintext
        )
        return packet.decode_response(plaintext)

    async def send_command(self, characteristic: str, payload: bytes = b"") -> None:
        """Encrypt a command frame and write it to a UPS characteristic"""
        client, cipher = self._require_session()
        frame = cipher.encrypt(packet.encode_command(cipher.token, payload))
        self._logger.log_filtered(
            LogOptions.DECRYPTED_PAYLOADS, "Write %s: %r", characteristic, payload
        )
        self._listeners.on_data_send(frame)
        await client.write_gatt_char(
            self._characteristic(characteristic), frame, response=True
        )

    async def send_config(self, message_type: int, payload: bytes = b"") -> None:
        """Encrypt a message and write it to the configuration characteristic"""
        client, cipher = self._require_session()
        frame = cipher.encrypt(
            packet.encode_config_message(cipher.token, message_type, payload)
        )
        self._logger.log_filtered(
            LogOptions.DECRYPTED_PAYLOADS,
            "Write config message 0x%02x: %r",
            message_type,
            payload,
        )
        self._listeners.on_data_send(frame)
        await client.write_gatt_char(
            self._characteristic(CONFIG_CHARACTERISTIC_UUID), frame, response=True
        )

    async def add_error(self, exception: Exception) -> None:
        tb = "".join(traceback.format_tb(exception.__traceback__))
        self._logger.error("Captured exception: %s:\n%s", exception, tb)
        self._errors += 1
        self._last_exception = exception
        if self._errors <= MAX_ERRORS_BEFORE_RECONNECT:
            return

        self._errors = 0
        self._set_state(ConnectionState.ERROR_TOO_MANY_ERRORS, exception)
        if self._client is not None and self._client.is_connected:
            self._logger.warning("Disconnecting after too many errors")
            with contextlib.suppress(EOFError, BleakError):
                await self._client.disconnect()

    def set_state(
        self, state: ConnectionState, exc: Exception | type[Exception] | None = None
    ) -> None:
        self._set_state(state, exc)

    async def _establish_session(self) -> None:
        self._set_state(ConnectionState.ESTABLISHING_SESSION)
        assert self._client is not None

        sample = bytes(
            await self._client.read_gatt_char(
                self._characteristic(INFO_CHARACTERISTIC_UUID)
            )
        )
        self._listeners.on_data_received(sample, self._connection_state)

        address = int(self._address.replace(":", ""), 16)
        for base_mac in candidate_base_macs(self._address, self._base_mac_hint):
            cipher = SessionCipher(derive_session_key(base_mac))
            try:
                plaintext = cipher.decrypt(sample)
            except PacketParseError as e:
                raise SessionKeyError(str(e)) from e

            if _is_valid_info_frame(plaintext):
                self._cipher = cipher
                self._base_mac_offset = int.from_bytes(base_mac) - address
                self._base_mac_from_name = base_mac == self._base_mac_hint
                self._info = packet.decode_response(plaintext)
                self._set_state(ConnectionState.SESSION_ESTABLISHED)
                self._logger.log_filtered(
                    LogOptions.CONNECTION_DEBUG,
                    "Session key matched (from advertised name: %s, MAC offset: %d)",
                    self._base_mac_from_name,
                    self._base_mac_offset,
                )
                return

        raise SessionKeyError(
            "No factory MAC candidate produced a session key that decrypts the device "
            "info frame"
        )

    async def _subscribe(self) -> None:
        self._set_state(ConnectionState.SUBSCRIBING)
        assert self._client is not None

        kwargs = {}
        if self._options.bluez_start_notify:
            kwargs["bluez"] = {"use_start_notify": True}

        await self._client.start_notify(
            self._characteristic(TELEMETRY_CHARACTERISTIC_UUID),
            self._on_telemetry,
            **kwargs,
        )

        # answers to configuration requests arrive as notifications; telemetry works
        # without them, so a missing or failing subscription only disables the
        # settings that use the configuration channel
        if self._config_parse is None:
            return
        config = self._client.services.get_characteristic(CONFIG_CHARACTERISTIC_UUID)
        if config is None:
            self._logger.warning("Device has no configuration characteristic")
            return
        try:
            await self._client.start_notify(config, self._on_config, **kwargs)
        except BleakError as e:
            self._logger.warning("Could not subscribe to configuration messages: %s", e)

    async def _on_telemetry(self, _: BleakGATTCharacteristic, data: bytearray) -> None:
        frame = bytes(data)
        self._listeners.on_data_received(frame, self._connection_state)
        self._logger.log_filtered(LogOptions.PACKETS, "Telemetry frame: %r", frame)

        try:
            processed = await self._data_parse(frame)
        except Exception as e:  # noqa: BLE001
            await self.add_error(e)
            return

        self._errors = 0
        if not processed:
            self._logger.log_filtered(
                LogOptions.CONNECTION_DEBUG, "Unprocessed frame: %r", frame
            )

    async def _on_config(self, _: BleakGATTCharacteristic, data: bytearray) -> None:
        frame = bytes(data)
        self._listeners.on_data_received(frame, self._connection_state)
        if self._cipher is None or self._config_parse is None:
            return

        try:
            plaintext = self._cipher.decrypt(frame)
            message = packet.ConfigMessage.from_bytes(plaintext)
        except PacketParseError as e:
            self._logger.warning("Could not decode configuration message: %s", e)
            return

        self._logger.log_filtered(
            LogOptions.DECRYPTED_PAYLOADS, "Config message: %r", plaintext
        )
        if message.token != self._cipher.token:
            self._logger.warning("Ignoring configuration message with foreign token")
            return

        try:
            await self._config_parse(message)
        except Exception as e:  # noqa: BLE001
            await self.add_error(e)

    def _characteristic(self, uuid: str) -> BleakGATTCharacteristic:
        assert self._client is not None
        if (characteristic := self._client.services.get_characteristic(uuid)) is None:
            available = [
                f"{c.uuid} {c.properties}"
                for c in self._client.services.characteristics.values()
            ]
            raise UnsupportedBluetoothProtocol(uuid, available)
        return characteristic

    def _require_session(self) -> tuple[BleakClient, SessionCipher]:
        if self._client is None or not self._client.is_connected:
            raise BleakError("Device is not connected")
        if self._cipher is None:
            raise SessionKeyError("Session is not established")
        return self._client, self._cipher

    async def _fail(self, state: ConnectionState, exc: Exception) -> None:
        self._logger.error("Connection failed: %s", exc)
        self._set_state(state, exc)
        client, self._client = self._client, None
        if client is not None and client.is_connected:
            with contextlib.suppress(EOFError, BleakError):
                await client.disconnect()

    def _set_state(
        self, state: ConnectionState, exc: Exception | type[Exception] | None = None
    ) -> None:
        self._state_exception = exc
        if exc is not None:
            self._last_exception = exc

        self._last_state = self._connection_state
        self._connection_state = state
        self._state_changed.set()
        self._state_changed.clear()
        self._listeners.on_connection_state_change(state)

        if state.is_error:
            self._notify_disconnect(exc)

    def _notify_disconnect(self, exc: Exception | type[Exception] | None = None):
        self._listeners.on_disconnect(exc if exc is not None else self._last_exception)

    def _on_reconnect_done(self, task: asyncio.Task[None]) -> None:
        self._reconnect_task = None
        if not task.cancelled() and (exc := task.exception()) is not None:
            self._logger.error("Reconnect failed: %s", exc)

    def _add_task(self, coro: Coroutine) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _cancel_tasks(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()


def _is_valid_info_frame(plaintext: bytes) -> bool:
    # The info response carries a 10-byte plaintext that the firmware zero-pads to a
    # full AES block. Checking the padding in addition to the magic byte makes a false
    # match with a wrong candidate key practically impossible.
    return plaintext[0] == packet.FRAME_MAGIC and plaintext[-4:] == bytes(4)
