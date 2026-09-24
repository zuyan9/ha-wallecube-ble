from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture

from custom_components.wallecube_ble.wclib.connection import (
    CONFIG_CHARACTERISTIC_UUID,
    INFO_CHARACTERISTIC_UUID,
    TELEMETRY_CHARACTERISTIC_UUID,
    Connection,
    ConnectionState,
)
from custom_components.wallecube_ble.wclib.encryption import (
    SessionCipher,
    derive_session_key,
)
from custom_components.wallecube_ble.wclib.exceptions import SessionKeyError

ADDRESS = "88:56:A6:00:C4:BE"
BASE_MAC = bytes.fromhex("8856a600c4bc")


def info_ciphertext(base_mac: bytes = BASE_MAC) -> bytes:
    # magic, unused byte, two words from the power board, then two firmware constants
    plaintext = bytes.fromhex("5100 0301 0200 0300 1300")
    return SessionCipher(derive_session_key(base_mac)).encrypt(plaintext)


@pytest.fixture
def client():
    client = MagicMock()
    client.is_connected = True
    client.read_gatt_char = AsyncMock(return_value=bytearray(info_ciphertext()))
    client.write_gatt_char = AsyncMock()
    client.start_notify = AsyncMock()
    client.disconnect = AsyncMock()
    client.services.get_characteristic = MagicMock(
        side_effect=lambda uuid: SimpleNamespace(uuid=uuid)
    )
    return client


@pytest.fixture
def establish(mocker: MockerFixture, client):
    return mocker.patch(
        "custom_components.wallecube_ble.wclib.connection.establish_connection",
        new=AsyncMock(return_value=client),
    )


def make_connection(
    data_parse=None, base_mac_hint: bytes | None = BASE_MAC, config_parse=None
):
    ble_dev = MagicMock(address=ADDRESS)
    ble_dev.name = "Walle-8856A600C4BC"
    return Connection(
        ble_dev=ble_dev,
        data_parse=data_parse or AsyncMock(return_value=True),
        base_mac_hint=base_mac_hint,
        config_parse=config_parse,
    ).with_disabled_reconnect()


def notify_handler(client, uuid: str):
    for args in client.start_notify.await_args_list:
        if args.args[0].uuid == uuid:
            return args.args[1]
    raise AssertionError(f"{uuid} was not subscribed")


def config_ciphertext(message_type: int, payload: bytes, token: int) -> bytes:
    plaintext = (
        bytes([0x40 | message_type, len(payload)])
        + b"\x12\x34"
        + token.to_bytes(4, "little")
        + payload
    )
    return SessionCipher(derive_session_key(BASE_MAC)).encrypt(plaintext)


async def test_connect_establishes_session_from_advertised_name(establish, client):
    conn = make_connection()

    await conn.connect()

    assert conn.state is ConnectionState.AUTHENTICATED
    assert conn.session_established
    assert conn.base_mac_from_name
    assert conn.base_mac_offset == -2
    # responses carry no length, the payload includes the block's zero padding
    assert conn.info == bytes.fromhex("00 0301 0200 0300 1300") + bytes(6)
    client.start_notify.assert_awaited_once()
    assert client.start_notify.await_args.args[0].uuid == TELEMETRY_CHARACTERISTIC_UUID


async def test_connect_falls_back_to_address_offsets(establish):
    conn = make_connection(base_mac_hint=None)

    await conn.connect()

    assert conn.state is ConnectionState.AUTHENTICATED
    assert not conn.base_mac_from_name
    assert conn.base_mac_offset == -2


async def test_wrong_key_fails_authentication(establish, client):
    client.read_gatt_char.return_value = bytearray(
        info_ciphertext(bytes.fromhex("aabbccddeeff"))
    )
    conn = make_connection()
    disconnects = MagicMock()
    conn.on_disconnect(disconnects)

    await conn.connect()

    assert conn.state is ConnectionState.ERROR_AUTH_FAILED
    disconnects.assert_called_once()
    client.disconnect.assert_awaited()
    with pytest.raises(SessionKeyError):
        await conn.wait_until_authenticated_or_error(raise_on_error=True)


