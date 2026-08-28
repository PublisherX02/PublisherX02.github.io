"""Tests for firewall.rules._util's numeric coercion and notional extraction.

Conformance-audit finding A4: Alpaca's real place_stock_order/
place_option_order/place_crypto_order/replace_order_by_id schemas type
qty/notional/limit_price as JSON strings, not numbers. A string-typed
"100000" is normal, expected data from the real API -- not malformed
input -- so _as_number must parse it, not treat every real order as
unassessable.
"""

from datetime import date

from firewall.rules._util import (
    _as_number,
    extract_notional,
    parse_occ_expiry,
    parse_occ_option_type,
    parse_occ_underlying,
)


# --- _as_number --------------------------------------------------------


def test_parses_int():
    assert _as_number(100000) == 100000.0


def test_parses_float():
    assert _as_number(500.5) == 500.5


def test_parses_numeric_string():
    assert _as_number("100000") == 100000.0


def test_parses_decimal_string():
    assert _as_number("500.00") == 500.0


def test_parses_negative_numeric_string():
    assert _as_number("-12.5") == -12.5


def test_rejects_non_numeric_string():
    assert _as_number("not-a-number") is None


def test_rejects_empty_string():
    assert _as_number("") is None


def test_rejects_none():
    assert _as_number(None) is None


def test_rejects_bool():
    # bool is a subclass of int in Python; must not be treated as a number.
    assert _as_number(True) is None
    assert _as_number(False) is None


def test_rejects_nan_string():
    # float("nan") succeeds but is not a usable order size -- a NaN
    # notional would compare False against every cap (bypassing it),
    # so this must be treated as unparseable, not as zero risk.
    assert _as_number("nan") is None


def test_rejects_infinity_string():
    assert _as_number("inf") is None
    assert _as_number("-inf") is None


def test_rejects_list():
    assert _as_number([100000]) is None


# --- extract_notional ---------------------------------------------------


def test_extract_notional_prefers_explicit_notional_field():
    result = extract_notional(
        {"notional": "1234.5", "qty": "1", "limit_price": "1"},
        "notional",
        "qty",
        "limit_price",
    )
    assert result == 1234.5


def test_extract_notional_computes_qty_times_price_from_strings():
    # The exact live-execution shape from the audit: place_stock_order
    # with qty="100000", limit_price="500.00" -- true notional $50,000,000.
    result = extract_notional(
        {"qty": "100000", "limit_price": "500.00"}, "notional", "qty", "limit_price"
    )
    assert result == 50_000_000.0


def test_extract_notional_none_when_qty_unparseable():
    result = extract_notional(
        {"qty": "not-a-number", "limit_price": "500.00"},
        "notional",
        "qty",
        "limit_price",
    )
    assert result is None


def test_extract_notional_none_when_both_fields_absent():
    result = extract_notional({}, "notional", "qty", "limit_price")
    assert result is None


def test_extract_notional_mixed_numeric_and_string_types():
    # Real payloads can mix a numeric qty with a string price (or vice
    # versa) depending on which fields a caller set explicitly.
    result = extract_notional(
        {"qty": 100, "limit_price": "50.5"}, "notional", "qty", "limit_price"
    )
    assert result == 5050.0


def test_extract_notional_applies_contract_multiplier_to_qty_times_price():
    # place_option_order's qty is a contract count and limit_price is a
    # per-share premium/net-debit -- one option contract represents 100
    # shares, so the real dollar notional is qty * price * 100, not
    # qty * price. Multi-leg orders (parent qty = strategy multiplier,
    # limit_price = net debit/credit) get the same treatment.
    result = extract_notional(
        {"qty": "10", "limit_price": "5.00"},
        "notional",
        "qty",
        "limit_price",
        contract_multiplier=100.0,
    )
    assert result == 5000.0


def test_extract_notional_multiplier_does_not_apply_to_explicit_notional_field():
    # An explicit "notional" field (stock/crypto market orders) is already
    # a dollar amount -- the contract multiplier must only apply to the
    # qty * price fallback, never scale an already-dollar-denominated value.
    result = extract_notional(
        {"notional": "1234.5", "qty": "1", "limit_price": "1"},
        "notional",
        "qty",
        "limit_price",
        contract_multiplier=100.0,
    )
    assert result == 1234.5


