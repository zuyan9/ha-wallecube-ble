import struct
from dataclasses import astuple, dataclass
from functools import cache
from inspect import get_annotations
from typing import (
    Annotated,
    ClassVar,
    Self,
    dataclass_transform,
    get_args,
    get_origin,
)


@dataclass_transform()
class RawData:
    r"""
    Fixed-width little-endian binary message

    Declare fields as `Annotated[<type>, "<struct format character>"]`, see
    https://docs.python.org/3/library/struct.html#format-characters. All fields default
    to None, so a payload shorter than `SIZE` decodes partially: trailing fields that
    do not fit stay None.

    Examples
    --------
        class Message(RawData):
            voltage: Annotated[int, "H"]
            flags: Annotated[int, "B"]

    >>> Message.from_bytes(b"\x10\x27\x01")
    Message(voltage=10000, flags=1)
    """

    _FORMAT: ClassVar[tuple[str, ...]] = ()
    SIZE: ClassVar[int] = 0

    def __init_subclass__(cls) -> None:
        # start from the parent's format so subclasses can extend a message; fields
        # mapped to the parent are not assigned from a subclass, see
        # `RawDataProps.update_from_bytes`
        format_chars = list(cls._FORMAT)
        for name, annotation in get_annotations(cls).items():
            if get_origin(annotation) is not Annotated:
                continue
            _, *metadata = get_args(annotation)
            if not metadata:
                continue
            format_chars.append(metadata[0])
            setattr(cls, name, None)

        dataclass(cls)
        cls._FORMAT = tuple(format_chars)
        cls.SIZE = struct.calcsize(cls._struct_format(len(format_chars)))

    @classmethod
    def from_bytes(cls, data: bytes) -> Self:
        """Decode data, leaving fields that do not fit in `data` as None"""
        count = cls._fields_fitting(len(data))
        fmt = cls._struct_format(count)
        return cls(*struct.unpack(fmt, data[: struct.calcsize(fmt)]))

    def to_bytes(self) -> bytes:
        """Encode the message, all fields have to be set"""
        values = astuple(self)
        return struct.pack(self._struct_format(len(values)), *values)

    @classmethod
    def _struct_format(cls, count: int) -> str:
        return "<" + "".join(cls._FORMAT[:count])

    @classmethod
    @cache
    def _fields_fitting(cls, data_len: int) -> int:
        count = len(cls._FORMAT)
        while count and struct.calcsize(cls._struct_format(count)) > data_len:
            count -= 1
        return count
