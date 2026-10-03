import logging
import re
import time
from collections import deque
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .connection import ConnectionState
    from .devicebase import DeviceBase

# messages kept for diagnostics in each direction, about 3.5 min of telemetry
_FRAMES_KEPT = 200


def device_logger(name: str, address: str) -> logging.Logger:
    """
    Logger of a module for one device

    It is named after the last four digits of the address, like the device's default
    name, so messages of two devices can be told apart without the whole address.
    """
    return logging.getLogger(f"{name}.{address.replace(':', '')[-4:].upper()}")


def mask_address(address: str) -> str:
    """Keep the vendor part of a MAC address and hide the device-specific part"""
    octets = re.findall(r"[0-9A-Fa-f]{2}", address)
    if len(octets) != 6:
        return "*" * len(address)
    delimiter = address[2] if len(address) == 17 else ""
    return delimiter.join([*octets[:3], "**", "**", "**"])


def mask_local_name(local_name: str | None) -> str | None:
    """Mask the factory MAC that the device embeds in its advertised name"""
    if local_name is None:
        return None
    prefix, _, suffix = local_name.rpartition("-")
    if prefix and re.fullmatch(r"[0-9A-Fa-f]{12}", suffix):
        return f"{prefix}-{mask_address(suffix)}"
    return local_name


def mask_identifiers(text: str, address: str, base_mac: bytes | None) -> str:
    """Hide the device-specific part of a device's addresses in a text"""
    identifiers = [address] if base_mac is None else [address, base_mac.hex()]
    for identifier in identifiers:
        octets = re.findall(r"[0-9A-Fa-f]{2}", identifier)
        # also the forms with "_" or "-" between the octets, e.g. in BlueZ object paths
        pattern = re.compile("[:_-]?".join(octets), re.IGNORECASE)
        text = pattern.sub(lambda match: mask_address(match.group(0)), text)
    return text


class DeviceDiagnosticsCollector:
    """
    Keeps the recent connection history and data exchanged for diagnostics

    Nothing it keeps identifies the device: payloads are stored decrypted, without
    the session token and redacted by the device, and addresses are masked. Encrypted
    frames are never stored, they would let anyone recover the device's MAC address
    and with it the session key.
    """

    def __init__(self, device: "DeviceBase"):
        self._device = device
        self._start = time.monotonic()
        # state changes and the errors that ended a connection
        self._history: deque[dict[str, float | str]] = deque(maxlen=50)
        # time, source and payload, see `connection.DataReceivedListener`
        self._frames_received: deque[tuple[float, str, bytes]] = deque(
            maxlen=_FRAMES_KEPT
        )
        self._frames_sent: deque[tuple[float, str, bytes]] = deque(maxlen=_FRAMES_KEPT)

        listeners = device.listeners
        listeners.on_state_change.add(self._on_state_change)
        listeners.on_disconnect.add(self._on_disconnect)
        listeners.on_data_received.add(self._on_data_received)
        listeners.on_data_send.add(self._on_data_send)

    def build_diagnostics_dict(self) -> dict[str, Any]:
        """Assemble diagnostics with device identifiers masked"""
        device = self._device
        return {
            "device": device.device,
            "name": _mask_name(device.name, device.identifier),
            # the advertised name embeds the factory MAC
            "local_name": mask_local_name(device.local_name),
            "address": mask_address(device.address),
            "connection_state": device.connection_state,
            "connection_history": list(self._history),
            "session": device.session_info,
            "frames_received": [
                (t, source, payload.hex())
                for t, source, payload in self._frames_received
            ],
            "frames_sent": [
                (t, target, payload.hex()) for t, target, payload in self._frames_sent
            ],
        }

    @property
    def _now(self) -> float:
        return round(time.monotonic() - self._start, 3)

    def _on_state_change(self, state: "ConnectionState"):
        self._history.append({"time": self._now, "state": state.name})

    def _on_disconnect(self, exc: Exception | None):
        if exc is not None:
            device = self._device
            error = mask_identifiers(repr(exc), device.address, device.base_mac_hint)
            self._history.append({"time": self._now, "error": error})

    def _on_data_received(self, source: str, payload: bytes):
        payload = self._device.redact_payload(source, bytes(payload))
        self._frames_received.append((self._now, source, payload))

    def _on_data_send(self, target: str, payload: bytes):
        payload = self._device.redact_payload(target, bytes(payload))
        self._frames_sent.append((self._now, target, payload))


def _mask_name(name: str, identifier: str) -> str:
    # the default name ends with the last four digits of the address
    suffix = identifier[-4:]
    if name.upper().endswith(suffix):
        return name[: -len(suffix)] + "*" * len(suffix)
    return name
