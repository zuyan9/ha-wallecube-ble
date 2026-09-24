import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass

from Crypto.Cipher import AES

from .exceptions import PacketParseError
from .keydata import SESSION_SECRET

# The firmware advertises "Walle-" followed by the upper-case hex of its factory (base)
# MAC, which is the same string it hashes into the session key
LOCAL_NAME_PREFIX = "Walle-"
_LOCAL_NAME_RE = re.compile(rf"^{LOCAL_NAME_PREFIX}([0-9A-Fa-f]{{12}})$")

# Fallback when the advertised name is not available: ESP-IDF derives the Bluetooth
# address from the base MAC with an offset that depends on how many universal
# addresses the firmware reserves, so the likely candidates are tried in this order
_BASE_MAC_OFFSETS = (-2, -1, 0, -3, 1, 2)

_ZERO_IV = bytes(AES.block_size)


@dataclass(frozen=True)
class SessionKey:
    """AES key and the 32-bit token the device expects in every command frame"""

    key: bytes
    token: int


def derive_session_key(base_mac: bytes) -> SessionKey:
    """
    Derive the session key from the device's factory MAC address

    The firmware hashes the MAC as upper-case hex together with a static secret. The
    token is the big-endian value of digest bytes 8-11, which the firmware stores and
    compares as a native (little-endian) word.
    """
    material = base_mac.hex().upper().encode("ascii") + SESSION_SECRET
    digest = hashlib.md5(material, usedforsecurity=False).digest()
    return SessionKey(key=digest, token=int.from_bytes(digest[8:12], "big"))


def base_mac_from_local_name(local_name: str | None) -> bytes | None:
    """Return the factory MAC encoded in the advertised name, if present"""
    if local_name is None or (match := _LOCAL_NAME_RE.match(local_name)) is None:
        return None
    return bytes.fromhex(match.group(1))


def candidate_base_macs(address: str, hint: bytes | None = None) -> Iterator[bytes]:
    """
    Yield plausible factory MAC addresses, most likely first

    Parameters
    ----------
    address
        Advertised BLE address of the device
    hint, optional
        Factory MAC parsed from the advertised name, tried before any derived guess
    """
    seen = set()
    if hint is not None:
        seen.add(hint)
        yield hint

    value = int(address.replace(":", "").replace("-", ""), 16)
    for offset in _BASE_MAC_OFFSETS:
        if not 0 <= (candidate_value := value + offset) < 1 << 48:
            continue
        if (candidate := candidate_value.to_bytes(6, "big")) not in seen:
            seen.add(candidate)
            yield candidate


class SessionCipher:
    """AES-128-CBC with zero IV and zero padding, as implemented by the firmware"""

    def __init__(self, session_key: SessionKey) -> None:
        self._session_key = session_key

    @property
    def token(self) -> int:
        return self._session_key.token

    @property
    def key(self) -> bytes:
        return self._session_key.key

    def encrypt(self, plaintext: bytes) -> bytes:
        if remainder := len(plaintext) % AES.block_size:
            plaintext += bytes(AES.block_size - remainder)
        return self._cipher().encrypt(plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        if not ciphertext or len(ciphertext) % AES.block_size:
            raise PacketParseError(
                f"Ciphertext is not block aligned: {len(ciphertext)} bytes"
            )
        return self._cipher().decrypt(ciphertext)

    def _cipher(self):
        return AES.new(self._session_key.key, AES.MODE_CBC, iv=_ZERO_IV)
