from typing import Annotated

from .base import RawData


class InfoBlock(RawData):
    """
    Versions reported by the info characteristic

    The front panel asks the power board for its versions over UART at boot and
    reports its own versions as constants. The vendor cloud lists the same four values
    as UPS and system hardware and software versions, in this order.
    """

    # not initialized by the firmware
    reserved_0: Annotated[bytes, "1s"]
    power_board_hardware_version: Annotated[int, "H"]
    power_board_firmware_version: Annotated[int, "H"]
    hardware_version: Annotated[int, "H"]
    firmware_version: Annotated[int, "H"]
