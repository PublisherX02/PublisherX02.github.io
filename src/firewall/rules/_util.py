"""Shared helpers for rule implementations."""

from __future__ import annotations

import math
from datetime import date
from typing import Any


def matches_any(tool_name: str, patterns: list[str]) -> bool:
    """True if `tool_name` contains any of `patterns`, case-insensitively."""
    lowered = tool_name.lower()
    return any(pattern.lower() in lowered for pattern in patterns)


def _as_number(value: Any) -> float | None:
    """Parse a JSON-typed argument into a float, or None if it can't be.

    Alpaca's real place_stock_order/place_option_order/place_crypto_order/
    replace_order_by_id schemas type qty/notional/limit_price as JSON
    strings, not numbers (verified against the live inputSchema -- see
    AUDIT.md's A4 finding). A string-typed "100000" is normal, expected
    data from the real API, not malformed input -- so numeric strings are
    parsed here, not rejected. Only genuinely unparseable values (a
    non-numeric string, a missing field, NaN/Infinity) fall through to
    None, which callers treat as "can't assess" -- notional_cap/
    position_cap fail closed for tools where that matters (see their
    sizing_tool_match); cvar_gate/pct_of_adv currently allow instead
    (see README's "What this does not do").
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None

    if not math.isfinite(number):
        # NaN compares False against every threshold (a silent bypass);
        # +/-Infinity is not a real order size either. Treat both as
        # unparseable rather than as a usable number.
        return None
    return number


def extract_notional(
    arguments: dict[str, Any],
    notional_field: str,
    qty_field: str,
    price_field: str,
    contract_multiplier: float = 1.0,
) -> float | None:
    """Best-effort USD notional of an order.

    Prefers an explicit notional field; otherwise falls back to
    qty * price * contract_multiplier. Returns None if neither is present,
    since the order's size can't be assessed.

    `contract_multiplier` only scales the qty * price fallback, never an
    explicit notional field (that's already a dollar amount). Callers
    should pass 100.0 for options tools -- place_option_order's qty is a
    contract count and limit_price is a per-share premium/net-debit, and
    one option contract represents 100 shares -- see notional_cap.py's
    module docstring for the live-schema evidence.
    """
    notional = _as_number(arguments.get(notional_field))
    if notional is not None:
        return notional

    qty = _as_number(arguments.get(qty_field))
    price = _as_number(arguments.get(price_field))
    if qty is not None and price is not None:
        return qty * price * contract_multiplier

    return None


def _parse_occ(symbol: Any) -> tuple[str, str, str, str] | None:
    """Split an OCC-format option symbol into (root, YYMMDD, C/P, 8-digit
    strike), or None if `symbol` isn't shaped like a valid OCC symbol.

    OCC format is root + YYMMDD + C/P + 8-digit strike (strike * 1000),
    e.g. "AAPL250321C00150000" -> ("AAPL", "250321", "C", "00150000"). The
    date/type/strike suffix is always exactly 15 characters, fixed-width;
    the root prefix is the variable-length remainder (roots range 1-6
    characters), so this parses from the right, not from a fixed offset.

    Shared by parse_occ_underlying/parse_occ_expiry so both agree on what
    counts as a valid symbol; not exported, since callers only ever need
    one field of the tuple, not the raw split.
    """
    if not isinstance(symbol, str) or len(symbol) <= 15:
        return None

    root, suffix = symbol[:-15], symbol[-15:]
    date_part, type_char, strike_part = suffix[:6], suffix[6], suffix[7:]
    if not date_part.isdigit() or type_char not in ("C", "P") or not strike_part.isdigit():
        return None
    if not root:
        return None
    return root.upper(), date_part, type_char, strike_part


def parse_occ_underlying(symbol: Any) -> str | None:
    """Extract the underlying ticker from an OCC-format option symbol,
    e.g. "AAPL250321C00150000" -> "AAPL". Returns None if `symbol` isn't a
    string shaped like a valid OCC symbol -- callers must treat that as
    "can't identify the underlying," not as any particular symbol.
    """
    parsed = _parse_occ(symbol)
    return parsed[0] if parsed is not None else None


def parse_occ_option_type(symbol: Any) -> str | None:
    """Extract the option type ("C" or "P") from an OCC-format option
    symbol, e.g. "AAPL250321C00150000" -> "C". Returns None if `symbol`
    isn't shaped like a valid OCC symbol -- callers must treat that as
    "can't determine option type," not as any particular type.

    Same end-anchored parsing as parse_occ_underlying/parse_occ_expiry
    (both delegate to the shared, right-to-left `_parse_occ`): the type
    character sits at a fixed OFFSET FROM THE END (immediately before the
    8-digit strike), not a fixed offset from the start -- invariant to the
    root ticker's length, unlike a fixed "position 15" slice, which would
    silently misparse any root that isn't exactly 4 characters.
    """
    parsed = _parse_occ(symbol)
    return parsed[2] if parsed is not None else None


def parse_occ_expiry(symbol: Any) -> date | None:
    """Extract the expiration date from an OCC-format option symbol's
    YYMMDD component, e.g. "AAPL260918P00220000" -> date(2026, 9, 18).

    Returns None if `symbol` isn't shaped like a valid OCC symbol, or if
    the 6 digits don't form a real calendar date (e.g. month 13) --
    callers must treat that as "can't determine expiry," not as any
    particular date.
    """
    parsed = _parse_occ(symbol)
    if parsed is None:
        return None
    _, date_part, _, _ = parsed
    year, month, day = int(date_part[:2]), int(date_part[2:4]), int(date_part[4:6])
    try:
        return date(2000 + year, month, day)
    except ValueError:
        return None
