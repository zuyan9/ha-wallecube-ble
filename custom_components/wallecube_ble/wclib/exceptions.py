class PacketParseError(Exception):
    """Frame received from the device could not be decoded"""


class SessionKeyError(Exception):
    """No candidate session key decrypted the device's frames"""


class ConnectionTimeout(TimeoutError):
    """Connection timeout reached"""


class MaxConnectionAttemptsReached(Exception):
    """Device could not complete the initial connection after maximum attempts"""

    def __init__(
        self, last_error: Exception | type[Exception] | None = None, attempts: int = 0
    ) -> None:
        super().__init__(
            f"Could not connect to device after {attempts} unsuccessful attempts"
        )
        self.last_error = last_error
        self.attempts = attempts


class MaxReconnectAttemptsReached(Exception):
    """Device could not reconnect after maximum attempts"""

    def __init__(
        self, last_error: Exception | type[Exception] | None = None, attempts: int = 0
    ) -> None:
        super().__init__(
            f"Could not reconnect to device after {attempts} unsuccessful attempts"
        )
        self.last_error = last_error
        self.attempts = attempts


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
