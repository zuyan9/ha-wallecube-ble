from bleak.backends.scanner import AdvertisementData

from ..connection import UPS_SERVICE_UUID
from ..devicebase import DeviceBase
from ..encryption import LOCAL_NAME_PREFIX
from ..model import UpsTelemetry
from ..packet import TelemetryFrame
from ..props import Field, dataclass_attr_mapper, raw_field
from ..props.raw_data_props import RawDataProps
from ..props.transforms import pdiv, prop_has_bit_on

tele = dataclass_attr_mapper(UpsTelemetry)

# set while the output is powered from the battery, the firmware only shows the
# remaining time in that state
_on_battery = prop_has_bit_on(8)


class Device(DeviceBase, RawDataProps):
    """W150"""

    NAME_PREFIX = "W150-"

    battery_level = raw_field(tele.battery_level, pdiv(10, 1))
    battery_current = raw_field(tele.battery_current, pdiv(1000, 3))
    dc_input_voltage = raw_field(tele.dc_input_voltage, pdiv(1000, 3))
    dc_output_voltage = raw_field(tele.dc_output_voltage, pdiv(1000, 3))
    dc_output_current = raw_field(tele.dc_output_current, pdiv(1000, 3))
    temperature = raw_field(tele.temperature, pdiv(10, 1))
    status_flags = raw_field(tele.status_flags)

    output_power = Field[float]()
    remaining_time_discharging = Field[int]()

    _warned_truncated = False

    @classmethod
    def check(cls, adv_data: AdvertisementData) -> bool:
        local_name = adv_data.local_name or ""
        return (
            local_name.startswith(LOCAL_NAME_PREFIX)
            or UPS_SERVICE_UUID in adv_data.service_uuids
        )

    async def data_parse(self, frame: bytes) -> bool:
        self.reset_updated()

        telemetry_frame = TelemetryFrame.from_bytes(frame)
        if telemetry_frame.truncated and not self._warned_truncated:
            self._warned_truncated = True
            self._logger.warning(
                "Telemetry frame truncated to %d bytes, the connection MTU is too "
                "small to receive all fields",
                len(frame),
            )
        telemetry = self.update_from_bytes(UpsTelemetry, telemetry_frame.payload)

        if self.dc_output_voltage is not None and self.dc_output_current is not None:
            self.output_power = round(
                self.dc_output_voltage * self.dc_output_current, 2
            )

        self.remaining_time_discharging = (
            round(telemetry.remaining_time / 60)
            if telemetry.remaining_time is not None
            and _on_battery(telemetry.status_flags)
            else None
        )

        for field_name in self.updated_fields:
            self.update_callback(field_name)
            self.update_state(field_name, getattr(self, field_name, None))

        return True
