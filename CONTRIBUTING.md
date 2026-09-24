# Contributing to ha-wallecube-ble

Thanks for your interest in improving the integration! This guide explains how to report
problems, set up a development environment and get a change merged.

## Table of contents

- [Ways to contribute](#ways-to-contribute)
- [Development setup](#development-setup)
- [Making a pull request](#making-a-pull-request)
- [Style guide](#style-guide)
- [Project layout](#project-layout)

## Ways to contribute

### Reporting bugs

Use the [bug report](https://github.com/zuyan9/ha-wallecube-ble/issues/new?template=bug_report.yaml)
template. Include the integration and Home Assistant versions, how the device is
connected (local adapter or proxy) and, ideally, a diagnostics download.

### Reporting a missing or incorrect sensor

Use the [sensor template](https://github.com/zuyan9/ha-wallecube-ble/issues/new?template=sensor_request.yaml)
and compare the value with what the device display shows at the same moment. A
diagnostics download with packet collection enabled lets us check the raw telemetry.

### Testing another WalleCube model

Only the W150 is verified. If you own another model, open a
[device request](https://github.com/zuyan9/ha-wallecube-ble/issues/new?template=device_request.yaml)
with its advertised Bluetooth name and what happens during setup.

## Development setup

### Prerequisites

- **Python 3.13 or newer.**
- **[`uv`](https://github.com/astral-sh/uv)** for dependency management and tooling.
- **git**, and ideally a Home Assistant instance plus a device to test end-to-end.

### Clone and install

```bash
git clone https://github.com/zuyan9/ha-wallecube-ble.git
cd ha-wallecube-ble
uv sync --all-groups
```

This creates `.venv/` with the runtime dependencies and the `dev`, `test`, `lint` and
`hass` groups. The `hass` group installs Home Assistant for editor support; the library
tests do not need it.

### Running tests

```bash
uv run pytest tests/wclib
```

The library tests run without Home Assistant. Use `uv run pytest -k <name>` for a single
test. Add tests for protocol changes next to the existing ones in `tests/wclib/`.

### Code style and linting

Style and lint rules are enforced with [`prek`](https://github.com/j178/prek) running the
hooks from [`.pre-commit-config.yaml`](.pre-commit-config.yaml), the same hooks CI runs:

```bash
uvx prek install              # enable the pre-commit git hook (one-time)
uvx prek run --all-files      # check the whole repo (matches CI)
```

The hooks include [`ruff`](https://docs.astral.sh/ruff/) for linting and formatting
(configured in `pyproject.toml`), [`rumdl`](https://github.com/rvben/rumdl) for
Markdown and a few generic hygiene checks.

### Running the integration in Home Assistant

Symlink the component into a Home Assistant config directory and restart Home Assistant
after changing Python code:

```bash
ln -s "$(pwd)/custom_components/wallecube_ble" /path/to/ha-config/custom_components/wallecube_ble
```

## Making a pull request

- Create a feature branch off `main` and keep each PR focused on a single concern.
- Write commit messages in the imperative mood, e.g. "Add battery voltage sensor".
- Describe the user-visible effect, the hardware you tested on and the commands you ran.
- AI-assisted contributions are fine as long as you have read, understood and tested
  every line you submit.

## Style guide

Most rules are enforced by `ruff`. Conventions that are not:

- **Python ≥ 3.13** features are welcome: pattern matching, PEP 695 generics, `type`
  statements, dataclasses.
- **Type hints** on public functions and on fields declared with `raw_field`. The
  transforms in `wclib/props/transforms.py` preserve `None` - prefer them over inline
  lambdas.
- **American English** for identifiers, comments and user-facing strings.
- **Logging:** lazy `%s` formatting, no trailing periods, and no identifying data - the
  device address and the MAC in its advertised name are sensitive.
- **Async-safe** code: no blocking I/O on the event loop and no reuse of `BleakClient`
  instances across connections.
- **Exceptions:** raise the most specific type, keep `try` blocks minimal and reserve
  broad `except Exception` for config flows and background tasks.
- **Docstrings:** NumPy style without types; the summary line has no trailing period,
  and multi-line docstrings start the summary on the second line. Document *why*, not
  what.

## Project layout

```text
custom_components/wallecube_ble/
├── __init__.py            # HA integration entry point
├── config_flow.py         # Discovery and options flows
├── binary_sensor.py       # Status flag entities
├── sensor.py              # Measurement entities
├── translations/          # User-facing strings
└── wclib/                 # Home Assistant-independent device library
    ├── devices/           # One module per device model
    ├── model/             # Fixed-width binary message definitions
    ├── props/             # Field descriptors and transforms
    ├── connection.py      # BLE connection, session and state machine
    └── encryption.py      # Session key derivation and cipher
docs/
└── ble-protocol.md        # Protocol reference
tests/
├── wclib/                 # Library-level tests (no Home Assistant)
└── ha/                    # Integration tests using Home Assistant
```
