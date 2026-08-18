"""Shared helpers for rule implementations."""

from __future__ import annotations

from typing import Any


def matches_any(tool_name: str, patterns: list[str]) -> bool:
    """True if `tool_name` contains any of `patterns`, case-insensitively."""
    lowered = tool_name.lower()
    return any(pattern.lower() in lowered for pattern in patterns)


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def extract_notional(
    arguments: dict[str, Any],
    notional_field: str,
    qty_field: str,
    price_field: str,
) -> float | None:
    """Best-effort USD notional of an order.

    Prefers an explicit notional field; otherwise falls back to qty * price.
    Returns None if neither is present, since the order's size can't be
    assessed.
    """
    notional = _as_number(arguments.get(notional_field))
    if notional is not None:
        return notional

    qty = _as_number(arguments.get(qty_field))
    price = _as_number(arguments.get(price_field))
    if qty is not None and price is not None:
        return qty * price

    return None
