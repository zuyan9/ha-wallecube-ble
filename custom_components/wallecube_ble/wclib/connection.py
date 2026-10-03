import asyncio
import contextlib
import logging
import traceback
from collections.abc import Awaitable, Callable
from enum import StrEnum, auto
from typing import Any

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakNotFoundError, establish_connection

from . import packet
from .encryption import SessionCipher, candidate_base_macs, derive_session_key
from .exceptions import (
    PacketParseError,
    SessionKeyError,
    UnsupportedBluetoothProtocol,
)
from .listeners import ListenerGroup
from .logging_util import (
    device_logger,
    mask_address,
    mask_identifiers,
    mask_local_name,
)

MAX_ERRORS_BEFORE_RECONNECT = 5

_CIPHER_BLOCK_SIZE = 16


def _uuid16(value: int) -> str:
    return f"0000{value:04x}-0000-1000-8000-00805f9b34fb"


def _source(uuid: str) -> str:
    # the 16-bit form, e.g. F0B2, as the docs name the characteristics
    return uuid[4:8].upper()


def config_source(message_type: int) -> str:
    """Name of a configuration message in the data passed to data listeners"""
    return f"F0C1/{message_type:02X}"


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
    ERROR_TOO_MANY_ERRORS = auto()

    DISCONNECTING = auto()
    DISCONNECTED = auto()

    @property
    def is_error(self) -> bool:
        return self in _ERROR_STATES

    @property
    def is_connected(self) -> bool:
        return self in _CONNECTED_STATES


# the state a failed connect ends in, by the exception that made it fail; the most
# specific first, BleakNotFoundError is a BleakError
_FAILURE_STATES: tuple[tuple[type[Exception], ConnectionState], ...] = (
    (SessionKeyError, ConnectionState.ERROR_AUTH_FAILED),
    (UnsupportedBluetoothProtocol, ConnectionState.ERROR_UNSUPPORTED_PROTOCOL),
    (TimeoutError, ConnectionState.ERROR_TIMEOUT),
    (BleakNotFoundError, ConnectionState.ERROR_NOT_FOUND),
    (BleakError, ConnectionState.ERROR_BLEAK),
)
_FAILURE_TYPES = tuple(kind for kind, _ in _FAILURE_STATES)
_ERROR_STATES = frozenset(state for _, state in _FAILURE_STATES) | {
    ConnectionState.ERROR_TOO_MANY_ERRORS
}
_CONNECTED_STATES = frozenset(
    {
        ConnectionState.CONNECTED,
        ConnectionState.ESTABLISHING_SESSION,
        ConnectionState.SESSION_ESTABLISHED,
        ConnectionState.SUBSCRIBING,
    }
)


# called on disconnect with the exception that caused it, if any
type DisconnectListener = Callable[[Exception | None], None]
type ConnectionStateListener = Callable[[ConnectionState], None]
# called with the characteristic or configuration message, see `config_source`, and the
# decrypted payload without session header; encrypted frames are never passed on
type DataReceivedListener = Callable[[str, bytes], None]
type DataSendListener = Callable[[str, bytes], None]
type DataParser = Callable[[bytes], Awaitable[bool]]
type ConfigParser = Callable[[packet.ConfigMessage], Awaitable[bool]]


class Listeners:
    """Listeners of a device, shared by its successive connections"""

    def __init__(self) -> None:
        self.on_disconnect: ListenerGroup[DisconnectListener] = ListenerGroup()
        self.on_state_change: ListenerGroup[ConnectionStateListener] = ListenerGroup()
        self.on_data_received: ListenerGroup[DataReceivedListener] = ListenerGroup()
        self.on_data_send: ListenerGroup[DataSendListener] = ListenerGroup()


