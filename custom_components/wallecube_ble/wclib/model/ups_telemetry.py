from typing import Annotated

from .base import RawData


class UpsTelemetry(RawData):
    """
    Measurement block the front panel forwards from the power-board MCU

    Units follow the firmware's display code: voltages in mV, currents in mA, battery
    level in 0.1 %, temperature in 0.1 °C and remaining time in seconds. Input current,
    battery voltage and the energy counter are only named by the vendor app's decoder,
    the cell voltages and the cycle count only by the vendor cloud, which receives the
    same block.
    """

    # compared against the output voltage to detect loss of input power
    dc_input_voltage: Annotated[int, "H"]
    dc_input_current: Annotated[int, "H"]
    dc_output_voltage: Annotated[int, "H"]
    dc_output_current: Annotated[int, "H"]
    battery_level: Annotated[int, "H"]
    battery_voltage: Annotated[int, "H"]
    # the four series cells, in mV like the battery voltage
    cell_voltage_1: Annotated[int, "H"]
    cell_voltage_2: Annotated[int, "H"]
    cell_voltage_3: Annotated[int, "H"]
    cell_voltage_4: Annotated[int, "H"]
    battery_current: Annotated[int, "h"]
    temperature: Annotated[int, "h"]
    # the vendor app's decoder reads its remaining seconds here, but the front panel's
    # screen and the cloud take them from the next field
    battery_cycles: Annotated[int, "H"]
    # only meaningful while running on battery, see `status_flags`
    remaining_time: Annotated[int, "H"]
    # the vendor app divides it by 10^6 and presents energy in kWh
    energy_total: Annotated[int, "I"]
    # the vendor app reads a 32-bit value here but does not use it, neither do the
    # front panel and the cloud
    reserved_32: Annotated[bytes, "4s"]
    status_flags: Annotated[int, "H"]
