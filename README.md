<div align="center">

# 🔋 WalleCube BLE

**Unofficial Bluetooth LE Integration for Home Assistant**

[![hassfest](https://img.shields.io/github/actions/workflow/status/zuyan9/ha-wallecube-ble/validate-hassfest.yaml?style=for-the-badge&label=hassfest)](https://github.com/zuyan9/ha-wallecube-ble/actions/workflows/validate-hassfest.yaml)
[![HACS Validation](https://img.shields.io/github/actions/workflow/status/zuyan9/ha-wallecube-ble/validate-hacs.yaml?style=for-the-badge&label=HACS)](https://github.com/zuyan9/ha-wallecube-ble/actions/workflows/validate-hacs.yaml)

---

**Monitor and Configure your WalleCube UPS locally via Bluetooth**

No cloud account • No internet connection required • Real-time status updates

[Supported Devices](#supported-devices) • [Installation](#installation) •
[Development](#development)

</div>

---

## Overview

This integration connects Home Assistant directly to WalleCube DC UPS units over
**Bluetooth LE**, allowing you to:

- **Monitor** battery level, output voltage, current and power, and remaining runtime
- **Automate** on power outages, e.g. shut down a NAS when the UPS runs on battery
- **Operate** independently of the vendor app and cloud

The encrypted session is established locally from data the device advertises, so no
pairing with the vendor app or account is needed.

---

## Supported Devices

<details>
<summary><b>W150</b></summary>

<br>

| *Sensors*                         | *Binary Sensors*    | *Events*    | *Controls*                   |
|-----------------------------------|---------------------|-------------|------------------------------|
| Battery Level                     | Input Power         | Power Event | Buzzer                       |
| Battery Voltage                   | Charging            |             | Screen Language              |
| Battery Current                   | Discharging         |             | Temperature Unit             |
| Cell 1–4 Voltage                  | Overload            |             | Screen Timeout               |
| Max Voltage Difference            | Shutdown Imminent   |             | Keep Screen On               |
| Battery Health                    | Battery Fault       |             | Screen Brightness            |
| Number of Cycles                  | Battery Overheating |             | Screen Idle Brightness       |
| DC Input Voltage                  | Battery Too Cold    |             | Sleep Time                   |
| DC Input Current                  | Input Overvoltage   |             | Sleep Min Current            |
| DC Output Voltage                 | Output Overcurrent  |             | Adapter Voltage *(disabled)* |
| DC Output Current                 | Wi-Fi               |             | Adapter Current *(disabled)* |
| Output Power                      |                     |             |                              |
| Battery Temperature               |                     |             |                              |
| Discharge Time Remaining          |                     |             |                              |
| Total Energy Consumed             |                     |             |                              |
| UPS Firmware Version              |                     |             |                              |
| UPS Hardware Version *(disabled)* |                     |             |                              |
| Wi-Fi Signal                      |                     |             |                              |
| Wi-Fi Network                     |                     |             |                              |
| IP Address                        |                     |             |                              |

> **📝 Note:** Discharge Time Remaining is only reported while the output runs on battery.
> Like the UPS display, it counts down to an empty battery, but the UPS turns its output
> off earlier, when the battery reaches the reserve it keeps. For a shutdown automation,
> use Shutdown Imminent or Battery Level instead.

Battery Current is positive while the battery charges. The UPS estimates DC Input
Current from the output current and the charging power instead of measuring it. On
battery, the UPS turns its output off when the charge falls to its reserve, 18 % by
default. With power-board firmware 1.29, Battery Level counts only the charge above the
reserve: it reaches 0 % at that point, and Shutdown Imminent turns on at about 8.5 %.
With older firmware, the output turns off at about 18 % and Shutdown Imminent turns on
at 25 %.

Cell 1–4 Voltage, Max Voltage Difference, Battery Health and Number of Cycles are the
values of the vendor app's battery health page. The UPS counts full charge cycles but
does not report a health value: like the vendor cloud, the integration estimates it
from the number of cycles, from 100 % at up to 100 cycles down to 0 % at 1500. Power
Event fires *Power Lost* or *Power Restored* when input power is lost or returns.
Battery Fault, Battery Overheating, Battery Too Cold, Input Overvoltage and Output
Overcurrent show the protection conditions the UPS reports. Input Overvoltage means that
the adapter delivers more than 1.8 V above the configured Adapter Voltage.
The Wi-Fi entities show the UPS's own network connection, which the vendor cloud and
Wake-on-LAN use; they are updated every minute. The device page shows the front panel's
firmware and hardware versions and the UPS version entities those of the power board,
as the numbers the UPS reports. The vendor app shows them in its own format, e.g.
firmware 19 as V1.19.

The controls mirror the vendor app's advanced configuration page and use its wording in
English, 简体中文 and Русский. Screen Brightness and Screen Idle Brightness are not in the
app: the idle level applies once the screen timeout expires, and 0 turns the screen dark.
Sleep Time and Sleep Min Current turn the output off when the UPS runs on battery with a
smaller load than the minimum current for longer than the sleep time. The output comes
back when input power returns or the front-panel button is pressed, not when the load
rises again. Temperature Unit needs front-panel firmware 1.18 or newer and is left out on
older firmware.

> **⚠️ Warning:** Adapter Voltage and Adapter Current must match the label of the power
> adapter feeding the UPS; wrong values can stop the battery from charging. The UPS also
> generates Adapter Voltage at its output while on battery. As in the app, the other
> adapter limits are derived from them, and the UPS applies the change only after you
> gently press the reset hole on the front panel. A change fails with an error if the UPS
> does not confirm that its power board received it. Both entities are disabled by
> default.

</details>

<br>

> [!NOTE]
> The W180 runs the same front-panel firmware and telemetry format as the W150 and should
> work, but it is untested; the device page shows the model its power board reports.
> According to the vendor apps, the W120 sends an older telemetry format, which the
> integration does not decode. Wake-on-LAN targets, Wi-Fi setup and the factory reset are
> not implemented. Please
> [open an issue](https://github.com/zuyan9/ha-wallecube-ble/issues/new/choose) if you can
> help test another model.

---

## Installation

### Prerequisites

- Home Assistant 2025.2 or newer with Bluetooth support (a local adapter or an
  [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html) in range).
  The WalleCube icon and logo show in the UI from Home Assistant 2026.3
- [HACS](https://hacs.xyz/) installed (recommended method)

### Method 1: HACS Installation (Recommended)

**Quick Install:** Click the badge below to open this repository directly in HACS:

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=zuyan9&repository=ha-wallecube-ble&category=integration)

**Manual steps:**

1. Open **HACS** in your Home Assistant instance
2. Click the **⋮** menu (three dots) in the top right
3. Select **Custom repositories**
4. Add this repository URL: `https://github.com/zuyan9/ha-wallecube-ble`
5. Select category: **Integration** and click **Add**
6. Search for **"WalleCube BLE"** and click **Download**
7. Restart Home Assistant

### Method 2: Manual Installation

1. Download the latest release from
   [GitHub Releases](https://github.com/zuyan9/ha-wallecube-ble/releases)
2. Copy the `custom_components/wallecube_ble` folder into your Home Assistant
   `config/custom_components/` directory
3. Restart Home Assistant

### Configuration

After installation, the integration automatically discovers WalleCube devices in range
via Bluetooth LE. Confirm the discovered device under **Settings → Devices & Services**,
or add it manually with **Add Integration → WalleCube BLE**.

---

## Development

### BLE Protocol

The Bluetooth protocol - advertising, GATT layout, session key derivation, frame formats
and the telemetry layout - is documented in [docs/ble-protocol.md](docs/ble-protocol.md).

### Contributing

Contributions are welcome! Please read **[CONTRIBUTING.md](CONTRIBUTING.md)**.

To report wrong or missing values, enable packet collection in the integration options,
reproduce the situation and attach the diagnostics download to your issue. Device
addresses are masked in it.

---

## Support & Warranty

> [!CAUTION]
> **No Warranty • Use at Your Own Risk**

> [!WARNING]
> **Firmware Updates May Break This Integration**
>
> The integration relies on Bluetooth protocol. Future firmware updates may change it.

---

### Purpose & Motivation

A UPS matters most when the network is down - which is exactly when a cloud-connected app
cannot reach it. This integration keeps monitoring local and independent of vendor
servers.
