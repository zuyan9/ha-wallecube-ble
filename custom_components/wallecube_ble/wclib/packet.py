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


# status of a command result, any other value means the power board did not answer
COMMAND_CONFIRMED = 0
_COMMAND_RESULT_SIZE = 4


def decode_command_result(frame: bytes) -> int:
    """
    Return the status of the result notified after a command

    The device notifies the frame magic, two zero bytes and the status in the clear on
    the characteristic that received the command, once it has forwarded the command to
    the power-board MCU.
    """
    if len(frame) < _COMMAND_RESULT_SIZE or frame[0] != FRAME_MAGIC:
        raise PacketParseError(f"Unexpected command result: {frame.hex()}")
    return frame[3]


CONFIG_TYPE_FLAG = 0x40
CONFIG_NONCE_SIZE = 2


def encode_config_message(
    token: int, message_type: int, payload: bytes = b"", *, nonce: bytes | None = None
) -> bytes:
    """
    Build the plaintext of a message written to the configuration characteristic

    The configuration channel uses its own header: the message type tagged with
    `0x40`, the payload length and a nonce the device ignores, followed by the session
    token. The result has to be encrypted with the session cipher before writing.
    """
    if nonce is None:
        nonce = os.urandom(CONFIG_NONCE_SIZE)
    if len(nonce) != CONFIG_NONCE_SIZE:
        raise ValueError(f"Nonce must be {CONFIG_NONCE_SIZE} bytes, got {len(nonce)}")
    return (
        bytes([CONFIG_TYPE_FLAG | message_type, len(payload)])
        + nonce
        + token.to_bytes(4, "little")
        + payload
    )


@dataclass(frozen=True)
class ConfigMessage:
    """Decrypted message received on the configuration characteristic"""

    HEADER_SIZE: ClassVar[int] = 8

    message_type: int
    token: int
    payload: bytes

    @classmethod
    def from_bytes(cls, plaintext: bytes) -> Self:
        if (
            len(plaintext) < cls.HEADER_SIZE
            or plaintext[0] & 0xC0 != CONFIG_TYPE_FLAG
            or len(plaintext) < cls.HEADER_SIZE + plaintext[1]
        ):
            raise PacketParseError(f"Unexpected configuration frame: {plaintext.hex()}")
        return cls(
            message_type=plaintext[0] & 0x3F,
            token=int.from_bytes(plaintext[4:8], "little"),
            payload=plaintext[cls.HEADER_SIZE : cls.HEADER_SIZE + plaintext[1]],
        )


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
    def event(self) -> int:
        """Event byte, set only on the sample where input power is lost or returns"""
        return self.header >> 8

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
