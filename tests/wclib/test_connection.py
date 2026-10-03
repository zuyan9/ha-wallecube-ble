import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from custom_components.wallecube_ble.wclib.connection import (
    ADAPTER_CHARACTERISTIC_UUID,
    CONFIG_CHARACTERISTIC_UUID,
    INFO_CHARACTERISTIC_UUID,
    MAX_ERRORS_BEFORE_RECONNECT,
    TELEMETRY_CHARACTERISTIC_UUID,
    Connection,
    ConnectionState,
)
from custom_components.wallecube_ble.wclib.encryption import (
    SessionCipher,
    derive_session_key,
)
from custom_components.wallecube_ble.wclib.exceptions import (
    PacketParseError,
    SessionKeyError,
)
from tests.fakes import (
    ADDRESS,
    BASE_MAC,
    CIPHER,
    LOCAL_NAME,
    W150_INFO,
    ble_device,
    config_frame,
    drop_link,
    encrypted,
    notify_handler,
    telemetry_frame,
    ups_client,
)


def make_connection(
    base_mac_hint: bytes | None = BASE_MAC,
    config_parse=None,
    data_parse=None,
    ble_dev: BLEDevice | None = None,
):
    return Connection(
        ble_dev=ble_dev or ble_device(),
        data_parse=data_parse or AsyncMock(return_value=True),
        base_mac_hint=base_mac_hint,
        config_parse=config_parse,
    )


@pytest.mark.parametrize("from_name", [True, False])
async def test_connect_establishes_the_session(establish, client, from_name: bool):
    # without the advertised name the factory MAC is derived from the address
    conn = make_connection(base_mac_hint=BASE_MAC if from_name else None)

    await conn.connect()

    assert conn.state is ConnectionState.AUTHENTICATED
    assert conn.session_established
    assert conn.base_mac_from_name is from_name
    # responses carry no length, the payload includes the block's zero padding
    assert conn.info == W150_INFO + bytes(6)
    client.start_notify.assert_awaited_once()
    assert client.start_notify.await_args.args[0].uuid == TELEMETRY_CHARACTERISTIC_UUID


# bleak raises TimeoutError when BlueZ does not confirm the end of the link in time
@pytest.mark.parametrize("disconnect_error", [None, TimeoutError()])
async def test_wrong_key_fails_authentication(
    establish, client, disconnect_error: Exception | None
):
    wrong_key = SessionCipher(derive_session_key(bytes.fromhex("aabbccddeeff")))
    client.read_gatt_char.return_value = encrypted(b"\x51" + W150_INFO, wrong_key)
    client.disconnect.side_effect = disconnect_error
    conn = make_connection()
    disconnects = MagicMock()
    received = MagicMock()
    conn.listeners.on_disconnect.add(disconnects)
    conn.listeners.on_data_received.add(received)

    with pytest.raises(SessionKeyError):
        await conn.connect()

    assert conn.state is ConnectionState.ERROR_AUTH_FAILED
    disconnects.assert_called_once()
    client.disconnect.assert_awaited()
    # the info block of a failed session is not passed on
    received.assert_not_called()


async def test_data_listeners_get_decrypted_payloads_only(establish, client):
    conn = make_connection(config_parse=AsyncMock(return_value=True))
    received = MagicMock()
    sent = MagicMock()
    conn.listeners.on_data_received.add(received)
    conn.listeners.on_data_send.add(sent)
    await conn.connect()

    client.read_gatt_char.return_value = encrypted(b"\x51\x01")
    assert await conn.read_value(ADAPTER_CHARACTERISTIC_UUID) == b"\x01" + bytes(14)
    await conn.send_command(ADAPTER_CHARACTERISTIC_UUID, b"\x02")
    await conn.send_config(0x0C)
    handler = notify_handler(client, CONFIG_CHARACTERISTIC_UUID)
    await handler(None, config_frame(0x0C, b"\x2c\x01"))

    # nothing encrypted and no session token, either would let anyone test guessed MAC
    # addresses
    assert received.call_args_list == [
        call("F0BF", conn.info),
        call("F0B2", b"\x01" + bytes(14)),
        call("F0C1/0C", b"\x2c\x01"),
    ]
    assert sent.call_args_list == [call("F0B2", b"\x02"), call("F0C1/0C", b"")]


