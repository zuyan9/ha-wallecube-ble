"""Repair issues of WalleCube BLE"""

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .wclib import DeviceBase


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Close the issue once the user confirms the restart"""
    # the UPS reports nothing that would show that it restarted
    return ConfirmRepairFlow()


@callback
def async_create_restart_issue(
    hass: HomeAssistant, entry_id: str, device: DeviceBase
) -> None:
    """
    Ask the user to restart the device so that it applies the changed settings

    Only the adapter settings of the UPS need a restart, so the issue names them, with
    the values the device reports after the change. The diagnostics include the issue,
    so it leaves out the device name, which contains part of the address.
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        _restart_issue_id(entry_id),
        is_fixable=True,
        # Home Assistant cannot tell after its own restart whether the UPS restarted
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="restart_required",
        translation_placeholders={
            "model": device.device,
            "voltage": _number(getattr(device, "adapter_voltage", None)),
            "current": _number(getattr(device, "adapter_current", None)),
            "power_off": _number(getattr(device, "power_good_voltage", None), 1),
        },
    )


@callback
def async_delete_restart_issue(hass: HomeAssistant, entry_id: str) -> None:
    ir.async_delete_issue(hass, DOMAIN, _restart_issue_id(entry_id))


def _restart_issue_id(entry_id: str) -> str:
    return f"restart_required_{entry_id}"


def _number(value: float | None, decimals: int | None = None) -> str:
    if value is None:
        return "?"
    return f"{value:g}" if decimals is None else f"{value:.{decimals}f}"
