from dataclasses import dataclass
from typing import ClassVar, Self

from .exceptions import PacketParseError

FRAME_MAGIC = 0x51


def encode_command(token: int, payload: bytes = b"", flags: int = 0) -> bytes:
    """
    Build the plaintext of a command frame written to a UPS characteristic

    The device accepts the frame only if `flags` is 0 and the session token matches.
    The token is serialized little-endian because the firmware reads it as a native
    32-bit word. The result has to be encrypted with the session cipher before writing.
    """
    return bytes([FRAME_MAGIC, flags]) + token.to_bytes(4, "little") + payload


def decode_response(plaintext: bytes) -> bytes:
    """Validate a decrypted read response and return its payload"""
    if not plaintext or plaintext[0] != FRAME_MAGIC:
        raise PacketParseError(f"Unexpected response frame: {plaintext.hex()}")
    return plaintext[1:]


@dataclass(frozen=True)
class TelemetryFrame:
    """
    Telemetry notification sent in the clear on the telemetry characteristic

    The firmware prepends its internal event word to the measurement payload it
    receives from the power-board MCU and forwards the payload unchanged. On a link with
    the default ATT MTU the notification is truncated to 20 bytes, so a shorter payload
    is accepted and decoded partially.
    """

    SIZE: ClassVar[int] = 40
    HEADER_SIZE: ClassVar[int] = 2

    header: int
    payload: bytes

    @property
    def truncated(self) -> bool:
        return len(self.payload) < self.SIZE - self.HEADER_SIZE

    @classmethod
    def from_bytes(cls, data: bytes) -> Self:
        if len(data) <= cls.HEADER_SIZE:
            raise PacketParseError(f"Telemetry frame without payload: {data.hex()}")
        return cls(
            header=int.from_bytes(data[: cls.HEADER_SIZE], "little"),
            payload=bytes(data[cls.HEADER_SIZE : cls.SIZE]),
        )