@pytest.mark.parametrize(
    ("name", "shown"),
    [(LOCAL_NAME, "Walle-0A1B2C******"), (None, "0A:1B:2C:**:**:**")],
)
async def test_connection_messages_do_not_name_the_factory_mac(
    establish, name: str | None, shown: str
):
    conn = make_connection(ble_dev=BLEDevice(ADDRESS, name, None))

    await conn.connect()

    # bleak-retry-connector puts this name into its log lines and error messages
    assert establish.await_args.args[2] == shown


async def test_send_command_writes_authenticated_frame(establish, client):
    conn = make_connection()
    await conn.connect()

    await conn.send_command("0000f0b9-0000-1000-8000-00805f9b34fb", b"\x01")

    characteristic, frame = client.write_gatt_char.await_args.args
    assert characteristic.uuid == "0000f0b9-0000-1000-8000-00805f9b34fb"
    plaintext = CIPHER.decrypt(frame)
    assert plaintext[:2] == b"\x51\x00"
    assert int.from_bytes(plaintext[2:6], "little") == CIPHER.token
    assert plaintext[10] == 0x01


def notify_result_on_write(client, result: bytes) -> None:
    """Answer the next write with a result notification, like the front panel"""

    async def write(characteristic, frame, response):
        handler = notify_handler(client, characteristic.uuid)
        handler(characteristic, bytearray(result))

    client.write_gatt_char.side_effect = write


async def test_confirmed_command_returns_the_notified_status(establish, client):
    conn = make_connection()
    await conn.connect()
    notify_result_on_write(client, b"\x51\x00\x00\x01")

    status = await conn.send_confirmed_command(
        ADAPTER_CHARACTERISTIC_UUID, b"\x01", timeout=1
    )

    assert status == 1
    # subscribed before the write, so the result cannot be missed
    subscribed = client.start_notify.await_args_list[-1].args[0]
    assert subscribed.uuid == ADAPTER_CHARACTERISTIC_UUID
    assert client.stop_notify.await_args.args[0].uuid == ADAPTER_CHARACTERISTIC_UUID


async def test_confirmed_command_times_out_without_a_result(establish, client):
    conn = make_connection()
    await conn.connect()

    with pytest.raises(TimeoutError):
        await conn.send_confirmed_command(
            ADAPTER_CHARACTERISTIC_UUID, b"\x01", timeout=0.01
        )

    client.stop_notify.assert_awaited_once()


async def test_confirmed_command_rejects_an_unexpected_result(establish, client):
    conn = make_connection()
    await conn.connect()
    notify_result_on_write(client, b"\x00")

    with pytest.raises(PacketParseError):
        await conn.send_confirmed_command(
            ADAPTER_CHARACTERISTIC_UUID, b"\x01", timeout=1
        )


def report_end_of_link(establish, client) -> None:
    """Let bleak report the end of the link while the client disconnects"""

    async def disconnect():
        drop_link(establish, client)

    client.disconnect.side_effect = disconnect


async def test_disconnect_is_not_reported_as_a_lost_link(establish, client):
    conn = make_connection()
    disconnects = MagicMock()
    conn.listeners.on_disconnect.add(disconnects)
    await conn.connect()
    report_end_of_link(establish, client)

    await conn.disconnect()

    assert conn.state is ConnectionState.DISCONNECTED
    client.disconnect.assert_awaited_once()
    disconnects.assert_not_called()


@pytest.mark.parametrize("error", [BleakError("failed"), EOFError(), TimeoutError()])
async def test_disconnect_ends_the_connection_when_bleak_fails(
    establish, client, error: Exception
):
    conn = make_connection()
    await conn.connect()
    client.disconnect.side_effect = error

    await conn.disconnect()

    assert conn.state is ConnectionState.DISCONNECTED


async def test_late_report_of_an_ended_link_is_ignored(establish, client, caplog):
    conn = make_connection()
    disconnects = MagicMock()
    conn.listeners.on_disconnect.add(disconnects)
    await conn.connect()
    # BlueZ did not confirm the end of the link in time
    client.disconnect.side_effect = TimeoutError
    await conn.disconnect()
    establish.return_value = ups_client()
    await conn.connect()

    # and reports it while the next link is up
    drop_link(establish, client)

    assert conn.state is ConnectionState.AUTHENTICATED
    assert conn.is_connected
    disconnects.assert_not_called()
    assert "Disconnected from device" not in caplog.text


async def test_lost_link_is_reported_without_an_error(establish, client):
    conn = make_connection()
    disconnects = MagicMock()
    conn.listeners.on_disconnect.add(disconnects)
    await conn.connect()

    drop_link(establish, client)

    assert conn.state is ConnectionState.DISCONNECTED
    disconnects.assert_called_once_with(None)


