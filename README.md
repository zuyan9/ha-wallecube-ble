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

| *Sensors*                          | *Binary Sensors*  | *Controls*                     |
|------------------------------------|-------------------|--------------------------------|
| Battery Level                      | Input Power       | Buzzer                         |
| Battery Voltage                    | Charging          | Screen Language                |
| Battery Current                    | Discharging       | Temperature Unit               |
| DC Input Voltage                   | Overload          | Screen Timeout                 |
| DC Input Current                   | Shutdown Imminent | Keep Screen On                 |
| DC Output Voltage                  |                   | Sleep Time                     |
| DC Output Current                  |                   | Sleep Min Current              |
| Output Power                       |                   | Adapter Voltage *(disabled)*   |
| Temperature                        |                   | Adapter Current *(disabled)*   |
| Discharge Time Remaining           |                   |                                |
| Total Energy Consumed *(disabled)* |                   |                                |

> **📝 Note:** Discharge Time Remaining is only reported while the output runs on battery.
> The sign convention of Battery Current (charging vs. discharging) and the unit of Total
> Energy Consumed are not confirmed yet.

The controls mirror the vendor app's advanced configuration page and use its wording in
English, 简体中文 and Русский.

> **⚠️ Warning:** Adapter Voltage and Adapter Current must match the label of the power
> adapter feeding the UPS; wrong values can stop the battery from charging. As in the
> app, the other adapter limits are derived from them, and the UPS applies the change only
> after you gently press the reset hole on the front panel. Both entities are disabled
> by default.

</details>

<br>

> [!NOTE]
> Other WalleCube models that advertise as `Walle-…` (e.g. the W120, which uses the same
> front-panel board) may work but are untested. Wake-on-LAN targets, Wi-Fi setup and the
> system reset of the vendor app are not implemented yet. Please
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
