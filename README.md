<div align="center">

# 🔋 WalleCube BLE

**Unofficial Bluetooth LE Integration for Home Assistant**

[![hassfest](https://img.shields.io/github/actions/workflow/status/zuyan9/ha-wallecube-ble/validate-hassfest.yaml?style=for-the-badge&label=hassfest)](https://github.com/zuyan9/ha-wallecube-ble/actions/workflows/validate-hassfest.yaml)
[![HACS Validation](https://img.shields.io/github/actions/workflow/status/zuyan9/ha-wallecube-ble/validate-hacs.yaml?style=for-the-badge&label=HACS)](https://github.com/zuyan9/ha-wallecube-ble/actions/workflows/validate-hacs.yaml)

---

**Monitor your WalleCube DC UPS locally via Bluetooth**

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

| *Sensors*                           |
|-------------------------------------|
| Battery Level                       |
| Battery Current                     |
| DC Input Voltage                    |
| DC Output Voltage                   |
| DC Output Current                   |
| Output Power                        |
| Temperature                         |
| Discharge Time Remaining            |

> **📝 Note:** Discharge Time Remaining is only reported while the output runs on battery.
> The sign convention of Battery Current (charging vs. discharging) is not confirmed yet.

</details>

<br>

> [!NOTE]
> Other WalleCube models that advertise as `Walle-…` (e.g. the W120, which uses the same
> front-panel board) may work but are untested. Controls (output switching, thresholds,
> buzzer) are not implemented yet. Please
> [open an issue](https://github.com/zuyan9/ha-wallecube-ble/issues/new/choose) if you can
> help test another model.

---

## Support & Warranty

> [!CAUTION]
> **No Warranty • Use at Your Own Risk**
>
> - This is an **unofficial integration**, not affiliated with WalleCube in any way
> - It is provided without warranty of any kind, and the author takes no responsibility
>   for device behavior or damage
> - Do not rely on it as the only safeguard for critical equipment

> [!WARNING]
> **Firmware Updates May Break This Integration**
>
> The integration relies on a reverse-engineered Bluetooth protocol. Future firmware
> updates may change it.

---

## Installation

### Prerequisites

- Home Assistant 2025.2 or newer with Bluetooth support (a local adapter or an
  [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html) in range)
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

### Reverse Engineering

The Bluetooth protocol - advertising, GATT layout, session key derivation, frame formats
and the telemetry layout - is documented in [docs/ble-protocol.md](docs/ble-protocol.md).

### Contributing

Contributions are welcome! Please read **[CONTRIBUTING.md](CONTRIBUTING.md)** first - it
covers development setup, running tests, code style, and the PR workflow.

To report wrong or missing values, enable packet collection in the integration options,
reproduce the situation and attach the diagnostics download to your issue. Device
addresses are masked in it.

---

## Legal

> **This repository is not for sale.**

### Purpose & Motivation

A UPS matters most when the network is down - which is exactly when a cloud-connected app
cannot reach it. This integration keeps monitoring local and independent of vendor
servers.

### Declaration

This work is provided to improve local control and resilience of equipment you own. The
protocol was studied for interoperability only, and there is **no intention to harm** any
individual or entity.
