from typing import Annotated

from .base import RawData


class UpsTelemetry(RawData):
    """
    Measurement block the front panel forwards from the power-board MCU

    Units follow the firmware's display code: voltages in mV, currents in mA, battery
    level in 0.1 %, temperature in 0.1 °C and remaining time in seconds. The reserved
    fields are not used by the front-panel firmware.
    """

    # compared against the output voltage to detect loss of input power
    dc_input_voltage: Annotated[int, "H"]
    reserved_2: Annotated[int, "H"]
    dc_output_voltage: Annotated[int, "H"]
    dc_output_current: Annotated[int, "H"]
    battery_level: Annotated[int, "H"]
    reserved_10: Annotated[bytes, "10s"]
    battery_current: Annotated[int, "h"]
    temperature: Annotated[int, "h"]
    reserved_24: Annotated[int, "H"]
    # only meaningful while running on battery, see `status_flags`
    remaining_time: Annotated[int, "H"]
    reserved_28: Annotated[bytes, "8s"]
    status_flags: Annotated[int, "H"]
