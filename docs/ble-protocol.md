# WalleCube W150 BLE protocol

Local Bluetooth LE protocol of the W150 DC UPS as implemented by this integration. It was
recovered from the front-panel firmware (ESP32-C3 / ESP8685); statements are verified
against that firmware unless marked otherwise. The device's separate cloud path (MQTT with
JWT authentication) is out of scope.

## Advertising

The device advertises as connectable and general-discoverable with:

- flags `0x06`
- complete local name `Walle-<MAC>`, where `<MAC>` is the upper-case hex of the device's
  factory (base) MAC address, e.g. `Walle-8856A6xxxxxx`
- complete list of 16-bit service UUIDs: `0xF0A1`

The advertised Bluetooth address is derived from the factory MAC by ESP-IDF and is
usually a small offset away from it.

## GATT services

Standard **Device Information** service `0x180A` (plaintext):

| Characteristic | Value |
| --- | --- |
| `0x2A24` Model Number | `Walle_DCUPS` |
| `0x2A29` Manufacturer Name | `ESP32` |
| `0x2A23` System ID | `Walle` |

Custom **UPS** service `0xF0A1`:

| Characteristic | Properties | Content |
| --- | --- | --- |
| `0xF0B1` | Notify | Live telemetry, plaintext (see below) |
| `0xF0B2` | Read, Write, Notify | Configuration block A (10 bytes) |
| `0xF0B3` | Read, Write | Read mirror of configuration block B |
| `0xF0B4` | Read, Write | Configuration block B |
| `0xF0B5` | Write | Command trigger |
| `0xF0B6` | Write | Command trigger |
| `0xF0B7` | Read, Write, Notify | Single-byte command |
| `0xF0B8` | Read, Write | Mode byte |
| `0xF0B9` | Read, Write | Flag (0/1) |
| `0xF0BF` | Read | Info block (10 bytes) |

Custom **Wi-Fi provisioning** service `0xF0A2` with characteristic `0xF0C1` (Write,
Notify).

All reads and writes of the UPS and Wi-Fi characteristics are encrypted. Telemetry
notifications on `0xF0B1` are not.

## Session cipher

Frames are encrypted with **AES-128-CBC**, a **zero IV** and zero padding to a multiple
of 16 bytes. The key is derived at boot from the factory MAC and a 123-byte constant
compiled into the firmware (`wclib/keydata.py`); there is no runtime key exchange:

```text
mac_hex = upper-case hex of the 6-byte factory MAC (the advertised name suffix)
digest  = MD5(mac_hex_ascii + SESSION_SECRET)     # 16 bytes
key     = digest                                  # AES-128 key
token   = int.from_bytes(digest[8:12], "big")     # 32-bit session token
```

The firmware forms the token from digest bytes 8-11 as a big-endian value, stores it as a
native 32-bit word and compares it against the token field of each command frame, which
it also reads as a native word. The core is little-endian, so the frame carries the token
little-endian: `struct.pack("<I", token)`, i.e. bytes `digest[11], digest[10],
digest[9], digest[8]`.

If the advertised name is not available, the factory MAC can be recovered by trying
small offsets from the Bluetooth address and keeping the key that decrypts the info
characteristic `0xF0BF`. Its 10-byte plaintext is zero-padded to a full block, so the
padding confirms a match in addition to the magic byte.

## Frame formats

Plaintext before encryption and padding:

- **Command (client to device, write):** `0x51`, flags (`0x00`), token (4 bytes,
  little-endian), payload. The device ignores the frame unless the flags byte is zero and
  the token matches.
- **Response (device to client, read):** `0x51`, payload. Responses carry no token and no
  length; the decrypted block includes the zero padding.

## Telemetry

`0xF0B1` notifications are plaintext **40-byte** frames. The front panel receives the
measurements from the power-board MCU and forwards the 38-byte payload unchanged,
prefixed with its 2-byte internal event word:

| Frame offset | Payload offset | Type | Field | Unit |
| --- | --- | --- | --- | --- |
| 0 | - | u16 | event word | - |
| 2 | 0 | u16 | DC input voltage | mV |
| 6 | 4 | u16 | DC output voltage | mV |
| 8 | 6 | u16 | DC output current | mA |
| 10 | 8 | u16 | battery level | 0.1 % |
| 22 | 20 | s16 | battery current | mA |
| 24 | 22 | s16 | temperature | 0.1 °C |
| 28 | 26 | u16 | remaining time | s |
| 38 | 36 | u16 | status flags | bitfield |

All fields are little-endian; the remaining payload bytes are not used by the front
panel. Units follow the firmware's display code, which renders voltage, current and power
with one decimal from these fields and output power as voltage × current. The input
voltage is identified by the firmware comparing it against the output voltage to detect
loss of input power. The remaining time is only shown while status flag bit 8 is set,
i.e. while the output runs on battery. The sign convention of the battery current is not
confirmed.

## Power-board link

For context: the power-board MCU talks to the front panel over UART1 at 19200 baud, 8N1.
Frames from the power board are `0xA0, command, length (u16 LE), payload, CRC` with
CRC-16/X.25 (polynomial 0x1021 reflected, init 0xFFFF, final XOR 0xFFFF). Frames from the
front panel to the power board use a `0x55 0xAA` header with CRC-16/MODBUS instead.

## Controls

Command semantics of the writable characteristics (output switching, thresholds, buzzer,
Wi-Fi provisioning) are not mapped yet and are not exposed by the integration.