class Connection:
    """Manages the BLE client and the session, and forwards telemetry for parsing"""

    def __init__(
        self,
        ble_dev: BLEDevice,
        data_parse: DataParser,
        base_mac_hint: bytes | None = None,
        config_parse: ConfigParser | None = None,
        listeners: Listeners | None = None,
        bluez_start_notify: bool = False,
    ) -> None:
        self._ble_dev = ble_dev
        self._address = ble_dev.address
        self._data_parse = data_parse
        self._config_parse = config_parse
        self._base_mac_hint = base_mac_hint
        self.listeners = listeners if listeners is not None else Listeners()
        # subscribe through BlueZ's StartNotify instead of AcquireNotify
        self.bluez_start_notify = bluez_start_notify
        self._logger = device_logger(__name__, self._address)

        self._client: BleakClient | None = None
        self._cipher: SessionCipher | None = None
        self._base_mac_from_name = False
        self._info: bytes = b""
        self._characteristics: frozenset[str] | None = None
        self._warned_truncated_config = False

        self._errors = 0
        self._failure_logged = False
        self._connection_state = ConnectionState.DISCONNECTED

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
    def base_mac_from_name(self) -> bool:
        """True if the session key came from the MAC in the advertised name"""
        return self._base_mac_from_name

    @property
    def info(self) -> bytes:
        """Payload of the info characteristic read while establishing the session"""
        return self._info

    @property
    def characteristics(self) -> frozenset[str] | None:
        """UUIDs of the characteristics the device exposed, None before connecting"""
        return self._characteristics

    def update_ble_device(self, ble_dev: BLEDevice):
        self._ble_dev = ble_dev

    async def connect(self) -> None:
        """
        Connect, derive the session key and subscribe to telemetry

        A failure is raised after it was logged and passed to the listeners.
        """
        if self._connection_state.is_connected or self._connection_state in (
            ConnectionState.ESTABLISHING_CONNECTION,
            ConnectionState.AUTHENTICATED,
        ):
            return

        self._set_state(ConnectionState.ESTABLISHING_CONNECTION)
        self._logger.info("Connecting to device")
        try:
            # kept in a local: `disconnected` clears `_client` when bleak reports the
            # end of the link, which can happen at any await of the handshake
            client = self._client = await establish_connection(
                BleakClient,
                self._ble_dev,
                # only used in messages, which would otherwise carry the factory MAC
                mask_local_name(self._ble_dev.name) or mask_address(self._address),
                disconnected_callback=self.disconnected,
                ble_device_callback=lambda: self._ble_dev,
            )
            self._ensure_connected(client)
            self._set_state(ConnectionState.CONNECTED)
            self._logger.info("Connected, establishing session")
            self._errors = 0
            # kept after disconnecting: the set only changes with a firmware update
            self._characteristics = frozenset(
                c.uuid for c in client.services.characteristics.values()
            )
            await self._establish_session(client)
            await self._subscribe(client)
        except _FAILURE_TYPES as e:
            await self._fail(e)
            raise

        self._failure_logged = False
        self._set_state(ConnectionState.AUTHENTICATED)
        self._logger.info("Session established, receiving telemetry")

    def disconnected(self, client: BleakClient) -> None:
        """Handle a disconnect reported by bleak"""
        # e.g. a link this connection ended itself, which BlueZ can report late
        if client is not self._client:
            return
        self._client = None

        # `connect` fails and reports a link lost before the session is established;
        # bleak-retry-connector also retries internally and raises on the final failure
        if (
            self._connection_state is ConnectionState.ESTABLISHING_CONNECTION
            or self._connection_state.is_connected
        ):
            return

        # errors have already been reported to the listeners
        if self._connection_state.is_error:
            return

        self._logger.warning("Disconnected from device")
        self._set_state(ConnectionState.DISCONNECTED)
        self.listeners.on_disconnect(None)

    async def disconnect(self) -> None:
        self._logger.info("Disconnecting from device")
        client, self._client = self._client, None
        if client is not None and client.is_connected:
            self._set_state(ConnectionState.DISCONNECTING)
            await self._disconnect_client(client)

        if self._connection_state is not ConnectionState.DISCONNECTED:
            self._set_state(ConnectionState.DISCONNECTED)

    async def read_value(self, characteristic: str) -> bytes:
        """Read an encrypted characteristic and return its decrypted payload"""
        client, cipher = self._require_session()
        data = bytes(
            await client.read_gatt_char(_characteristic(client, characteristic))
        )
        payload = packet.decode_response(cipher.decrypt(data))
        self.listeners.on_data_received(_source(characteristic), payload)
        return payload

    async def send_command(self, characteristic: str, payload: bytes = b"") -> None:
        """Encrypt a command frame and write it to a UPS characteristic"""
        client, cipher = self._require_session()
        frame = cipher.encrypt(packet.encode_command(cipher.token, payload))
        self.listeners.on_data_send(_source(characteristic), payload)
        await client.write_gatt_char(
            _characteristic(client, characteristic), frame, response=True
        )

    async def send_confirmed_command(
        self, characteristic: str, payload: bytes, timeout: float
    ) -> int:
        """
        Write a command and wait for the result the device notifies

        The device notifies a result on the characteristics whose commands it forwards
        to the power-board MCU. The characteristic is only subscribed while the command
        waits for its result.

        Returns
        -------
        Status of the result, `packet.COMMAND_CONFIRMED` if the power board answered
        """
        client, _ = self._require_session()
        target = _characteristic(client, characteristic)
        result: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

        def on_result(_: BleakGATTCharacteristic, data: bytearray) -> None:
            # the result is not encrypted
            frame = bytes(data)
            self.listeners.on_data_received(f"{_source(characteristic)} result", frame)
            if not result.done():
                result.set_result(frame)

        await client.start_notify(target, on_result, **self._notify_kwargs())
        try:
            await self.send_command(characteristic, payload)
            frame = await asyncio.wait_for(result, timeout)
        finally:
            if client.is_connected:
                with contextlib.suppress(EOFError, BleakError):
                    await client.stop_notify(target)

        return packet.decode_command_result(frame)

    async def send_config(self, message_type: int, payload: bytes = b"") -> None:
        """Encrypt a message and write it to the configuration characteristic"""
        client, cipher = self._require_session()
        frame = cipher.encrypt(
            packet.encode_config_message(cipher.token, message_type, payload)
        )
        self.listeners.on_data_send(config_source(message_type), payload)
        await client.write_gatt_char(
            _characteristic(client, CONFIG_CHARACTERISTIC_UUID), frame, response=True
        )

    async def add_error(self, exception: Exception) -> None:
        tb = "".join(traceback.format_tb(exception.__traceback__))
        self._logger.error("Captured exception: %s:\n%s", exception, tb)
        self._errors += 1
        if self._errors <= MAX_ERRORS_BEFORE_RECONNECT:
            return

        self._errors = 0
        self._set_state(ConnectionState.ERROR_TOO_MANY_ERRORS, exception)
        if self._client is not None and self._client.is_connected:
            self._logger.warning("Disconnecting after too many errors")
            await self._disconnect_client(self._client)

    async def _establish_session(self, client: BleakClient) -> None:
        self._set_state(ConnectionState.ESTABLISHING_SESSION)

        # passed on to data listeners only once decrypted: anyone could test guessed
        # MAC addresses against the encrypted sample
        sample = bytes(
            await client.read_gatt_char(
                _characteristic(client, INFO_CHARACTERISTIC_UUID)
            )
        )
        self._ensure_connected(client)

        address = int(self._address.replace(":", ""), 16)
        for base_mac in candidate_base_macs(self._address, self._base_mac_hint):
            cipher = SessionCipher(derive_session_key(base_mac))
            try:
                plaintext = cipher.decrypt(sample)
            except PacketParseError as e:
                raise SessionKeyError(str(e)) from e

            if _is_valid_info_frame(plaintext):
                self._cipher = cipher
                self._base_mac_from_name = base_mac == self._base_mac_hint
                self._info = packet.decode_response(plaintext)
                self.listeners.on_data_received(
                    _source(INFO_CHARACTERISTIC_UUID), self._info
                )
                self._set_state(ConnectionState.SESSION_ESTABLISHED)
                self._logger.debug(
                    "Session key matched (from advertised name: %s, MAC offset: %d)",
                    self._base_mac_from_name,
                    int.from_bytes(base_mac) - address,
                )
                return

        raise SessionKeyError(
            "No factory MAC candidate produced a session key that decrypts the device "
            "info frame"
        )

    async def _subscribe(self, client: BleakClient) -> None:
        self._set_state(ConnectionState.SUBSCRIBING)

        await client.start_notify(
            _characteristic(client, TELEMETRY_CHARACTERISTIC_UUID),
            self._on_telemetry,
            **self._notify_kwargs(),
        )
        self._ensure_connected(client)
        await self._subscribe_config(client)
        self._ensure_connected(client)

    async def _subscribe_config(self, client: BleakClient) -> None:
        # answers to configuration requests arrive as notifications; telemetry works
        # without them, so a missing or failing subscription only disables the
        # settings that use the configuration channel. A link that dropped meanwhile
        # fails the connection in `_subscribe`.
        if self._config_parse is None:
            return
        config = client.services.get_characteristic(CONFIG_CHARACTERISTIC_UUID)
        if config is None:
            self._logger.warning("Device has no configuration characteristic")
            return
        try:
            await client.start_notify(config, self._on_config, **self._notify_kwargs())
        except BleakError as e:
            self._logger.warning("Could not subscribe to configuration messages: %s", e)

    async def _on_telemetry(self, _: BleakGATTCharacteristic, data: bytearray) -> None:
        # telemetry is not encrypted
        frame = bytes(data)
        self.listeners.on_data_received(_source(TELEMETRY_CHARACTERISTIC_UUID), frame)

        try:
            processed = await self._data_parse(frame)
        except Exception as e:  # noqa: BLE001
            await self.add_error(e)
            return

        self._errors = 0
        if not processed:
            self._logger.debug("Unprocessed frame: %s", frame.hex())

    async def _on_config(self, _: BleakGATTCharacteristic, data: bytearray) -> None:
        frame = bytes(data)
        if self._cipher is None or self._config_parse is None:
            return

        # a notification longer than the ATT MTU allows arrives cut off and cannot be
        # decrypted, e.g. the Wi-Fi status on a link that kept the default MTU
        if len(frame) % _CIPHER_BLOCK_SIZE:
            self.listeners.on_data_received(f"F0C1 truncated to {len(frame)}", b"")
            if not self._warned_truncated_config:
                self._warned_truncated_config = True
                self._logger.warning(
                    "Configuration message truncated to %d bytes, the connection MTU "
                    "is too small to receive it",
                    len(frame),
                )
            return

        try:
            plaintext = self._cipher.decrypt(frame)
            message = packet.ConfigMessage.from_bytes(plaintext)
        except PacketParseError as e:
            self.listeners.on_data_received("F0C1 undecodable", b"")
            self._logger.warning("Could not decode configuration message: %s", e)
            return

        if message.token != self._cipher.token:
            self.listeners.on_data_received("F0C1 foreign token", b"")
            self._logger.warning("Ignoring configuration message with foreign token")
            return

        self.listeners.on_data_received(
            config_source(message.message_type), message.payload
        )

        try:
            await self._config_parse(message)
        except Exception as e:  # noqa: BLE001
            await self.add_error(e)

    def _notify_kwargs(self) -> dict[str, Any]:
        if self.bluez_start_notify:
            return {"bluez": {"use_start_notify": True}}
        return {}

    def _ensure_connected(self, client: BleakClient) -> None:
        # a request can succeed although the link dropped meanwhile, e.g. a
        # subscription; `disconnected` also clears `_client` when bleak reports it
        if self._client is not client or not client.is_connected:
            raise BleakError("Disconnected while establishing the session")

    def _require_session(self) -> tuple[BleakClient, SessionCipher]:
        if self._client is None or not self._client.is_connected:
            raise BleakError("Device is not connected")
        if self._cipher is None:
            raise SessionKeyError("Session is not established")
        return self._client, self._cipher

    async def _fail(self, exc: Exception) -> None:
        # HA retries a failed connect indefinitely, so only the first failure in a row
        # is worth a warning
        level = logging.DEBUG if self._failure_logged else logging.WARNING
        self._failure_logged = True
        # bleak-retry-connector and BlueZ name the device by its addresses
        error = mask_identifiers(str(exc), self._address, self._base_mac_hint)
        self._logger.log(level, "Connection failed: %s", error)
        state = next(state for kind, state in _FAILURE_STATES if isinstance(exc, kind))
        self._set_state(state, exc)
        client, self._client = self._client, None
        if client is not None and client.is_connected:
            await self._disconnect_client(client)

    async def _disconnect_client(self, client: BleakClient) -> None:
        # the link can be gone already, and bleak raises TimeoutError when the end of
        # the link is not confirmed in time, from BlueZ after 10 s
        try:
            await client.disconnect()
        except (EOFError, BleakError, TimeoutError) as e:
            error = mask_identifiers(repr(e), self._address, self._base_mac_hint)
            self._logger.debug("Disconnecting failed: %s", error)

    def _set_state(self, state: ConnectionState, exc: Exception | None = None) -> None:
        self._connection_state = state
        self.listeners.on_state_change(state)
        if state.is_error:
            self.listeners.on_disconnect(exc)


def _characteristic(client: BleakClient, uuid: str) -> BleakGATTCharacteristic:
    if (characteristic := client.services.get_characteristic(uuid)) is None:
        available = [
            f"{c.uuid} {c.properties}" for c in client.services.characteristics.values()
        ]
        raise UnsupportedBluetoothProtocol(uuid, available)
    return characteristic


def _is_valid_info_frame(plaintext: bytes) -> bool:
    # The info response carries a 10-byte plaintext that the firmware zero-pads to a
    # full AES block. Checking the padding in addition to the magic byte makes a false
    # match with a wrong candidate key practically impossible.
    return plaintext[0] == packet.FRAME_MAGIC and plaintext[-4:] == bytes(4)
