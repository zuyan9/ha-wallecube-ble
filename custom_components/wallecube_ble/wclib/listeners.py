from collections.abc import Callable
from dataclasses import dataclass, field
from inspect import get_annotations
from typing import TYPE_CHECKING, Any, Self, cast, dataclass_transform, overload


class ListenerGroup[T: Callable](list[T]):
    """List of listeners that can be invoked as if it were a single listener"""

    def add(self, listener: T) -> Callable[[], None]:
        """
        Add listener to the group

        Returns
        -------
        Function that removes the listener again
        """
        self.append(listener)

        def _remove() -> None:
            if listener in self:
                self.remove(listener)

        return _remove

    if TYPE_CHECKING:
        __call__: T
    else:

        def __call__(self, *args, **kwargs):
            # iterate over a copy so listeners can unsubscribe while being notified
            for listener in list(self):
                listener(*args, **kwargs)


class _PerInstance[R]:
    """Descriptor that lazily creates one registry per owner instance"""

    def __init__(self, registry_cls: type[R]) -> None:
        self._registry_cls = registry_cls
        self._attr = ""

    def __set_name__(self, owner: type, name: str) -> None:
        self._attr = f"__{name}_registry"

    @overload
    def __get__(self, instance: None, owner: type) -> Self: ...

    @overload
    def __get__(self, instance: object, owner: type) -> R: ...

    def __get__(self, instance: Any, owner: type) -> "Self | R":
        if instance is None:
            return self

        registry = instance.__dict__.get(self._attr)
        if registry is None:
            registry = self._registry_cls()
            instance.__dict__[self._attr] = registry
        return registry


@dataclass_transform(kw_only_default=True)
class ListenerRegistry:
    """
    Declarative collection of listener groups

    Every annotated attribute of a subclass becomes a `ListenerGroup`. Assign the
    result of `create()` to a class attribute to get one registry per instance.
    """

    def __init_subclass__(cls) -> None:
        for name in get_annotations(cls):
            setattr(cls, name, field(default_factory=ListenerGroup))

        dataclass(cls)

    @classmethod
    def create(cls) -> Self:
        return cast("Self", _PerInstance(cls))
