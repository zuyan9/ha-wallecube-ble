from collections import defaultdict
from functools import cached_property

from ..logging_util import LogOptions
from ..model.base import RawData
from .raw_data_field import RawDataField
from .updatable_props import UpdatableProps


class RawDataProps(UpdatableProps):
    """Mixin that assigns `raw_field` fields from decoded fixed-width messages"""

    def update_from_data(self, data: RawData, reset: bool = False):
        if reset:
            self.reset_updated()

        for base in type(data).get_bases():
            for field in self._datatype_to_field.get(base, []):
                setattr(self, field.public_name, data)

    def update_from_bytes[T: RawData](
        self, data_type: type[T], payload: bytes, reset: bool = False
    ) -> T:
        """Decode `payload` as `data_type`, assign mapped fields and return message"""
        message = data_type.from_bytes(payload)
        self.update_from_data(message, reset=reset)

        if (logger := getattr(self, "_logger", None)) is not None:
            logger.log_filtered(
                LogOptions.DESERIALIZED_MESSAGES,
                "Decoded %s\n%s",
                data_type.__name__,
                message,
            )
        return message

    @cached_property
    def _datatype_to_field(self) -> dict[type[RawData], list[RawDataField]]:
        field_map: dict[type[RawData], list[RawDataField]] = defaultdict(list)
        for field in self._fields:
            if isinstance(field, RawDataField):
                field_map[field.data_attr.message_type].append(field)
        return field_map
