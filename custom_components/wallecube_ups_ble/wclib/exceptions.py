class PacketParseError(Exception):
    """Frame received from the device could not be decoded"""


class SessionKeyError(Exception):
    """No candidate session key decrypted the device's frames"""


class UnsupportedBluetoothProtocol(Exception):
    """Device does not expose the expected GATT characteristic"""

    def __init__(self, characteristic: str, available: list[str]) -> None:
        listing = "\n    ".join(available)
        super().__init__(
            f"Device does not expose characteristic {characteristic}.\n"
            f"Available characteristics:\n    {listing}"
        )


class SettingUnavailable(Exception):
    """A setting could not be changed because its current value is not known"""


class SettingNotConfirmed(Exception):
    """The device did not report that the power-board MCU received a setting"""
