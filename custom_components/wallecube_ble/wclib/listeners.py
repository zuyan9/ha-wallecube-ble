from collections.abc import Callable
from typing import TYPE_CHECKING


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
