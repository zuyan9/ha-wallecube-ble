from collections.abc import Callable
from dataclasses import fields
from functools import cached_property
from typing import Any, cast, overload

from ..model.base import RawData
from .updatable_props import Field


class _DataclassAttr:
    """Reference to one field of a `RawData` message type"""

    def __init__(self, message_type: type[RawData], attr: str):
        self.message_type = message_type
        self.attr = attr

    def __repr__(self):
        return f"dataclass_attr({self.message_type.__name__}.{self.attr})"


class _DataclassAccessor:
    def __init__(self, message_type: type[RawData]):
        self._message_type = message_type

    @cached_property
    def _field_names(self) -> set[str]:
        return {f.name for f in fields(self._message_type)}

    def __getattr__(self, name: str) -> _DataclassAttr:
        if name.startswith("_") or name not in self._field_names:
            raise AttributeError(
                f"{self._message_type.__name__} does not contain field named '{name}'"
            )
        return _DataclassAttr(self._message_type, name)


def dataclass_attr_mapper[T: RawData](message_type: type[T]) -> type[T]:
    """
    Create an accessor whose attributes reference fields of `message_type`

    The result is typed as the message type so field references are checked by type
    checkers, e.g. `raw_field(dataclass_attr_mapper(Message).voltage)`.
    """
    return cast("type[T]", _DataclassAccessor(message_type))


class RawDataField[T](Field[T]):
    """Field that is assigned from an attribute of a decoded `RawData` message"""

    def __init__(
        self,
        data_attr: _DataclassAttr,
        transform: Callable[[Any], Any] | None = None,
    ) -> None:
        super().__init__(transform)
        self.data_attr = data_attr

    def __set__(self, instance, value: Any):
        if isinstance(value, RawData):
            value = getattr(value, self.data_attr.attr)
        super().__set__(instance, value)


@overload
def raw_field[T_ATTR](attr: T_ATTR, transform: None = None) -> RawDataField[T_ATTR]: ...


@overload
def raw_field[T_ATTR, T_OUT](
    attr: T_ATTR, transform: Callable[[T_ATTR], T_OUT]
) -> RawDataField[T_OUT]: ...


def raw_field(
    attr: Any, transform: Callable[[Any], Any] | None = None
) -> RawDataField[Any]:
    """
    Create field that is assigned from a decoded raw message

    Parameters
    ----------
    attr
        Attribute of an accessor returned from `dataclass_attr_mapper`
    transform, optional
        Function applied to the raw value, it should preserve None
    """
    if not isinstance(attr, _DataclassAttr):
        raise TypeError(
            "Attribute has to be taken from `dataclass_attr_mapper`, "
            f"but received value of '{attr}'"
        )
    return RawDataField(data_attr=attr, transform=transform)
