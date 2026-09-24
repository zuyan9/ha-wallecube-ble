import os
from dataclasses import dataclass
from typing import ClassVar, Self

from .exceptions import PacketParseError

FRAME_MAGIC = 0x51
NONCE_SIZE = 4


def encode_command(
    token: int, payload: bytes = b"", flags: int = 0, *, nonce: bytes | None = None
) -> bytes:
    """
    Build the plaintext of a command frame written to a UPS characteristic

    The device accepts the frame only if `flags` is 0 and the session token matches.
    The token is serialized little-endian because the firmware reads it as a native
    32-bit word. The device ignores the four bytes between the token and the payload;
    the vendor app fills them with random data, which also varies the first cipher
    block. The result has to be encrypted with the session cipher before writing.
    """
    if nonce is None:
        nonce = os.urandom(NONCE_SIZE)
    if len(nonce) != NONCE_SIZE:
        raise ValueError(f"Nonce must be {NONCE_SIZE} bytes, got {len(nonce)}")
    return bytes([FRAME_MAGIC, flags]) + token.to_bytes(4, "little") + nonce + payload


def decode_response(plaintext: bytes) -> bytes:
    """Validate a decrypted read response and return its payload"""
    if not plaintext or plaintext[0] != FRAME_MAGIC:
        raise PacketParseError(f"Unexpected response frame: {plaintext.hex()}")
    return plaintext[1:]


@dataclass(frozen=True)
class TelemetryFrame:
    """
    Telemetry notification sent in the clear on the telemetry characteristic

    The firmware prepends the frame magic and an event byte to the measurement payload
    it receives from the power-board MCU and forwards the payload unchanged. On a link
    with the default ATT MTU the notification is truncated to 20 bytes, so a shorter
    payload is accepted and decoded partially.
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
