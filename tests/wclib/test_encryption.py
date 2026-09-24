import pytest

from custom_components.wallecube_ble.wclib.encryption import (
    SessionCipher,
    base_mac_from_local_name,
    candidate_base_macs,
    derive_session_key,
)
from custom_components.wallecube_ble.wclib.exceptions import PacketParseError
from custom_components.wallecube_ble.wclib.keydata import SESSION_SECRET
from custom_components.wallecube_ble.wclib.packet import (
    FRAME_MAGIC,
    decode_response,
    encode_command,
)

# vectors computed from the firmware key schedule for two addresses of one unit
KEY_VECTORS = [
    ("0a1b2c3d4e50", "12226d8d36e6bfcd93be41626e885160", 0x93BE4162),
    ("0a1b2c3d4e52", "74711d0562d0c80ca1c8baf7302a1a99", 0xA1C8BAF7),
]


def test_session_secret_length():
    assert len(SESSION_SECRET) == 123


@pytest.mark.parametrize(("base_mac", "key", "token"), KEY_VECTORS)
def test_derive_session_key(base_mac: str, key: str, token: int):
    session_key = derive_session_key(bytes.fromhex(base_mac))

    assert session_key.key.hex() == key
    assert session_key.token == token


def test_token_is_big_endian_digest_bytes_8_to_11():
    session_key = derive_session_key(bytes.fromhex("0a1b2c3d4e50"))

    assert session_key.token == int.from_bytes(session_key.key[8:12], "big")


def test_command_frame_serializes_token_little_endian():
    session_key = derive_session_key(bytes.fromhex("0a1b2c3d4e50"))

    frame = encode_command(session_key.token, b"\x01", nonce=b"\xa1\xa2\xa3\xa4")

    # the firmware compares the token as a native little-endian word, so the frame
    # carries digest bytes 8-11 in reverse order
    assert frame[:2] == bytes([FRAME_MAGIC, 0x00])
    assert frame[2:6] == session_key.key[8:12][::-1]
    assert frame[6:10] == b"\xa1\xa2\xa3\xa4"
    assert frame[10:] == b"\x01"


def test_command_frame_places_payload_after_random_nonce():
    first = encode_command(0x01020304, b"\x02")
    second = encode_command(0x01020304, b"\x02")

    # the firmware reads the payload at offset 10 regardless of the nonce
    assert len(first) == len(second) == 11
    assert first[10:] == second[10:] == b"\x02"


def test_command_frame_rejects_wrong_nonce_size():
    with pytest.raises(ValueError, match="Nonce"):
        encode_command(0, nonce=b"\x00")


def test_cipher_round_trip_zero_pads_to_block_size():
    cipher = SessionCipher(derive_session_key(bytes.fromhex("0a1b2c3d4e50")))

    ciphertext = cipher.encrypt(b"\x51\x02")

    assert len(ciphertext) == 16
    assert cipher.decrypt(ciphertext) == b"\x51\x02" + bytes(14)


def test_cipher_rejects_unaligned_ciphertext():
    cipher = SessionCipher(derive_session_key(bytes.fromhex("0a1b2c3d4e50")))

    with pytest.raises(PacketParseError):
        cipher.decrypt(bytes(15))


def test_decode_response_strips_magic():
    assert decode_response(b"\x51\x07\x08") == b"\x07\x08"

    with pytest.raises(PacketParseError):
        decode_response(b"\x50\x07")


@pytest.mark.parametrize(
    ("local_name", "expected"),
    [
        ("Walle-0A1B2C3D4E50", "0a1b2c3d4e50"),
        ("Walle-0a1b2c3d4e50", "0a1b2c3d4e50"),
        ("Walle_DCUPS", None),
        ("Walle-0A1B2C3D4E", None),
        (None, None),
    ],
)
def test_base_mac_from_local_name(local_name: str | None, expected: str | None):
    base_mac = base_mac_from_local_name(local_name)

    assert (base_mac.hex() if base_mac is not None else None) == expected


def test_candidate_base_macs_tries_hint_first_without_duplicates():
    hint = bytes.fromhex("0a1b2c3d4e50")

    candidates = list(candidate_base_macs("0A:1B:2C:3D:4E:52", hint))

    assert candidates[0] == hint
    assert len(candidates) == len(set(candidates))
    assert bytes.fromhex("0a1b2c3d4e52") in candidates


def test_candidate_base_macs_without_hint_starts_with_bt_offset():
    candidates = list(candidate_base_macs("0A:1B:2C:3D:4E:52"))

    assert candidates[0] == bytes.fromhex("0a1b2c3d4e50")