async def test_too_many_errors_in_a_row_end_the_connection(establish, client):
    error = ValueError("broken field")
    errors = [error] * MAX_ERRORS_BEFORE_RECONNECT
    # a processed frame starts the count again
    parse = AsyncMock(side_effect=[*errors, True, *errors, error])
    conn = make_connection(data_parse=parse)
    disconnects = MagicMock()
    conn.listeners.on_disconnect.add(disconnects)
    await conn.connect()
    report_end_of_link(establish, client)

    # errors up to the limit, a processed frame, errors up to the limit again
    for _ in range(2 * MAX_ERRORS_BEFORE_RECONNECT + 1):
        await notify_handler(client)(None, bytearray(telemetry_frame()))

    assert conn.state is ConnectionState.AUTHENTICATED
    disconnects.assert_not_called()

    await notify_handler(client)(None, bytearray(telemetry_frame()))

    assert conn.state is ConnectionState.ERROR_TOO_MANY_ERRORS
    client.disconnect.assert_awaited_once()
    # once, with the error: the end of the link bleak reports is not passed on again
    disconnects.assert_called_once_with(error)


async def test_config_notifications_are_decrypted_and_forwarded(establish, client):
    config_parse = AsyncMock(return_value=True)
    conn = make_connection(config_parse=config_parse)
    await conn.connect()

    handler = notify_handler(client, CONFIG_CHARACTERISTIC_UUID)
    await handler(None, config_frame(0x0C, b"\x2c\x01\x00\x00\x46"))

    message = config_parse.await_args.args[0]
    assert message.message_type == 0x0C
    assert message.payload == b"\x2c\x01\x00\x00\x46"


async def test_config_notifications_with_foreign_token_are_ignored(establish, client):
    config_parse = AsyncMock(return_value=True)
    conn = make_connection(config_parse=config_parse)
    await conn.connect()

    handler = notify_handler(client, CONFIG_CHARACTERISTIC_UUID)
    await handler(None, config_frame(0x0C, bytes(5), token=0x11223344))

    config_parse.assert_not_awaited()


async def test_truncated_config_notification_warns_once(establish, client, caplog):
    config_parse = AsyncMock(return_value=True)
    conn = make_connection(config_parse=config_parse)
    await conn.connect()
    handler = notify_handler(client, CONFIG_CHARACTERISTIC_UUID)
    # a Wi-Fi status reply cut to 20 bytes by the default ATT MTU
    frame = config_frame(0x01, bytes(21), token=0)[:20]

    await handler(None, frame)
    await handler(None, frame)

    config_parse.assert_not_awaited()
    warnings = [r for r in caplog.records if "MTU" in r.getMessage()]
    assert len(warnings) == 1


async def test_send_config_writes_config_frame(establish, client):
    conn = make_connection(config_parse=AsyncMock())
    await conn.connect()

    await conn.send_config(0x0B, b"\x58\x02\x00\x00")

    characteristic, frame = client.write_gatt_char.await_args.args
    assert characteristic.uuid == CONFIG_CHARACTERISTIC_UUID
    plaintext = CIPHER.decrypt(frame)
    # type 0x0B tagged with 0x40, payload length, 2-byte nonce, token, payload
    assert plaintext[:2] == b"\x4b\x04"
    assert int.from_bytes(plaintext[4:8], "little") == CIPHER.token
    assert plaintext[8:12] == b"\x58\x02\x00\x00"


async def test_telemetry_is_subscribed_before_config(establish, client):
    conn = make_connection(config_parse=AsyncMock())

    await conn.connect()

    subscribed = [args.args[0].uuid for args in client.start_notify.await_args_list]
    assert subscribed == [TELEMETRY_CHARACTERISTIC_UUID, CONFIG_CHARACTERISTIC_UUID]


async def test_connect_records_the_device_characteristics(establish, client):
    client.services.characteristics = {
        handle: SimpleNamespace(uuid=uuid)
        for handle, uuid in enumerate(
            (TELEMETRY_CHARACTERISTIC_UUID, INFO_CHARACTERISTIC_UUID)
        )
    }
    conn = make_connection()
    assert conn.characteristics is None

    await conn.connect()
    await conn.disconnect()

    # firmware versions differ in their characteristics, the set outlives the link
    assert conn.characteristics == {
        TELEMETRY_CHARACTERISTIC_UUID,
        INFO_CHARACTERISTIC_UUID,
    }