def test_extract_notional_default_multiplier_is_one():
    result = extract_notional(
        {"qty": "10", "limit_price": "5.00"}, "notional", "qty", "limit_price"
    )
    assert result == 50.0


# --- parse_occ_underlying -------------------------------------------------


def test_parse_occ_underlying_extracts_root_symbol():
    # OCC symbol: root + YYMMDD + C/P + 8-digit strike (strike * 1000).
    assert parse_occ_underlying("AAPL250321C00150000") == "AAPL"


def test_parse_occ_underlying_handles_short_root():
    assert parse_occ_underlying("F250321P00012000") == "F"


def test_parse_occ_underlying_handles_put():
    assert parse_occ_underlying("MSFT250321P00300000") == "MSFT"


def test_parse_occ_underlying_none_for_plain_ticker():
    # Too short to contain the fixed 15-char date/type/strike suffix.
    assert parse_occ_underlying("AAPL") is None


def test_parse_occ_underlying_none_for_bad_type_char():
    assert parse_occ_underlying("AAPL250321X00150000") is None


def test_parse_occ_underlying_none_for_non_numeric_date():
    assert parse_occ_underlying("AAPLXX0321C00150000") is None


def test_parse_occ_underlying_none_for_non_numeric_strike():
    assert parse_occ_underlying("AAPL250321CXXXXXXXX") is None


def test_parse_occ_underlying_none_for_non_string():
    assert parse_occ_underlying(None) is None
    assert parse_occ_underlying(12345) is None


# --- parse_occ_expiry ------------------------------------------------------


def test_parse_occ_expiry_decodes_call():
    assert parse_occ_expiry("AAPL260918P00220000") == date(2026, 9, 18)


def test_parse_occ_expiry_decodes_short_root():
    assert parse_occ_expiry("F250321C00012000") == date(2025, 3, 21)


def test_parse_occ_expiry_none_for_plain_ticker():
    assert parse_occ_expiry("AAPL") is None


def test_parse_occ_expiry_none_for_bad_type_char():
    assert parse_occ_expiry("AAPL250321X00150000") is None


def test_parse_occ_expiry_none_for_invalid_calendar_date():
    # Month 13 -- structurally shaped like an OCC symbol (digits in the
    # right places) but not a real date.
    assert parse_occ_expiry("AAPL251321C00150000") is None


def test_parse_occ_expiry_none_for_non_string():
    assert parse_occ_expiry(None) is None
    assert parse_occ_expiry(12345) is None


# --- parse_occ_option_type --------------------------------------------------
#
# End-anchored parsing, same as parse_occ_underlying/parse_occ_expiry (both
# already delegate to the shared, right-to-left _parse_occ): the type
# character is always exactly 8 characters before the end of the string
# (immediately before the 8-digit strike), regardless of the root ticker's
# length -- a fixed start-position offset would break for any ticker that
# isn't exactly 4 characters, which is exactly the bug this function must
# not have.


def test_parse_occ_option_type_decodes_call():
    assert parse_occ_option_type("AAPL250321C00150000") == "C"


def test_parse_occ_option_type_decodes_put():
    assert parse_occ_option_type("AAPL260918P00220000") == "P"


def test_parse_occ_option_type_handles_short_root():
    # Proves end-anchored, not a fixed start offset: "F" is a 1-character
    # root, not the 4-character root a fixed-position slice would assume.
    assert parse_occ_option_type("F250321C00012000") == "C"


def test_parse_occ_option_type_handles_long_root():
    # A root longer than 4 characters, the other direction a fixed offset
    # would get wrong.
    assert parse_occ_option_type("GOOGL250321P00150000") == "P"


def test_parse_occ_option_type_none_for_plain_ticker():
    assert parse_occ_option_type("AAPL") is None


def test_parse_occ_option_type_none_for_bad_type_char():
    assert parse_occ_option_type("AAPL250321X00150000") is None


def test_parse_occ_option_type_none_for_non_string():
    assert parse_occ_option_type(None) is None
    assert parse_occ_option_type(12345) is None
