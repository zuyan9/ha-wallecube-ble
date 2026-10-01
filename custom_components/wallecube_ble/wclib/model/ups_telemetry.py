from typing import Annotated

from .base import RawData


class UpsTelemetry(RawData):
    """
    Measurement block the front panel forwards from the power-board MCU

    Units follow the power-board firmware that fills the block: voltages in mV,
    currents in mA, battery level in 0.1 %, temperature in 0.1 °C, remaining time in
    seconds and energy in mWh.
    """

    # compared against the output voltage to detect loss of input power
    dc_input_voltage: Annotated[int, "H"]
    # estimated from the output current and the charging power, not measured
    dc_input_current: Annotated[int, "H"]
    dc_output_voltage: Annotated[int, "H"]
    dc_output_current: Annotated[int, "H"]
    # power-board firmware 1.29 counts the level above the reserve capacity the UPS
    # keeps, older versions the whole charge
    battery_level: Annotated[int, "H"]
    # sum of the cells corrected by the drop over the internal resistance
    battery_voltage: Annotated[int, "H"]
    # the four series cells, from the bottom of the stack up
    cell_voltage_1: Annotated[int, "H"]
    cell_voltage_2: Annotated[int, "H"]
    cell_voltage_3: Annotated[int, "H"]
    cell_voltage_4: Annotated[int, "H"]
    # positive while charging
    battery_current: Annotated[int, "h"]
    # measured with the battery monitor's thermistor
    temperature: Annotated[int, "h"]
    # equivalent full cycles: charge moved in both directions over twice the capacity.
    # The vendor app's decoder takes it for the remaining time.
    battery_cycles: Annotated[int, "H"]
    # runtime at the present output load until the battery is empty
    remaining_time: Annotated[int, "H"]
    # energy delivered at the output, kept across restarts
    energy_total: Annotated[int, "I"]
    # protection and fault conditions, one bit each
    fault_flags: Annotated[int, "I"]
    status_flags: Annotated[int, "H"]