async def test_connects_without_config_characteristic(establish, client):
    client.services.get_characteristic.side_effect = lambda uuid: (
        None if uuid == CONFIG_CHARACTERISTIC_UUID else SimpleNamespace(uuid=uuid)
    )
    conn = make_connection(config_parse=AsyncMock())

    await conn.connect()

    assert conn.state is ConnectionState.AUTHENTICATED
    client.start_notify.assert_awaited_once()


async def test_failed_config_subscription_keeps_telemetry(establish, client):
    async def start_notify(characteristic, handler, **kwargs):
        if characteristic.uuid == CONFIG_CHARACTERISTIC_UUID:
            raise BleakError("NotSupported")

    client.start_notify.side_effect = start_notify
    conn = make_connection(config_parse=AsyncMock())

    await conn.connect()

    assert conn.state is ConnectionState.AUTHENTICATED
    assert notify_handler(client) is not None


@pytest.mark.parametrize(
    "dropped_during", [TELEMETRY_CHARACTERISTIC_UUID, CONFIG_CHARACTERISTIC_UUID]
)
@pytest.mark.parametrize("subscription_fails", [True, False])
async def test_disconnect_while_subscribing_fails_the_connection(
    establish, client, subscription_fails: bool, dropped_during: str
):
    async def start_notify(characteristic, handler, **kwargs):
        if characteristic.uuid == dropped_during:
            # bleak reports the dropped link while the subscription is pending
            drop_link(establish, client)
            if subscription_fails:
                raise BleakError("Not connected")

    client.start_notify.side_effect = start_notify
    conn = make_connection(config_parse=AsyncMock())
    disconnects = MagicMock()
    conn.listeners.on_disconnect.add(disconnects)

    with pytest.raises(BleakError):
        await conn.connect()

    assert conn.state is ConnectionState.ERROR_BLEAK
    # reported once, by the failed connect
    disconnects.assert_called_once()


@pytest.mark.parametrize("read_fails", [True, False])
# a client can report the end of the link before it stops reporting it is connected
@pytest.mark.parametrize("still_connected", [False, True])
async def test_disconnect_while_reading_the_info_fails_the_connection(
    establish, client, read_fails: bool, still_connected: bool
):
    info = client.read_gatt_char.return_value

    async def read_gatt_char(characteristic):
        # bleak reports the dropped link while the read is pending
        drop_link(establish, client)
        client.is_connected = still_connected
        if read_fails:
            raise BleakError("Not connected")
        return info

    client.read_gatt_char.side_effect = read_gatt_char
    conn = make_connection()
    disconnects = MagicMock()
    conn.listeners.on_disconnect.add(disconnects)

    with pytest.raises(BleakError):
        await conn.connect()

    assert conn.state is ConnectionState.ERROR_BLEAK
    disconnects.assert_called_once()
    assert isinstance(disconnects.call_args.args[0], BleakError)
    # nothing of the lost link keeps the next attempt from connecting
    establish.return_value = ups_client()
    await conn.connect()
    assert conn.state is ConnectionState.AUTHENTICATED


async def test_only_the_first_failure_in_a_row_is_a_warning(establish, client, caplog):
    establish.side_effect = [BleakError("1"), BleakError("2"), client, BleakError("3")]
    conn = make_connection()
    caplog.set_level(logging.DEBUG)

    for _ in range(2):
        with pytest.raises(BleakError):
            await conn.connect()
    await conn.connect()
    await conn.disconnect()
    with pytest.raises(BleakError):
        await conn.connect()

    failures = [
        (r.levelno, r.getMessage())
        for r in caplog.records
        if r.getMessage().startswith("Connection failed")
    ]
    assert failures == [
        (logging.WARNING, "Connection failed: 1"),
        (logging.DEBUG, "Connection failed: 2"),
        (logging.WARNING, "Connection failed: 3"),
    ]


async def test_connection_failures_are_logged_without_the_address(establish, caplog):
    establish.side_effect = BleakError(
        "Walle-0A1B2C3D4E50 - 0A:1B:2C:3D:4E:52: Failed to connect: "
        "/org/bluez/hci0/dev_0A_1B_2C_3D_4E_52 not found"
    )

    with pytest.raises(BleakError):
        await make_connection().connect()

    assert caplog.messages == [
        "Connection failed: Walle-0A1B2C****** - 0A:1B:2C:**:**:**: Failed to "
        "connect: /org/bluez/hci0/dev_0A_1B_2C_**_**_** not found",
    ]
    # named like the device by the last four digits of the address
    assert (
        caplog.records[0].name
        == "custom_components.wallecube_ble.wclib.connection.4E52"
    )
