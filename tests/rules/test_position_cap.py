"""Tests for the position_cap rule."""

from firewall.rules.base import RuleConfig
from firewall.rules.position_cap import PositionCapRule


def _rule(**params) -> PositionCapRule:
    config = RuleConfig.model_validate(
        {
            "id": "test-position-cap",
            "type": "position_cap",
            "severity": "hard",
            "regulation_ref": "SEC Rule 15c3-5(c)(1)(i)",
            **params,
        }
    )
    return PositionCapRule(config)


def test_order_that_breaches_symbol_cap_triggers():
    rule = _rule(max_usd_per_symbol=10000)
    state = {"positions": {"AAPL": 9500}}

    # current 9500 + notional 1000 = 10500, over the 10000 cap.
    outcome = rule.check(
        "place_order",
        {"symbol": "AAPL", "side": "buy", "qty": 10, "limit_price": 100},
        state,
    )

    assert outcome.triggered


def test_order_that_stays_just_under_symbol_cap_passes():
    rule = _rule(max_usd_per_symbol=10000)
    state = {"positions": {"AAPL": 9000}}

    # current 9000 + notional 999 = 9999, just under the 10000 cap.
    outcome = rule.check(
        "place_order",
        {"symbol": "AAPL", "side": "buy", "qty": 10, "limit_price": 99.9},
        state,
    )

    assert not outcome.triggered


def test_replace_order_with_symbol_qty_and_limit_price_still_triggers_over_cap():
    # Full-data amendment: notional is computable and over cap -- must
    # trigger exactly like a place call would (control for the fix below).
    rule = _rule(max_usd_per_symbol=1000)

    outcome = rule.check(
        "replace_order_by_id",
        {"symbol": "AAPL", "side": "buy", "qty": 100000, "limit_price": 500},
        {},
    )

    assert outcome.triggered


def test_replace_order_with_symbol_and_qty_only_fails_closed():
    # Conformance-audit finding A4, same fix as notional_cap: a partial
    # amendment missing limit_price must not silently skip this cap.
    rule = _rule(max_usd_per_symbol=1000)

    outcome = rule.check(
        "replace_order_by_id", {"symbol": "AAPL", "side": "buy", "qty": 100000}, {}
    )

    assert outcome.triggered
    assert "cannot compute notional" in outcome.reason


def test_replace_order_without_symbol_is_unaffected_by_this_fix():
    # Documents a real, unresolved residual gap rather than leaving it
    # silent: Alpaca's actual replace_order_by_id has no `symbol` argument
    # at all (verified against its real input schema), so the earlier
    # `if not symbol` check above fires before extract_notional is ever
    # called -- this fix closes the gap in notional_cap (which has no
    # symbol precondition) but NOT here, because position_cap structurally
    # cannot attribute exposure to a symbol it was never given. Closing
    # this one needs an order_id -> symbol lookup this rule doesn't have,
    # not a None-handling change -- see README's "What this does not do".
    rule = _rule(max_usd_per_symbol=1000)

    outcome = rule.check(
        "replace_order_by_id", {"order_id": "abc123", "qty": 100000, "limit_price": 500}, {}
    )

    assert not outcome.triggered


def test_string_typed_qty_and_price_over_cap_hard_blocks():
    # Conformance-audit finding A4 (live-execution shape): same string-typed
    # real schema as notional_cap -- a string qty/limit_price is normal,
    # expected data and must be parsed, not treated as unassessable.
    rule = _rule(max_usd_per_symbol=5000)
    state = {"positions": {"AAPL": 0}}

    outcome = rule.check(
        "place_stock_order",
        {"symbol": "AAPL", "side": "buy", "qty": "100000", "limit_price": "500.00"},
        state,
    )

    assert outcome.triggered


def test_market_order_with_no_price_is_not_penalized():
    # A plain market order legitimately carries qty and no price at all --
    # this must NOT fail closed just because notional can't be computed.
    # Only replace_order_by_id is in sizing_tool_match by default.
    rule = _rule(max_usd_per_symbol=1000)

    outcome = rule.check("place_order", {"symbol": "AAPL", "side": "buy", "qty": 10}, {})

    assert not outcome.triggered
