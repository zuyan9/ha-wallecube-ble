from collections.abc import Callable


def pdiv(
    divisor: float, precision: int | None = None
) -> Callable[[float | None], float | None]:
    """Return a transform that divides by divisor, optionally rounding"""

    def _divide(value: float | None) -> float | None:
        if value is None:
            return None
        result = value / divisor
        return round(result, precision) if precision is not None else result

    return _divide


def pround(precision: int = 2) -> Callable[[float | None], float | None]:
    """Return a transform that rounds to the given precision"""

    def _round(value: float | None) -> float | None:
        return None if value is None else round(value, precision)

    return _round


def prop_has_bit_on(bit_position: int) -> Callable[[int | None], bool]:
    """Return a transform that checks whether a specific bit is set"""

    def _transform(value: int | None) -> bool:
        return value is not None and bool((value >> bit_position) & 1)

    return _transform
