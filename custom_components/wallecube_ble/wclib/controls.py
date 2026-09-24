"""
Declarations of writable device settings

A device module decorates its setter methods with a control type bound to the field
that holds the current value, e.g.

    @controls.select(buzzer_mode, options=BuzzerMode)
    async def set_buzzer_mode(self, mode: BuzzerMode): ...

The Home Assistant platforms discover the controls with `get_controls` and build their
entities from them, so adding a control needs no platform code.
"""

import dataclasses
import enum
import functools
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast, dataclass_transform

if TYPE_CHECKING:
    from .devicebase import DeviceBase
    from .props.updatable_props import Field


@dataclasses.dataclass
@dataclass_transform(field_specifiers=(dataclasses.field,))
class ControlType:
    """Writable setting bound to the field that holds its current value"""

    field: "Field[Any]" = dataclasses.field(hash=False)

    enabled: bool = dataclasses.field(default=True, kw_only=True)
    translation_key: str | None = dataclasses.field(default=None, kw_only=True)

    @property
    def key(self) -> str:
        return self.field.public_name

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        dataclasses.dataclass(cls)

    def _register(self) -> None:
        self.field.control = self


class switch(ControlType):
    type EnableFunc = Callable[[DeviceBase, bool], Awaitable[None]]

    enable_func: EnableFunc = dataclasses.field(
        default=None,  # pyright: ignore[reportAssignmentType]
        repr=False,
        init=False,
    )

    def __call__[F: Callable[..., Awaitable[None]]](self, func: F) -> F:
        self.enable_func = func
        self._register()
        return func


class NumberType(ControlType):
    type ValueFunc = Callable[[DeviceBase, float], Awaitable[None]]

    set_value_func: ValueFunc = dataclasses.field(
        default=None,  # pyright: ignore[reportAssignmentType]
        repr=False,
        init=False,
    )
    min: float = dataclasses.field(default=0, kw_only=True)
    max: float = dataclasses.field(default=100, kw_only=True)
    step: float = dataclasses.field(default=1, kw_only=True)

    def __call__[F: Callable[..., Awaitable[None]]](self, func: F) -> F:
        control = self

        # the device method itself is replaced, so direct library calls get the same
        # limits as Home Assistant
        @functools.wraps(func)
        async def _clamped(device: "DeviceBase", value: float) -> None:
            await func(device, min(max(value, control.min), control.max))

        self.set_value_func = _clamped
        self._register()
        return cast("F", _clamped)


class duration(NumberType):
    """Number in seconds"""


class current(NumberType):
    """Number in amperes"""


class current_ma(NumberType):
    """Number in milliamperes"""


class voltage(NumberType):
    """Number in volts"""


class select[E: enum.IntEnum](ControlType):
    type SetFunc = Callable[[DeviceBase, Any], Awaitable[None]]

    options: type[E] = dataclasses.field(kw_only=True)
    set_value_func: SetFunc = dataclasses.field(
        default=None,  # pyright: ignore[reportAssignmentType]
        repr=False,
        init=False,
    )

    @property
    def options_str(self) -> list[str]:
        return [option_name(option) for option in self.options]

    def __call__[F: Callable[..., Awaitable[None]]](self, func: F) -> F:
        options = self.options

        @functools.wraps(func)
        async def _from_option(device: "DeviceBase", value: E | str) -> None:
            await func(
                device, options[value.upper()] if isinstance(value, str) else value
            )

        self.set_value_func = _from_option
        self._register()
        return cast("F", _from_option)


def option_name(option: enum.IntEnum) -> str:
    """Option key of an enum member as used in select entities and translations"""
    return option.name.lower()
