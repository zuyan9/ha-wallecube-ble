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
| `0xF0B2` | Read, Write, Notify | Power-adapter settings; notifies the write result |
| `0xF0B3` | Read, Write | Buzzer mode |
| `0xF0B4` | Read, Write | Standby settings |
| `0xF0B5` | Write | Telemetry refresh (see [Controls](#controls)) |
| `0xF0B6` | Write | Factory reset |
| `0xF0B7` | Read, Write, Notify | Raw power-board command, not mapped |
| `0xF0B8` | Read, Write | Screen language |
| `0xF0B9` | Read, Write | Screen temperature unit |
| `0xF0BF` | Read | Info block (10 bytes) |

Custom **configuration** service `0xF0A2` with characteristic `0xF0C1` (Write, Notify):
Wi-Fi provisioning, screen settings and Wake-on-LAN targets.

All reads and writes of the UPS and configuration characteristics are encrypted.
Telemetry notifications on `0xF0B1` and the write acknowledgements described in
[Controls](#controls) are not.

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

Plaintext before encryption and padding. All multi-byte fields are little-endian.

UPS service (`0xF0B2`-`0xF0BF`):

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 1 | magic `0x51` |
| 1 | 1 | flags, must be `0x00` |
| 2 | 4 | session token |
| 6 | 4 | nonce, ignored by the device (the vendor app sends random bytes) |
| 10 | n | payload |

The device silently ignores a write whose magic, flags or token do not match. Writes are
limited to 128 bytes. Reads return `0x51` followed by the payload, without token or
length; the decrypted block includes the zero padding.

Configuration service (`0xF0C1`), in both directions:

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 1 | `0x40` \| message type |
| 1 | 1 | payload length |
| 2 | 2 | nonce, ignored by the device |
| 4 | 4 | session token |
| 8 | n | payload |

Writes must be 16-128 bytes after encryption. Requests that read a value are answered
with a notification on `0xF0C1` in the same format, including the token.

## Telemetry

`0xF0B1` notifications are plaintext **40-byte** frames. The front panel receives the
measurements from the power-board MCU and forwards the 38-byte payload unchanged,
prefixed with the magic byte `0x51` and an event byte: `0x01` on the sample where input
power returns, `0x02` on the sample where it is lost and `0x00` otherwise. The front
panel treats input power as lost while the input voltage is more than about 2 V below
the output voltage.

| Frame offset | Payload offset | Type | Field | Unit |
| --- | --- | --- | --- | --- |
| 0 | - | u8 | magic `0x51` | - |
| 1 | - | u8 | event | - |
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
Frames in both directions are `0xA0, command, length (u16 LE), payload, CRC` with
CRC-16/X.25 (polynomial 0x1021 reflected, init 0xFFFF, final XOR 0xFFFF) over everything
before the CRC. The power board replies with the command byte it received. The front
panel polls telemetry with command `0x01`. At boot it reads the adapter settings
(`0x03`) and standby settings (`0x07`) from the power board, so the power board holds the
authoritative copy of both.

## Controls

Payload layouts below follow the headers from [Frame formats](#frame-formats). Adapter
and standby settings are forwarded to the power board; the other settings are stored in
the front panel's flash and take effect immediately. Values outside the listed ranges
are clamped by the device, not rejected. Defaults are the values after a factory reset.

The vendor app exposes no switch for the DC output, and none of the mapped commands
below switches it. The only unmapped command is `0xF0B7`.

### UPS service

| Characteristic | Write payload | Read payload | Default |
| --- | --- | --- | --- |
| `0xF0B2` adapter | 5 × u16, see below | same 5 × u16 | from power board |
| `0xF0B3` buzzer | u8: 0 mute, 1 beep when input power is lost or returns, 2 repeat | u8 | 1 |
| `0xF0B4` standby | u16 time (s, 20-7200), u16 current threshold (mA, 20-3000) | same 2 × u16 | from power board |
| `0xF0B5` | empty | - | - |
| `0xF0B6` reset | empty | - | - |
| `0xF0B7` | u8, forwarded to the power board | screen-language byte | - |
| `0xF0B8` language | u8: 0 English, 1 Simplified Chinese | u8 | 0 |
| `0xF0B9` temperature unit | u8: 0 °C, 1 °F | u8 | 0 |

Values other than 0 and 1 written to `0xF0B8` or `0xF0B9` are stored as 0. In repeat
mode the buzzer beeps every 2.5 s while status flag bit 10 is clear; the vendor app calls
that bit "AC OK".

**Adapter settings (`0xF0B2`)** tell the UPS how much the upstream power adapter can
deliver. The front panel validates them and forwards them to the power board:

| Payload offset | Field | Unit | Device range | Vendor app value |
| --- | --- | --- | --- | --- |
| 0 | adapter current | mA | 2000-10000 | entered current |
| 2 | charge current limit | mA | 1000 to adapter current - 1000 | 70 % of adapter current |
| 4 | adapter voltage | mV | 5000-20200 | entered voltage |
| 6 | stop-charge threshold | mV | voltage - 1500 to voltage - 150 | 96.5 % of voltage |
| 8 | power-good threshold | mV | voltage - 2500 to voltage - 300 | 95.8 % of voltage |

The device then notifies `0xF0B2` with the plaintext frame `0x51, 0x00, 0x00, status`:
status 0 means the power board confirmed, 1 means it did not answer in time. According
to the vendor app, the new settings take effect only after the UPS is restarted with the
reset hole on the front panel. Wrong values can stop the battery from charging.

**Standby (`0xF0B4`)**: according to the vendor app, the UPS goes into standby when the
output current stays below the threshold for the configured time. The power board keeps
three further u16 values in the same block, which the device accepts as an optional
6-byte extension (ranges 100-900, 20-7200 and 100-800). Their meaning is not known and
the vendor app does not send them. Standby writes are confirmed like adapter writes, with
a notification on `0xF0B4` even though that characteristic does not declare Notify.

**`0xF0B5` and `0xF0B6`**: the vendor app's "Restore System Settings" action writes
`0xF0B5`. In the analyzed firmware (v1.0-37), `0xF0B5` only requests a fresh telemetry
sample from the power board. The factory reset is on `0xF0B6`: it sends a reset command
to the power board and restores the front-panel defaults listed here, including the
screen settings, and clears the Wake-on-LAN list.

**`0xF0B7` (unmapped)**: a write forwards the first payload byte to the power board as
command `0x18`. The result is notified on `0xF0B7` in the same way as for `0xF0B2`. A
read returns the screen-language byte. The vendor app never uses `0xF0B7`, and the
power-board firmware is not available, so the effect is unknown. Do not write it except
in a deliberate hardware test.

**Info block (`0xF0BF`)**: `0x51`, the power-board firmware version (4 bytes), then two
u16 values that are constant in the firmware (3 and 19).

### Configuration service

| Type | Payload | Meaning |
| --- | --- | --- |
| `0x01` | - | request Wi-Fi status |
| `0x02` | - | request Wi-Fi scan |
| `0x04` | 96 bytes | set Wi-Fi credentials |
| `0x05` | 6-byte MAC | add Wake-on-LAN target (at most 8) |
| `0x06` | 6-byte MAC | remove Wake-on-LAN target |
| `0x07` | - | clear Wake-on-LAN targets |
| `0x08` | - | request Wake-on-LAN targets |
| `0x09` | 3 × u16 | set Wake-on-LAN trigger, see below |
| `0x0A` | - | request Wake-on-LAN trigger, answered as type `0x0A` |
| `0x0B` | u32 timeout (s, at least 30; `0xFFFFFFFF` keeps the screen on), optional u8 idle backlight (%, up to 100) | set screen timeout |
| `0x0C` | - | request screen timeout, answered as type `0x0C` with u32 timeout and u8 idle backlight |
| `0x0D` | u8 backlight (%, 20-100) | set active backlight |
| `0x0E` | - | request active backlight, answered as type `0x0E` with u8 backlight |

After the screen timeout expires without interaction, the screen switches to its idle
view at the idle backlight level. Defaults: timeout 300 s, both backlight levels 70 %.

The **Wake-on-LAN trigger** makes the UPS send magic packets to the stored targets over
its network connection after an outage: u16 outage time (s, at least 10) before the outage counts, u16
delay (s, at least 10) after input power returns, u16 minimum battery level (%, 20-80)
for sending. Defaults: 30 s, 30 s, 35 %.

Types `0x03` and `0x10` exist in the firmware but are not used by the vendor app.

The integration does not expose any of these controls yet.
