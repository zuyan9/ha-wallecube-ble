import logging
import re
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Flag, auto
from functools import cached_property
from typing import TYPE_CHECKING, Any

import bleak

if TYPE_CHECKING:
    from .connection import Connection, ConnectionState
    from .devicebase import DeviceBase


class LogOptions(Flag):
    MASKED = auto()

    ENCRYPTED_PAYLOADS = auto()
    DECRYPTED_PAYLOADS = auto()
    PACKETS = auto()
    DESERIALIZED_MESSAGES = auto()

    CONNECTION_DEBUG = auto()
    BLEAK_DEBUG = auto()

    @property
    def enabled(self):
        return self & (
            LogOptions.ENCRYPTED_PAYLOADS
            | LogOptions.DECRYPTED_PAYLOADS
            | LogOptions.PACKETS
            | LogOptions.DESERIALIZED_MESSAGES
            | LogOptions.CONNECTION_DEBUG
        )

    @staticmethod
    def no_options():
        return LogOptions(0)


type MaskFunc = Callable[[str], str | None]


def mask_address(address: str) -> str:
    """Keep the vendor part of a BLE address and hide the device-specific part"""
    delimiter = ":" if ":" in address else ""
    octets = re.findall(r"[0-9A-Fa-f]{2}", address)
    if len(octets) != 6:
        return "*" * len(address)
    return delimiter.join([*octets[:3], "**", "**", "**"])


def mask_local_name(local_name: str | None) -> str | None:
    """Mask the factory MAC that the device embeds in its advertised name"""
    if local_name is None:
        return None
    prefix, _, suffix = local_name.rpartition("-")
    if prefix and re.fullmatch(r"[0-9A-Fa-f]{12}", suffix):
        return f"{prefix}-{mask_address(suffix)}"
    return local_name


def _address_masker(address: str) -> MaskFunc:
    octets = re.findall(r"[0-9A-Fa-f]{2}", address)
    patterns = [
        re.compile(":".join(octets), re.IGNORECASE),
        re.compile("".join(octets), re.IGNORECASE),
    ]

    def _mask(message: str) -> str | None:
        masked = message
        for pattern in patterns:
            masked = pattern.sub(lambda m: mask_address(m.group(0)), masked)
        return masked if masked != message else None

    return _mask


class SensitiveMaskingFilter(logging.Filter):
    def __init__(self, mask_funcs: Sequence[MaskFunc], name: str = "") -> None:
        super().__init__(name)
        self._mask_funcs = mask_funcs

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.mask_message(record.msg)
        record.name = self.mask_message(record.name)

        if isinstance(record.args, Mapping):
            record.args = {k: self.mask_message(v) for k, v in record.args.items()}
        elif record.args is not None:
            record.args = tuple(self.mask_message(v) for v in record.args)

        return True

    def mask_message(self, message: Any) -> Any:
        text = message if isinstance(message, str) else str(message)
        replaced = False
        for mask in self._mask_funcs:
            if (masked := mask(text)) is not None:
                text = masked
                replaced = True
        return text if replaced else message


_BLEAK_LOGGER = logging.getLogger(bleak.__name__)
_ORIGINAL_BLEAK_LOG_LEVEL = _BLEAK_LOGGER.level


class MaskingLogger:
    """Logger wrapper that supports log options and masking of identifiers"""

    def __init__(self, logger: logging.Logger, mask_funcs: Sequence[MaskFunc]):
        self._logger = logger
        self._mask_funcs = mask_funcs
        self._options = LogOptions.no_options()

    @cached_property
    def _mask_filter(self):
        return SensitiveMaskingFilter(self._mask_funcs)

    def __getattr__(self, name: str):
        return getattr(self._logger, name)

    @property
    def options(self):
        return self._options

    def set_options(self, options: LogOptions):
        self._options = options
        self._logger.setLevel(logging.DEBUG if options.enabled else logging.INFO)

        for handler in logging.root.handlers:
            if LogOptions.MASKED in options:
                if self._mask_filter not in handler.filters:
                    handler.addFilter(self._mask_filter)
            elif self._mask_filter in handler.filters:
                handler.removeFilter(self._mask_filter)

        if LogOptions.BLEAK_DEBUG in options:
            _BLEAK_LOGGER.setLevel(logging.DEBUG)
        elif _BLEAK_LOGGER.isEnabledFor(logging.DEBUG):
            _BLEAK_LOGGER.setLevel(_ORIGINAL_BLEAK_LOG_LEVEL)

    def log_filtered(
        self,
        options: LogOptions,
        msg: object,
        *args: object,
        level: int = logging.DEBUG,
    ) -> None:
        """Log message only if all of the given log options are enabled"""
        if options in self._options:
            args = tuple(
                _LazyHex(a) if isinstance(a, bytes | bytearray) else a for a in args
            )
            self._logger.log(level, msg, *args)


def _identity_maskers(address: str, base_mac: bytes | None) -> list[MaskFunc]:
    maskers = [_address_masker(address)]
    if base_mac is not None:
        maskers.append(_address_masker(base_mac.hex()))
    return maskers


