from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar, overload

if TYPE_CHECKING:
    from ..controls import ControlType


class UpdatableProps:
    """
    Mixin for device classes whose attributes are declared as `Field` descriptors

    Assigning a field records its name in `updated_fields` if the value changed, so a
    device can notify only the entities affected by a received message.

    Attributes
    ----------
    updated
        True if any field changed after the last call to `reset_updated`
    """

    updated: bool = False
    _updated_fields: set[str] | None = None
    _fields: ClassVar[list["Field[Any]"]] = []

    @property
    def updated_fields(self) -> set[str]:
        """Names of fields changed after the last call to `reset_updated`"""
        if self._updated_fields is None:
            self._updated_fields = set()
        return self._updated_fields

    def reset_updated(self):
        self.updated = False
        self.updated_fields.clear()

    def get_value[T](self, field: "Field[T] | str") -> T | None:
        return getattr(self, field.public_name if isinstance(field, Field) else field)

    def set_value(self, field: "Field[Any] | str", value: Any):
        setattr(self, field.public_name if isinstance(field, Field) else field, value)

    def get_controls[C: "ControlType"](self, control_type: type[C]) -> list[C]:
        """Return the controls of the given type declared on this device's fields"""
        return [
            f.control
            for f in self._fields
            if f.control is not None and isinstance(f.control, control_type)
        ]

    def __str__(self) -> str:
        cls = f"{self.__class__.__module__}.{self.__class__.__name__}"
        lines = [f"  {f.public_name}: {self.get_value(f)!r}" for f in self._fields]
        return f"{cls}:\n" + "\n".join(lines)


class Skip:
    """Sentinel a transform can return to leave the current value unchanged"""


class Field[T]:
    """Descriptor that stores a value and records when it changes"""

    public_name: str
    private_name: str
    control: "ControlType | None" = None

    def __init__(self, transform: Callable[[Any], Any] | None = None) -> None:
        self._transform = transform if transform is not None else _identity

    def __set_name__(self, owner: type[UpdatableProps], name: str):
        self.public_name = name
        self.private_name = (
            f"_{name}" if not hasattr(owner, f"_{name}") else f"__{name}"
        )
        # assigning creates a per-class list that still contains the inherited fields
        owner._fields = [*(f for f in owner._fields if f.public_name != name), self]

    @overload
    def __get__(self, instance: None, owner: type) -> "Field[T]": ...

    @overload
    def __get__(self, instance: UpdatableProps, owner: type) -> T | None: ...

    def __get__(self, instance: UpdatableProps | None, owner: type):
        if instance is None:
            return self
        return getattr(instance, self.private_name, None)

    def __set__(self, instance: UpdatableProps, value: Any):
        if (value := self._transform(value)) is Skip:
            return
        if value == getattr(instance, self.private_name, None):
            return
        setattr(instance, self.private_name, value)
        instance.updated = True
        instance.updated_fields.add(self.public_name)

    def __repr__(self):
        return f"{self.__class__.__name__}({self.public_name})"


def _identity(value: Any) -> Any:
    return value
