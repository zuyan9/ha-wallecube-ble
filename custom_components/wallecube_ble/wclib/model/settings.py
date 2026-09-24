import struct
from typing import Annotated, Self

from .base import RawData


class AdapterSettings(RawData):
    """
    Power-adapter limits the power board uses for charging and input detection

    The vendor app only asks for the adapter's voltage and current and derives the
    other values from them, see `from_adapter`.
    """

    adapter_current: Annotated[int, "H"]  # mA
    charge_current_limit: Annotated[int, "H"]  # mA
    adapter_voltage: Annotated[int, "H"]  # mV
    stop_charge_voltage: Annotated[int, "H"]  # mV
    power_good_voltage: Annotated[int, "H"]  # mV

    @classmethod
    def from_adapter(cls, voltage: float, current: float) -> Self:
        """
        Derive all limits from the adapter rating like the vendor app does

        The derived limits repeat the app's floating-point expressions, including its
        truncation, so both produce identical blocks.
        """
        return cls(
            adapter_current=round(current * 1000),
            charge_current_limit=int(current * 1000 * 0.7),
            adapter_voltage=round(voltage * 1000),
            stop_charge_voltage=int(voltage * 1000 * 0.965),
            power_good_voltage=int(voltage * 0.958 * 1000),
        )

    def to_bytes(self) -> bytes:
        return struct.pack(
            "<5H",
            self.adapter_current,
            self.charge_current_limit,
            self.adapter_voltage,
            self.stop_charge_voltage,
            self.power_good_voltage,
        )


class StandbySettings(RawData):
    """
    Standby rule: the UPS sleeps once the output current stays below the threshold for
    the configured time

    The power board keeps three further values in the same block that the vendor app
    neither reads nor writes, so they are not modelled.
    """

    time: Annotated[int, "H"]  # s
    current_threshold: Annotated[int, "H"]  # mA

    def to_bytes(self) -> bytes:
        return struct.pack("<2H", self.time, self.current_threshold)
