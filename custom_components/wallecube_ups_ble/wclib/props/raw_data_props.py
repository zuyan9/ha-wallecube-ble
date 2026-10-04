from collections import defaultdict
from functools import cached_property

from ..model.base import RawData
from .raw_data_field import RawDataField
from .updatable_props import UpdatableProps


class RawDataProps(UpdatableProps):
    """Mixin that assigns `raw_field` fields from decoded fixed-width messages"""

    def update_from_bytes[T: RawData](self, data_type: type[T], payload: bytes) -> T:
        """Decode `payload` as `data_type`, assign mapped fields and return message"""
        message = data_type.from_bytes(payload)
        for field in self._datatype_to_field.get(data_type, []):
            setattr(self, field.public_name, message)
        return message

    @cached_property
    def _datatype_to_field(self) -> dict[type[RawData], list[RawDataField]]:
        field_map: dict[type[RawData], list[RawDataField]] = defaultdict(list)
        for field in self._fields:
            if isinstance(field, RawDataField):
                field_map[field.data_attr.message_type].append(field)
        return field_map
