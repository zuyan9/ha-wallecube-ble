"""
Fake WalleCube UPS shared by the library and the Home Assistant tests

Frames are encrypted with the real session cipher of the synthetic factory MAC.
"""

import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from bleak.backends.device import BLEDevice

from custom_components.wallecube_ups_ble.wclib.connection import (
    TELEMETRY_CHARACTERISTIC_UUID,
)
from custom_components.wallecube_ups_ble.wclib.encryption import (
    SessionCipher,
    derive_session_key,
)

ADDRESS = "0A:1B:2C:3D:4E:52"
BASE_MAC = bytes.fromhex("0a1b2c3d4e50")
LOCAL_NAME = "Walle-0A1B2C3D4E50"
CIPHER = SessionCipher(derive_session_key(BASE_MAC))

# info block of a W150 after the magic byte: uninitialized byte, power-board hardware
# 3 and firmware 29, front-panel hardware 3 and firmware 19
W150_INFO = bytes.fromhex("00 0300 1d00 0300 1300")


def ble_device() -> BLEDevice:
    return BLEDevice(ADDRESS, LOCAL_NAME, None)


def advertisement(*service_uuids: str) -> MagicMock:
    return MagicMock(local_name=LOCAL_NAME, service_uuids=list(service_uuids))


def encrypted(plaintext: bytes, cipher: SessionCipher = CIPHER) -> bytearray:
    return bytearray(cipher.encrypt(plaintext))


def config_frame(
    message_type: int, payload: bytes, token: int = CIPHER.token
) -> bytearray:
    """Configuration message as the front panel notifies it"""
    header = bytes([0x40 | message_type, len(payload), 0x12, 0x34])
    return encrypted(header + token.to_bytes(4, "little") + payload)


def telemetry_frame(
    *,
    input_mv: int = 12_150,
    input_ma: int = 2_210,
    output_mv: int = 12_020,
    output_ma: int = 1_530,
    battery_permille: int = 875,
    battery_mv: int = 12_480,
    cells_mv: tuple[int, int, int, int] = (3_122, 3_118, 3_122, 3_117),
    battery_ma: int = -1_450,
    temperature_decidegrees: int = 253,
    cycles: int = 12,
    remaining_seconds: int = 7_260,
    energy_raw: int = 1_234_567,
    fault_flags: int = 0,
    status_flags: int = 0,
    event: int = 0,
) -> bytes:
    """Build a 40-byte telemetry notification: magic, event byte, 38-byte payload"""
    payload = struct.pack(
        "<HHHHHH4HhhHHIIH",
        input_mv,
        input_ma,
        output_mv,
        output_ma,
        battery_permille,
        battery_mv,
        *cells_mv,
        battery_ma,
        temperature_decidegrees,
        cycles,
        remaining_seconds,
        energy_raw,
        fault_flags,
        status_flags,
    )
    return bytes([0x51, event]) + payload


def ups_client(info: bytes = W150_INFO, *, missing: str | None = None) -> MagicMock:
    """Connected BleakClient of a UPS, reads return the encrypted info block"""
    client = MagicMock(is_connected=True)
    client.read_gatt_char = AsyncMock(return_value=encrypted(b"\x51" + info))
    client.write_gatt_char = AsyncMock()
    client.start_notify = AsyncMock()
    client.stop_notify = AsyncMock()
    client.disconnect = AsyncMock()
    client.services.characteristics = {}
    client.services.get_characteristic = MagicMock(
        side_effect=lambda uuid: None if uuid == missing else SimpleNamespace(uuid=uuid)
    )
    return client


def notify_handler(client: MagicMock, uuid: str = TELEMETRY_CHARACTERISTIC_UUID):
    """Handler the connection subscribed for notifications of a characteristic"""
    for args in client.start_notify.await_args_list:
        if args.args[0].uuid == uuid:
            return args.args[1]
    raise AssertionError(f"{uuid} was not subscribed")


def drop_link(establish: AsyncMock, client: MagicMock) -> None:
    """Report a lost link the way bleak does"""
    client.is_connected = False
    establish.await_args.kwargs["disconnected_callback"](client)
