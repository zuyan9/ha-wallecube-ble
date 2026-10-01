# WalleCube W150 BLE protocol

Local Bluetooth LE protocol of the W150 DC UPS as implemented by this integration. It was
recovered from the firmware of the front panel (ESP32-C3 / ESP8685), which implements
Bluetooth, and of the power board (HC32L07x), which measures and controls the UPS.
Statements are verified against that firmware unless marked otherwise. The device's
separate cloud path (MQTT with JWT authentication) is out of scope.

## Firmware versions

The analysis covers front-panel firmware 1.16 to 1.20 (builds v1.0-25 to v1.0-47) and
power-board firmware 1.21, 1.27 and 1.29 of the W150. All front-panel versions implement
the protocol below identically, except that the temperature unit characteristic `0xF0B9`
only exists from 1.18 on. All power-board versions fill the telemetry block with the same
layout; where their behavior differs, the text says so. The info characteristic reports
both versions, see [Controls](#ups-service).

The W180 runs the same front-panel firmware. Its power board (firmware 1.25, 1.28 and
1.29 analyzed) uses a 4S Li-ion battery instead of 4S LiFePO4 but the same protocol.

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
| `0xF0B7` | Read, Write, Notify | USB wake-up setting of the power board |
| `0xF0B8` | Read, Write | Screen language |
| `0xF0B9` | Read, Write | Screen temperature unit, from front-panel firmware 1.18 |
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

**MTU**: the front panel sends every notification in one piece and does not split it
when the ATT MTU is too small; the Bluetooth stack cuts it off instead. Telemetry
notifications (40 bytes), the Wi-Fi status reply (up to 64 bytes) and the Wi-Fi scan
result (up to about 208 bytes) need a larger MTU than the default 23. BlueZ and ESPHome
Bluetooth proxies exchange the MTU when they connect, and so does the vendor app. On a
link that keeps the default, notifications are cut to 20 bytes: telemetry decodes
partially and encrypted replies longer than one block are lost.

## Telemetry

`0xF0B1` notifications are plaintext **40-byte** frames. The power board measures the
UPS and builds a 38-byte block, which it sends to the front panel about once a second
while it runs and whenever the front panel asks for it. The front panel forwards the
block unchanged, prefixed with the magic byte `0x51` and an event byte. All fields are
little-endian.

| Frame offset | Payload offset | Type | Field | Unit |
| --- | --- | --- | --- | --- |
| 0 | - | u8 | magic `0x51` | - |
| 1 | - | u8 | event, see below | - |
| 2 | 0 | u16 | DC input voltage | mV |
| 4 | 2 | u16 | DC input current, estimated, see below | mA |
| 6 | 4 | u16 | DC output voltage | mV |
| 8 | 6 | u16 | DC output current | mA |
| 10 | 8 | u16 | battery level, see below | 0.1 % |
| 12 | 10 | u16 | battery voltage, see below | mV |
| 14 | 12 | 4 × u16 | cell voltages, from the bottom of the stack up | mV |
| 22 | 20 | s16 | battery current, positive while charging | mA |
| 24 | 22 | s16 | battery temperature | 0.1 °C |
| 26 | 24 | u16 | battery cycle count, see below | - |
| 28 | 26 | u16 | remaining time, see below | s |
| 30 | 28 | u32 | output energy, see below | mWh |
| 34 | 32 | u32 | fault flags, see below | bitfield |
| 38 | 36 | u16 | status flags, see below | bitfield |

The power board measures input and output with its own ADC and the battery with a
battery monitor chip (TI BQ76920): the four cells, the battery current through a 5 mΩ
shunt and a thermistor. The front panel shows voltages, currents and power with one
decimal and computes the output power as output voltage × output current. It treats input
power as lost while the input voltage is more than 2 V below the output voltage.

The input current is not measured. The power board estimates it while input power is
present, as the output current plus the charging power divided by the input voltage and
an efficiency of 93 %, plus 10 mA.

The battery voltage is the sum of the cell voltages corrected by the drop over the
battery's internal resistance (70 mΩ by default), so it is above the measured voltage
while discharging and below it while charging.

**Battery level**: the power board counts the charge with the battery monitor and
corrects the count at rest from the cell voltages. It assumes a capacity of 4200 mAh on
the W150 and 5000 mAh on the W180. From power-board firmware 1.29 on, the level counts
only the charge above the reserve capacity the UPS keeps, 18 % by default (see
[standby](#ups-service)): 0 % means the reserve is reached. Older versions report the
whole charge.

**Battery temperature**: the front panel shows it rounded to whole degrees and leaves it
blank outside −40 to 85 °C.

**Event byte**: the front panel sends `0x02` on the sample where input power is lost,
`0x01` on the sample where it returns and `0x00` otherwise. It sends that sample as an
extra notification right away. The vendor app labels `0x01` "PowerDown" and `0x02`
"PowerOn", the opposite of the firmware logic: with `0x02` the firmware starts the
Wake-on-LAN outage timer, with `0x01` the delay after input power returns. No event is
sent for the first sample after boot.

**Battery cycle count**: equivalent full cycles, the charge moved into and out of the
battery divided by twice the capacity. The power board keeps the count across restarts;
a factory reset sets it to 0. The frame carries no health value: the vendor cloud
estimates it from the cycle count as min(100, (1500 - cycles) / 14) %, and the vendor
app's battery health page shows that estimate. The app's balance rating is computed by
the cloud from the largest difference between the cell voltages.

**Remaining time**: how long the battery would last at the present output load, from
the remaining charge, derated by 5 %, and an average of the output current. It counts
down to an empty battery, including the reserve capacity, although the UPS turns its
output off when it reaches the reserve. The power board reports `0xFFFF` while the
output current is too small for an estimate (below about 64 mA) and `0` when the battery
monitor missed a sample. It computes the value on input power as well. Power-board
firmware 1.21 works from the battery current instead and reports the time until the
battery is full while charging.

The front panel shows payload offset 26 divided by 60 as minutes. It does so only under
all of these conditions:

- status bit 8 is set;
- at least 30 s have passed since input power was lost or returned;
- the value is between 31 s and about 16.7 hours.

The vendor cloud reads its remaining seconds from offset 26 as well. The vendor app's
decoder reads them from offset 24, which holds the cycle count.

**Output energy**: the energy delivered at the output, counted while the UPS runs. The
power board keeps it across restarts, saving it every 2 Wh, so up to 2 Wh are lost when
it restarts. The vendor app divides the raw value by 10⁶ and shows kWh.

**Fault flags**: the power board sets these bits while the condition lasts. Bits 0-7
clear together once the battery is back in range. On an input over-voltage (bit 10) the
power board also stops powering the output from the battery; the output over-current
flag (bit 11) is only reported. The front panel and the cloud ignore the fault flags;
the vendor app's decoder reads them as a 32-bit value without using it.

| Bit | Mask | Condition |
| --- | --- | --- |
| 0 | `0x00000001` | cell under-voltage |
| 1 | `0x00000002` | cell over-voltage |
| 3 | `0x00000008` | discharge over-current or short circuit |
| 4 | `0x00000010` | battery monitor not responding |
| 5 | `0x00000020` | battery monitor alert input |
| 6 | `0x00000040` | battery monitor chip fault |
| 8 | `0x00000100` | battery temperature above the high limit: 60 °C (W150), 50 °C (W180) |
| 9 | `0x00000200` | battery temperature below −10 °C |
| 10 | `0x00000400` | input voltage more than 1.8 V above the adapter voltage setting |
| 11 | `0x00000800` | output current above 155 W ÷ adapter voltage setting, at most 11.5 A |
| 16 | `0x00010000` | a request on the power board's USB interface stalled, until it restarts |

**Status flags**:

| Bit | Mask | Vendor app name | Set while | Front-panel use |
| --- | --- | --- | --- | --- |
| 0 | `0x0001` | - | always | - |
| 2 | `0x0004` | overload | on battery with more than 120 W output, only reported | - |
| 3 | `0x0008` | - | battery temperature above the high limit, see fault bit 8 | - |
| 4 | `0x0010` | shutdown imminent | on battery below 25 % of the whole charge, or below the shutdown reserve if that is higher | - |
| 5 | `0x0020` | - | charge below the reserve capacity | - |
| 7 | `0x0080` | charging | not on battery and the battery current is above 20 mA | charging icon |
| 8 | `0x0100` | discharging | on battery: set when input power is lost, cleared about 30 s after it returns | remaining time shown |
| 10 | `0x0400` | AC OK | input voltage above the power-good threshold, see adapter settings | power icon; buzzer repeat mode beeps while clear |

The other bits are never set. On firmware 1.29 with the default reserve, bit 4 is set
below a battery level of about 8.5 %, and bit 5 together with a battery level of 0 %.

## Power-board link

For context: the power-board MCU talks to the front panel over UART1 at 19200 baud, 8N1.
Frames in both directions are `0xA0, command, length (u16 LE), payload, CRC` with
CRC-16/X.25 (polynomial 0x1021 reflected, init 0xFFFF, final XOR 0xFFFF) over everything
before the CRC. The power board answers a request with the command byte it received. It
keeps the adapter and standby settings in its EEPROM, and the front panel reads them at
boot.

| Command | Payload | Front panel sends it | Meaning |
| --- | --- | --- | --- |
| `0x01` | - | periodically and on a write to `0xF0B5` | telemetry block, which the power board also sends on its own |
| `0x03`, `0x13` | 5 × u16 | at boot, on a write to `0xF0B2` | read and write the adapter settings |
| `0x07`, `0x17` | 5 × u16 | at boot, on a write to `0xF0B4` | read and write the standby settings |
| `0x0A` | - | at boot | versions, see the info block |
| `0x18` | u8 | on a write to `0xF0B7` | USB wake-up setting |
| `0x20` | magic `0x5A1B0671` | on a write to `0xF0B6` | restore the defaults |
| `0x21` | 6 bytes | at boot | the front panel's MAC |
| `0x22` | magic `0x5A1B0672` | on request of the vendor cloud | restart the power board |
| `0x80`-`0x85` | - | during a firmware update from the vendor cloud | update the power board |

Writes of settings are answered with the number of fields the power board rejected, but
the front panel only checks that an answer arrives. The power board also sends button
events (`0x90`-`0x92`), which the front panel forwards to the vendor cloud only, and a
notice when it goes to sleep or shuts down (`0x95`). Its other commands read or reset
calibration, statistics and factory data; the front panel does not send them.

The power board also has a USB interface, which presents the UPS to a connected computer
as a USB HID power device and signals a remote wake-up when input power returns.

## Controls

Payload layouts below follow the headers from [Frame formats](#frame-formats). Adapter
and standby settings are forwarded to the power board; the other settings are stored in
the front panel's flash and take effect immediately. Values outside the listed ranges
are clamped by the device, not rejected, unless noted otherwise. Defaults are the values
after a factory reset.

None of the commands switches the DC output, and the vendor app has no switch for it
either.

### UPS service

| Characteristic | Write payload | Read payload | Default |
| --- | --- | --- | --- |
| `0xF0B2` adapter | 5 × u16, see below | same 5 × u16 | see below |
| `0xF0B3` buzzer | u8: 0 mute, 1 beep when input power is lost or returns, 2 repeat | u8 | 1 |
| `0xF0B4` standby | 2 or 5 × u16, see below | first 2 × u16 | see below |
| `0xF0B5` | empty | - | - |
| `0xF0B6` reset | empty | - | - |
| `0xF0B7` | u8, see below | screen-language byte | - |
| `0xF0B8` language | u8: 0 English, 1 Simplified Chinese | u8 | 0 |
| `0xF0B9` temperature unit | u8: 0 °C, 1 °F | u8 | 0 |

Values other than 0 and 1 written to `0xF0B8` or `0xF0B9` are stored as 0. The buzzer
byte is stored as written. In repeat mode the buzzer beeps every 2.5 s while status flag
bit 10 is clear; the vendor app calls that bit "AC OK".

**Adapter settings (`0xF0B2`)** tell the UPS which power adapter feeds it. The front
panel clamps them to its ranges and forwards them to the power board, which checks them
against its own ranges and keeps the previous value of each field outside them. The
power board saves the settings but applies them only when it starts. That is why the
vendor app asks for a restart with the reset hole on the front panel.

| Payload offset | Field | Unit | Front-panel range | Power-board range | Vendor app value | Default |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | adapter current | mA | 2000-10000 | 1000-10000 | entered current | 8000 |
| 2 | charge current limit | mA | 1000 to adapter current - 1000 | 500 to adapter current - 800 | 70 % of adapter current | 5000 |
| 4 | adapter voltage | mV | 5000-20200 | 5000-20000 | entered voltage | 12000 |
| 6 | stop-charge threshold | mV | voltage - 1500 to voltage - 150 | voltage - 2000 to voltage - 100 | 96.5 % of voltage | 11580 |
| 8 | power-good threshold | mV | voltage - 2500 to voltage - 300 | voltage - 2000 to voltage - 200 | 95.8 % of voltage | 11496 |

What the values control:

- The adapter voltage is also the output voltage the UPS generates on battery. The
  output current limit (155 W divided by it) and the input over-voltage limit (1.8 V
  above it) follow from it as well.
- The stop-charge threshold is the charger's input voltage limit: when the adapter's
  voltage sags to it, the charger reduces the charging current. If it is above the
  voltage the adapter delivers, the battery does not charge; the vendor app recommends
  lowering the entered voltage in that case.
- Below the power-good threshold the input counts as lost and the UPS runs from the
  battery (status flag bit 10). The vendor app calls it the power-off voltage.
- The adapter current and the charge current limit set the charger's current limits;
  how exactly is not confirmed.

The device then notifies `0xF0B2` with the plaintext frame `0x51, 0x00, 0x00, status`:
status 0 means the power board answered within 200 ms, 1 that the front panel did not see
the answer. The front panel only looks at the first message from the power board in that
window, so another message arriving first also gives status 1, and resending the block
is harmless. A field the power board rejected is still confirmed with status 0. The
front panel keeps the written block before forwarding it, so reads return it even when
the power board did not take it, until the front panel restarts.

**Standby (`0xF0B4`)**: the power board keeps five values. The vendor app reads and
writes only the first two.

| Payload offset | Field | Unit | Front-panel range | Power-board range | Default |
| --- | --- | --- | --- | --- | --- |
| 0 | sleep time | s | 20-7200 | 20-7200 | 600 |
| 2 | sleep current threshold | mA | 20-3000 | 20-3000, before firmware 1.29: 20-2000 | 150 |
| 4 | reserve capacity | 0.1 % | 100-900 | 50-600 | 180 |
| 6 | shutdown time | h | 20-7200 | 2-1440 | 72 |
| 8 | shutdown reserve | 0.1 % | 100-800 | 10-600 | 10 |

A write carries either the first two values or all five. The front panel keeps the
other three from the copy it read from the power board at boot and always forwards all
five. A read returns only the first two; the rest of the response is not initialized.
The power board keeps the previous value of each field outside its range and then does
not save the block, so the other changes last only until it restarts.

- While the UPS runs from the battery and its output current stays below the threshold
  for the sleep time, it goes to sleep and turns its output off. It also sleeps on
  battery when the charge falls to the reserve capacity, when the battery voltage drops
  under high load, at a high battery current and when the battery overheats.
- On input power the sleep time does not run. A sleeping UPS wakes up when input power
  returns or the front-panel button is pressed, not when the load rises again. After the
  shutdown time on battery, or below 5 % charge, it shuts down completely and starts
  again only when input power returns.
- From power-board firmware 1.29 on, the battery level counts from the reserve capacity
  up, see [Telemetry](#telemetry). Status flag bit 4 uses the shutdown reserve when it is
  above 25 %.

Standby writes are confirmed like adapter writes, with a notification on `0xF0B4` even
though that characteristic does not declare Notify. Clients such as BlueZ do not deliver
notifications of a characteristic without that property, so this result cannot be
received.

**`0xF0B5` and `0xF0B6`**: the vendor app's "Restore System Settings" action writes
`0xF0B5`. In every analyzed front-panel version, `0xF0B5` only requests a fresh
telemetry sample, so the action has no effect. The factory reset is on `0xF0B6`:

- The power board restores its defaults listed here, which fit a 12 V adapter, and sets
  the cycle count to 0. Power-board firmware 1.29 then restarts.
- The front panel restores its defaults listed here, including the screen settings, and
  clears the Wake-on-LAN list. It keeps the Wi-Fi credentials and does not restart.

With another adapter, the adapter settings have to be entered again after a reset.

**`0xF0B7`**: a write forwards the first payload byte to the power board as command
`0x18`. While the value is not 0, the power board stops re-attaching its USB interface
periodically to wake the connected computer. The value is not saved and returns to 0
when the power board restarts. The result is notified on `0xF0B7` in the same way as for
`0xF0B2`. A read returns the screen-language byte. The vendor app never uses `0xF0B7`,
and power-board firmware 1.21 does not know the command.

**Info block (`0xF0BF`)**: 10 bytes, zero padded to a block:

| Offset | Type | Field |
| --- | --- | --- |
| 0 | u8 | magic `0x51` |
| 1 | u8 | not initialized |
| 2 | u16 | power-board hardware version: 3 on the W150, 4 on the W180 |
| 4 | u16 | power-board firmware version, e.g. 29 |
| 6 | u16 | front-panel hardware version, 3 in all analyzed versions |
| 8 | u16 | front-panel firmware version, 16-20 in the analyzed versions |

The front panel asks the power board for its two versions over UART (command `0x0A`) at
boot and reports 0 for both if it gets no answer. It sends the same four values to the
vendor cloud, which lists them as UPS and system hardware and software versions. The
vendor app shows the cloud's version strings, e.g. `1.19` for front-panel firmware 19, so
they do not match the raw numbers.

### Configuration service

| Type | Payload | Meaning |
| --- | --- | --- |
| `0x01` | - | request Wi-Fi status, answered as type `0x01` |
| `0x02` | - | start a Wi-Fi scan, answered as type `0x03` |
| `0x04` | 32-byte SSID, 64-byte password, both zero padded | set Wi-Fi credentials |
| `0x05` | 6-byte MAC | add Wake-on-LAN target (at most 8) |
| `0x06` | 6-byte MAC | remove Wake-on-LAN target |
| `0x07` | - | clear Wake-on-LAN targets |
| `0x08` | - | request Wake-on-LAN targets, answered as type `0x08` with u8 count and the MACs |
| `0x09` | 3 × u16 | set Wake-on-LAN trigger, see below |
| `0x0A` | - | request Wake-on-LAN trigger, answered as type `0x0A` |
| `0x0B` | u32 timeout (s, at least 30; `0xFFFFFFFF` keeps the screen on), optional u8 idle backlight (%, up to 100) | set screen timeout |
| `0x0C` | - | request screen timeout, answered as type `0x0C` with u32 timeout and u8 idle backlight |
| `0x0D` | u8 backlight (%, 20-100) | set active backlight |
| `0x0E` | - | request active backlight, answered as type `0x0E` with u8 backlight |

Set requests are not answered. After new Wi-Fi credentials the front panel reconnects
without restarting. Type `0x10` is accepted but has no effect. Type `0x03` is only used
for scan results: a write of it makes the front panel read through a null pointer.

The **Wi-Fi scan** result is one notification that packs one record per network, s8
signal strength (dBm), u8 SSID length and the SSID, until the payload exceeds 200 bytes.

After the screen timeout expires without interaction, the screen switches to its idle
view at the idle backlight level. Defaults: timeout 300 s, both backlight levels 70 %.
The idle backlight has no lower limit, so 0 turns the screen dark. It can only be
written together with the timeout.

The **Wi-Fi status** request (`0x01`) is answered as type `0x01`:

| Payload offset | Type | Field |
| --- | --- | --- |
| 0 | u8 | 1 if connected, else 0 |
| 1 | s8 | signal strength (dBm) |
| 2 | 4 bytes | IP address |
| 6 | 4 bytes | gateway |
| 10 | 4 bytes | netmask |
| 14 | u8 | SSID length n |
| 15 | n bytes | SSID |

Addresses are in dotted order. While disconnected, offsets 0-13 are zero and the SSID
length is not initialized. With a 32-byte SSID the encrypted reply is 64 bytes, so it
needs a larger MTU than the default, like the telemetry notifications.

The **Wake-on-LAN trigger** makes the UPS send magic packets to the stored targets over
its network connection after an outage. Its payload is three u16 values:

- outage time before the outage counts (s, at least 10);
- delay after input power returns (s, at least 10);
- minimum battery level for sending (%, 20-80).

Defaults: 30 s, 30 s, 35 %.

The integration exposes the settings of the vendor app's advanced configuration page:
adapter, standby, screen timeout, temperature unit, screen language and buzzer. It also
exposes both screen backlight levels, the versions from the info block, the event byte
and the Wi-Fi status, which it requests every minute. It waits for the result of adapter
writes and sends an unconfirmed block once more; standby writes stay unconfirmed. The
factory reset, `0xF0B7`, the last three standby values, Wake-on-LAN and Wi-Fi setup are
not exposed.
