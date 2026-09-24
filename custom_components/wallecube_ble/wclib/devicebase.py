import abc
import asyncio
import time
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .connection import (
    Connection,
    ConnectionState,
    ConnectionStateListener,
    DataReceivedListener,
    DataSendListener,
    DisconnectListener,
)
from .encryption import base_mac_from_local_name
from .listeners import ListenerGroup, ListenerRegistry
from .logging_util import (
    ConnectionLog,
    DeviceDiagnosticsCollector,
    DeviceLogger,
    LogOptions,
)
from .packet import ConfigMessage
from .props.updatable_props import Field

# first messages after connecting are passed through immediately even when an update
# period is set, otherwise entities would stay unknown until the first period ends
_UNTHROTTLED_SECONDS = 5


class _Listeners(ListenerRegistry):
    on_disconnect: ListenerGroup[DisconnectListener]
    on_connection_state_change: ListenerGroup[ConnectionStateListener]
    on_data_received: ListenerGroup[DataReceivedListener]
    on_data_send: ListenerGroup[DataSendListener]


class DeviceBase(abc.ABC):
    """Device Base"""

    NAME_PREFIX: str

    _listeners = _Listeners.create()

    @classmethod
    @abc.abstractmethod
    def check(cls, adv_data: AdvertisementData) -> bool:
        """Return True if the advertisement belongs to this device type"""

    def __init__(self, ble_dev: BLEDevice, adv_data: AdvertisementData) -> None:
        self._ble_dev = ble_dev
        self._address = ble_dev.address
        # the device advertises no serial number, the BLE address is its stable id
        self._identifier = self._address.replace(":", "").upper()
        self._default_name = self.NAME_PREFIX + self._identifier[-4:]
        self._name = self._default_name
        self._local_name = adv_data.local_name
        self._base_mac_hint = base_mac_from_local_name(adv_data.local_name)

        self._logger = DeviceLogger(self)
        self._logger.debug("Creating new device: %s", self.device)

        self._conn: Connection | None = None
        self._connection_event = asyncio.Event()
        self._callbacks: set[Callable[[], None]] = set()
        self._callbacks_map: dict[str, set[Callable[[], None]]] = defaultdict(set)
        self._state_update_callbacks: dict[str, set[Callable[[Any], None]]] = (
            defaultdict(set)
        )
        self._update_period = 0
        self._last_updated = 0.0
        self._props_to_update: set[str] = set()
        self._wait_until_throttle: float | None = 0

        self._reconnect_disabled = False
        self._refresh_task: asyncio.Task | None = None
        self._options = Connection.Options()
        self._connection_log = ConnectionLog()
        self._diagnostics = DeviceDiagnosticsCollector(self)

    @property
    def device(self) -> str:
        return self.__doc__ or ""

    @property
    def address(self) -> str:
        return self._address

    @property
    def identifier(self) -> str:
        """Stable identifier used for unique ids"""
        return self._identifier

    @property
    def name(self) -> str:
        return self._name

    @property
    def local_name(self) -> str | None:
        return self._local_name

    @property
    def is_connected(self) -> bool:
        return self._conn is not None and self._conn.is_connected

    @property
    def connection_state(self) -> ConnectionState | None:
        return None if self._conn is None else self._conn.state

    @property
    def session_established(self) -> bool:
        return self._conn is not None and self._conn.session_established

    @property
    def base_mac_hint(self) -> bytes | None:
        """Factory MAC parsed from the advertised name, if it was available"""
        return self._base_mac_hint

    @property
    def session_info(self) -> dict[str, Any] | None:
        """Non-sensitive details about how the session key was established"""
        if self._conn is None or not self._conn.session_established:
            return None
        return {
            "from_advertised_name": self._conn.base_mac_from_name,
            "base_mac_offset": self._conn.base_mac_offset,
            "info": self._conn.info.hex(),
        }

    @property
    def diagnostics(self) -> DeviceDiagnosticsCollector:
        return self._diagnostics

    @property
    def connection_log(self) -> ConnectionLog:
        return self._connection_log

    def set_connection_state(
        self, state: ConnectionState, exc: Exception | type[Exception] | None = None
    ) -> None:
        if self._conn is not None:
            self._conn.set_state(state, exc)

    def update_ble_device(self, ble_dev: BLEDevice):
        self._ble_dev = ble_dev
        if self._conn is not None:
            self._conn.update_ble_device(ble_dev)

    def with_update_period(self, period: int):
        self._update_period = period
        return self

    def with_logging_options(self, options: LogOptions):
        self._logger.set_options(options)
        if self._conn is not None:
            self._conn.with_logging_options(options)
        return self

    def with_disabled_reconnect(self, is_disabled: bool = True):
        self._reconnect_disabled = is_disabled
        if self._conn is not None:
            self._conn.with_disabled_reconnect(is_disabled)
        return self

    def with_connection_options(self, options: Connection.Options):
        self._options = options
        if self._conn is not None:
            self._conn.with_options(options)
        return self

    def with_enabled_packet_diagnostics(
        self, enabled: bool = True, buffer_size: int = 100
    ):
        self._diagnostics.enabled(enabled)
        self._diagnostics.with_buffer_size(buffer_size)
        return self

    def with_name(self, name: str):
        self._name = name
        return self

    async def data_parse(self, frame: bytes) -> bool:
        """Parse a telemetry frame and update fields, return True if processed"""
        return False

    async def config_parse(self, message: ConfigMessage) -> bool:
        """Parse a configuration-channel message, return True if processed"""
        return False

    async def refresh_settings(self) -> None:
        """Read the device settings, called after every successful connection"""

    async def connect(self, max_attempts: int | None = None) -> None:
        if self._conn is None:
            self._conn = (
                Connection(
                    ble_dev=self._ble_dev,
                    data_parse=self.data_parse,
                    base_mac_hint=self._base_mac_hint,
                    config_parse=self.config_parse,
                )
                .with_logging_options(self._logger.options)
                .with_disabled_reconnect(self._reconnect_disabled)
                .with_options(self._options)
            )
            self._connection_event.set()
            self._logger.info("Connecting to %s", self.device)

            self._conn.on_disconnect(self._listeners.on_disconnect)
            self._conn.on_state_change(self._listeners.on_connection_state_change)
            self._conn.on_state_change(self._connection_log.append)
            self._conn.on_data_received(self._listeners.on_data_received)
            self._conn.on_data_send(self._listeners.on_data_send)
            self._conn.on_state_change(self._on_connection_state)

        await self._conn.connect(max_attempts=max_attempts)

    async def disconnect(self) -> None:
        if self._conn is None:
            self._logger.error("Device has no connection")
            return

        self._cancel_refresh()
        await self._conn.disconnect()
        self._connection_event.clear()
        self._conn = None

    async def wait_until_authenticated_or_error(
        self, raise_on_error: bool = False, return_exc: bool = False
    ):
        if self._conn is None:
            state = ConnectionState.NOT_CONNECTED
            return (state, None) if return_exc else state

        return await self._conn.wait_until_authenticated_or_error(
            raise_on_error=raise_on_error, return_exc=return_exc
        )

    async def observe_connection(self):
        while self._conn is None:
            yield ConnectionState.NOT_CONNECTED
            await self._connection_event.wait()

        async for state in self._conn.observe_connection():
            yield state

    async def read_value(self, characteristic: str) -> bytes:
        if self._conn is None:
            raise ConnectionError("Device has no connection")
        return await self._conn.read_value(characteristic)

    async def send_command(self, characteristic: str, payload: bytes = b"") -> None:
        if self._conn is None:
            raise ConnectionError("Device has no connection")
        await self._conn.send_command(characteristic, payload)

    async def send_config(self, message_type: int, payload: bytes = b"") -> None:
        if self._conn is None:
            raise ConnectionError("Device has no connection")
        await self._conn.send_config(message_type, payload)

    def _on_connection_state(self, state: ConnectionState) -> None:
        # settings can change from the device menu or the vendor app while HA is
        # disconnected, so they are read again after every connect
        if state is not ConnectionState.AUTHENTICATED:
            return
        # a reconnect can authenticate again while the previous refresh still waits
        # on the dropped link
        self._cancel_refresh()
        self._refresh_task = asyncio.get_running_loop().create_task(
            self._refresh_settings()
        )

    def _cancel_refresh(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            self._refresh_task = None

    async def _refresh_settings(self) -> None:
        try:
            await self.refresh_settings()
        except Exception as e:  # noqa: BLE001
            self._logger.warning("Could not read device settings: %s", e)
            self._logger.debug("Settings refresh failed", exc_info=True)

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

    def on_connection_state_change(self, listener: ConnectionStateListener):
        return self._listeners.on_connection_state_change.add(listener)

    def on_data_received(self, listener: DataReceivedListener):
        return self._listeners.on_data_received.add(listener)

    def on_data_send(self, listener: DataSendListener):
        return self._listeners.on_data_send.add(listener)

    def register_callback(
        self, callback: Callable[[], None], propname: str | None = None
    ) -> None:
        """Register callback that is called when the given property changes"""
        if propname is None:
            self._callbacks.add(callback)
        else:
            self._callbacks_map[propname].add(callback)

    def remove_callback(
        self, callback: Callable[[], None], propname: str | None = None
    ) -> None:
        if propname is None:
            self._callbacks.discard(callback)
        else:
            self._callbacks_map[propname].discard(callback)

    def update_callback(self, propname: "str | Field[Any]") -> None:
        """Call callbacks registered for the property, throttled by update period"""
        if isinstance(propname, Field):
            propname = propname.public_name

        self._props_to_update.add(propname)

        if self._update_period != 0:
            now = time.time()
            if now - self._last_updated < self._update_period:
                if self._wait_until_throttle is None:
                    return
                if self._wait_until_throttle == 0:
                    self._wait_until_throttle = now + _UNTHROTTLED_SECONDS
                elif self._wait_until_throttle < now:
                    self._wait_until_throttle = None
            self._last_updated = now

        for prop in self._props_to_update:
            for callback in self._callbacks_map.get(prop, set()):
                callback()
        for callback in self._callbacks:
            callback()

        self._props_to_update.clear()

    def register_state_update_callback(
        self, state_update_callback: Callable[[Any], None], propname: str
    ):
        """Register callback that receives the new value of the property"""
        self._state_update_callbacks[propname].add(state_update_callback)

    def remove_state_update_callback(
        self, callback: Callable[[Any], None], propname: str
    ):
        self._state_update_callbacks[propname].discard(callback)

    def update_state(self, propname: "str | Field[Any]", value: Any):
        if isinstance(propname, Field):
            propname = propname.public_name

        for update in self._state_update_callbacks.get(propname, set()):
            update(value)
