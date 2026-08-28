"""Tests for the notional_cap rule."""

from firewall.rules.base import RuleConfig
from firewall.rules.notional_cap import NotionalCapRule


def _rule(**params) -> NotionalCapRule:
    config = RuleConfig.model_validate(
        {
            "id": "test-notional-cap",
            "type": "notional_cap",
            "severity": "hard",
            "regulation_ref": "SEC Rule 15c3-5(c)(1)(i)",
            **params,
        }
    )
    return NotionalCapRule(config)


def test_order_over_cap_triggers():
    rule = _rule(max_usd=1000)

    outcome = rule.check("place_order", {"qty": 10, "limit_price": 150}, {})

    assert outcome.triggered


def test_order_just_under_cap_passes():
    rule = _rule(max_usd=1500)

    # qty * price == 1499.90, just under the 1500 cap.
    outcome = rule.check("place_order", {"qty": 10, "limit_price": 149.99}, {})

    assert not outcome.triggered


def test_replace_order_with_qty_and_limit_price_still_triggers_over_cap():
    # Full-data amendment: notional is computable and over cap -- must
    # trigger exactly like a place call would (control for the fix below).
    rule = _rule(max_usd=1000)

    outcome = rule.check(
        "replace_order_by_id", {"order_id": "abc123", "qty": 100000, "limit_price": 500}, {}
    )

    assert outcome.triggered


def test_replace_order_with_qty_only_fails_closed():
    # Conformance-audit finding A4: an Alpaca-legal partial amendment that
    # bumps qty without re-specifying limit_price previously skipped this
    # rule entirely (extract_notional returned None, treated as "can't
    # assess, allow"). It must now fail closed instead.
    rule = _rule(max_usd=1000)

    outcome = rule.check("replace_order_by_id", {"order_id": "abc123", "qty": 100000}, {})

    assert outcome.triggered
    assert "cannot compute notional" in outcome.reason


def test_replace_order_with_limit_price_only_fails_closed():
    rule = _rule(max_usd=1000)

    outcome = rule.check("replace_order_by_id", {"order_id": "abc123", "limit_price": 500}, {})

    assert outcome.triggered
    assert "cannot compute notional" in outcome.reason


def test_string_typed_qty_and_price_over_cap_hard_blocks():
    # Conformance-audit finding A4 (live-execution shape): Alpaca's real
    # place_stock_order schema types qty/limit_price as JSON strings, not
    # numbers. A string-typed "100000" is normal, expected data from the
    # real API -- not malformed input -- and must be parsed and checked
    # like any other order, not silently waved through.
    rule = _rule(max_usd=5000)

    outcome = rule.check(
        "place_stock_order", {"qty": "100000", "limit_price": "500.00"}, {}
    )

    assert outcome.triggered
    assert "50,000,000.00" in outcome.reason


def test_multi_leg_option_order_premium_uses_contract_multiplier():
    # Multi-leg place_option_order calls have no parent "symbol", so
    # position_cap/cvar_gate/pct_of_adv all no-op on them (they require a
    # symbol to assess). notional_cap is the one rule that still reaches
    # this call (no symbol precondition), so it's the only live defense
    # against an oversized multi-leg options structure -- and without a
    # contract multiplier, qty=10 (strategy multiplier) * limit_price=5.00
    # (net debit) computes to $50 instead of the real $5,000 total premium
    # (one contract = 100 shares). This must trigger against a $1,000 cap.
    rule = _rule(max_usd=1000)

    outcome = rule.check(
        "place_option_order",
        {
            "qty": "10",
            "limit_price": "5.00",
            "order_class": "mleg",
            "legs": [
                {"symbol": "AAPL250321C00150000", "ratio_qty": "1", "side": "buy"},
                {"symbol": "AAPL250321C00160000", "ratio_qty": "1", "side": "sell"},
            ],
        },
        {},
    )

    assert outcome.triggered
    assert "5,000.00" in outcome.reason


def test_multi_leg_option_order_premium_under_cap_passes():
    rule = _rule(max_usd=10000)

    outcome = rule.check(
        "place_option_order",
        {"qty": "10", "limit_price": "5.00", "order_class": "mleg", "legs": []},
        {},
    )

    assert not outcome.triggered


def test_non_option_order_does_not_get_contract_multiplier():
    # place_stock_order/place_crypto_order must not get the x100 multiplier
    # -- it only applies to place_option_order's contract-based qty.
    rule = _rule(max_usd=10000)

    outcome = rule.check("place_stock_order", {"qty": "10", "limit_price": "5.00"}, {})

    assert not outcome.triggered


def test_market_order_with_no_price_is_not_penalized():
    # A plain market order (the Alpaca default order type) legitimately
    # carries qty and no price at all -- this must NOT fail closed just
    # because notional can't be computed for it. Only replace_order_by_id
    # is in sizing_tool_match by default; place_order (a place_* stand-in
    # here) is not.
    rule = _rule(max_usd=1000)

    outcome = rule.check("place_order", {"symbol": "AAPL", "side": "buy", "qty": 10}, {})

    assert not outcome.triggered