class DeviceLogger(MaskingLogger):
    def __init__(self, device: "DeviceBase"):
        super().__init__(
            logging.getLogger(f"{device.__module__} - {device.address}"),
            mask_funcs=_identity_maskers(device.address, device.base_mac_hint),
        )


class ConnectionLogger(MaskingLogger):
    def __init__(self, connection: "Connection") -> None:
        super().__init__(
            logging.getLogger(f"{connection.__module__} - {connection.address}"),
            mask_funcs=_identity_maskers(connection.address, connection.base_mac_hint),
        )


@dataclass
class ConnectionLog:
    """Bounded history of connection state changes, relative to creation time"""

    maxlen: int = 20
    history: deque[dict[str, float | str]] = field(init=False)

    def __post_init__(self):
        self._start = time.monotonic()
        self.history = deque(maxlen=self.maxlen)

    def append(self, state: "ConnectionState", reason: str | None = None):
        entry: dict[str, float | str] = {
            "time": round(time.monotonic() - self._start, 3),
            "state": state.name,
        }
        if reason:
            entry["reason"] = reason
        self.history.append(entry)


class DeviceDiagnosticsCollector:
    """Collects raw frames, errors and connection events for diagnostics"""

    def __init__(self, device: "DeviceBase", buffer_size: int = 100):
        self._device = device
        self._enabled = False
        self._start = time.monotonic()

        self._frames_received: deque[tuple[float, bytes]] = deque(maxlen=buffer_size)
        self._frames_sent: deque[tuple[float, bytes]] = deque(maxlen=buffer_size)
        self._errors: deque[tuple[float, str]] = deque(maxlen=buffer_size)
        self._connect_times: deque[float] = deque(maxlen=buffer_size)
        self._disconnect_times: deque[float] = deque(maxlen=buffer_size)
        self._unlisten: list[Callable[[], None]] = []

    @property
    def is_enabled(self):
        return self._enabled

    def enabled(self, enabled: bool = True):
        """Start or stop collecting data by (un)subscribing from device events"""
        if enabled == self._enabled:
            return self

        self._enabled = enabled
        self._clear_buffers()
        for unlisten in self._unlisten:
            unlisten()
        self._unlisten.clear()

        if enabled:
            self._start = time.monotonic()
            self._unlisten.extend(
                [
                    self._device.on_connection_state_change(self._on_state_change),
                    self._device.on_disconnect(self._on_disconnect),
                    self._device.on_data_received(self._on_data_received),
                    self._device.on_data_send(self._on_data_send),
                ]
            )
        return self

    def with_buffer_size(self, buffer_size: int):
        self._frames_received = deque(self._frames_received, maxlen=buffer_size)
        self._frames_sent = deque(self._frames_sent, maxlen=buffer_size)
        self._errors = deque(self._errors, maxlen=buffer_size)
        self._connect_times = deque(self._connect_times, maxlen=buffer_size)
        self._disconnect_times = deque(self._disconnect_times, maxlen=buffer_size)
        return self

    @property
    def packet_buffer_size(self) -> int:
        return self._frames_received.maxlen or 0

    @property
    def packets_collected(self) -> int:
        return len(self._frames_received)

    def add_error(self, message: str):
        self._errors.append((self._now, message))

    def build_diagnostics_dict(self) -> dict[str, Any]:
        """Assemble diagnostics with device identifiers masked"""
        device = self._device
        result: dict[str, Any] = {
            "device": device.device,
            "name": device.name,
            "address": mask_address(device.address),
            "connection_state": device.connection_state,
            "connection_state_history": list(device.connection_log.history),
            "session": device.session_info,
        }
        if self._enabled:
            result |= {
                "frames_received": [(t, f.hex()) for t, f in self._frames_received],
                "frames_sent": [(t, f.hex()) for t, f in self._frames_sent],
                "errors": list(self._errors),
                "connect_times": list(self._connect_times),
                "disconnect_times": list(self._disconnect_times),
            }
        return result

    @property
    def _now(self):
        return round(time.monotonic() - self._start, 3)

    def _on_state_change(self, state: "ConnectionState"):
        if state.authenticated:
            self._connect_times.append(self._now)

    def _on_disconnect(self, exc: Exception | type[Exception] | None = None):
        self._disconnect_times.append(self._now)
        if exc is not None:
            self.add_error(repr(exc))

    def _on_data_received(self, data: bytes, state: "ConnectionState"):
        self._frames_received.append((self._now, bytes(data)))

    def _on_data_send(self, data: bytes):
        self._frames_sent.append((self._now, bytes(data)))

    def _clear_buffers(self):
        self._frames_received.clear()
        self._frames_sent.clear()
        self._errors.clear()
        self._connect_times.clear()
        self._disconnect_times.clear()


class _LazyHex:
    __slots__ = ("_data",)

    def __init__(self, data: bytes | bytearray) -> None:
        self._data = data

    def __str__(self) -> str:
        return bytes(self._data).hex()

    __repr__ = __str__