async def test_telemetry_notifications_are_forwarded(establish, client):
    data_parse = AsyncMock(return_value=True)
    received = MagicMock()
    conn = make_connection(data_parse)
    conn.on_data_received(received)
    await conn.connect()

    handler = client.start_notify.await_args.args[1]
    await handler(None, bytearray(b"\x51\x00" + bytes(38)))

    data_parse.assert_awaited_once_with(b"\x51\x00" + bytes(38))
    assert received.call_args.args == (
        b"\x51\x00" + bytes(38),
        ConnectionState.AUTHENTICATED,
    )


async def test_send_command_writes_authenticated_frame(establish, client):
    conn = make_connection()
    await conn.connect()

    await conn.send_command("0000f0b9-0000-1000-8000-00805f9b34fb", b"\x01")

    characteristic, frame = client.write_gatt_char.await_args.args
    assert characteristic.uuid == "0000f0b9-0000-1000-8000-00805f9b34fb"
    session_key = derive_session_key(BASE_MAC)
    plaintext = SessionCipher(session_key).decrypt(frame)
    assert plaintext[:2] == b"\x51\x00"
    assert int.from_bytes(plaintext[2:6], "little") == session_key.token
    assert plaintext[10] == 0x01


async def test_read_value_decrypts_response(establish, client):
    conn = make_connection()
    await conn.connect()
    client.read_gatt_char.return_value = bytearray(
        SessionCipher(derive_session_key(BASE_MAC)).encrypt(b"\x51\x01")
    )

    value = await conn.read_value(INFO_CHARACTERISTIC_UUID)

    assert value[:1] == b"\x01"


async def test_disconnect_sets_disconnected(establish, client):
    conn = make_connection()
    await conn.connect()

    await conn.disconnect()

    assert conn.state is ConnectionState.DISCONNECTED
    client.disconnect.assert_awaited_once()


async def test_config_notifications_are_decrypted_and_forwarded(establish, client):
    config_parse = AsyncMock(return_value=True)
    conn = make_connection(config_parse=config_parse)
    await conn.connect()
    token = derive_session_key(BASE_MAC).token

    handler = notify_handler(client, CONFIG_CHARACTERISTIC_UUID)
    await handler(
        None, bytearray(config_ciphertext(0x0C, b"\x2c\x01\x00\x00\x46", token))
    )

    message = config_parse.await_args.args[0]
    assert message.message_type == 0x0C
    assert message.payload == b"\x2c\x01\x00\x00\x46"


async def test_config_notifications_with_foreign_token_are_ignored(establish, client):
    config_parse = AsyncMock(return_value=True)
    conn = make_connection(config_parse=config_parse)
    await conn.connect()

    handler = notify_handler(client, CONFIG_CHARACTERISTIC_UUID)
    await handler(None, bytearray(config_ciphertext(0x0C, bytes(5), 0x11223344)))

    config_parse.assert_not_awaited()


async def test_send_config_writes_config_frame(establish, client):
    conn = make_connection(config_parse=AsyncMock())
    await conn.connect()

    await conn.send_config(0x0B, b"\x58\x02\x00\x00")

    characteristic, frame = client.write_gatt_char.await_args.args
    assert characteristic.uuid == CONFIG_CHARACTERISTIC_UUID
    session_key = derive_session_key(BASE_MAC)
    plaintext = SessionCipher(session_key).decrypt(frame)
    # type 0x0B tagged with 0x40, payload length, 2-byte nonce, token, payload
    assert plaintext[:2] == b"\x4b\x04"
    assert int.from_bytes(plaintext[4:8], "little") == session_key.token
    assert plaintext[8:12] == b"\x58\x02\x00\x00"
