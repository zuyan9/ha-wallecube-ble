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


def prop_has_bit_on(bit_position: int) -> Callable[[int | None], bool | None]:
    """Return a transform that checks whether a specific bit is set"""

    def _transform(value: int | None) -> bool | None:
        return None if value is None else bool((value >> bit_position) & 1)

    return _transform


def prop_has_any_bit_on(*bit_positions: int) -> Callable[[int | None], bool | None]:
    """Return a transform that checks whether any of the given bits is set"""
    mask = sum(1 << bit_position for bit_position in bit_positions)

    def _transform(value: int | None) -> bool | None:
        return None if value is None else bool(value & mask)

    return _transform
