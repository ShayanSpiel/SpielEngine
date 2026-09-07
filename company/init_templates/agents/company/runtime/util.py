"""Small neutral helpers for the clean runtime."""

from __future__ import annotations

_COMPARE_OPERATORS = {
    "ge": lambda a, b: a >= b,
    "gt": lambda a, b: a > b,
    "eq": lambda a, b: a == b,
    "le": lambda a, b: a <= b,
    "lt": lambda a, b: a < b,
}


def compare(value, operator: str, target) -> bool:
    """Evaluate one metric against its target.

    Unknown operators raise ValueError instead of silently failing
    closed: the CLI validates operators at goal creation, so an unknown
    operator reaching here is a defect to surface, not a comparison.
    """
    operation = _COMPARE_OPERATORS.get(operator)
    if operation is None:
        raise ValueError(f"unknown comparison operator: {operator!r}")
    return operation(value, target)
