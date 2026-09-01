"""Tests for the notional_cap rule."""

from firewall.market_data import BarsResult, DailyBar
from firewall.rules.base import RuleConfig
from firewall.rules.notional_cap import NotionalCapRule


def _rule(bars_fetcher=None, **params) -> NotionalCapRule:
    config = RuleConfig.model_validate(
        {
            "id": "test-notional-cap",
            "type": "notional_cap",
            "severity": "hard",
            "regulation_ref": "SEC Rule 15c3-5(c)(1)(i)",
            **params,
        }
    )
    return NotionalCapRule(config, bars_fetcher=bars_fetcher)


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
    # here) is not, and it's not "place_stock_order" either so the
    # reference-price fallback below doesn't apply to it.
    rule = _rule(max_usd=1000)

    outcome = rule.check("place_order", {"symbol": "AAPL", "side": "buy", "qty": 10}, {})

    assert not outcome.triggered


# --- reference-price fallback for plain place_stock_order market orders --
# see this module's own docstring for why this exists: a market order
# cannot carry limit_price (live-verified HTTP 422 on Alpaca's real API).


def test_stock_market_order_over_cap_via_reference_price_fails_closed_true():
    def fake_bars(symbol, lookback_days, **kwargs):
        return BarsResult(ok=True, bars=[DailyBar(close=600.0, volume=1_000_000.0)])

    rule = _rule(max_usd=1000, bars_fetcher=fake_bars)

    # qty=10 * reference close $600 = $6,000, over the $1,000 cap.
    outcome = rule.check("place_stock_order", {"symbol": "AAPL", "side": "buy", "qty": "10"}, {})

    assert outcome.triggered
    assert "6,000.00" in outcome.reason


def test_stock_market_order_under_cap_via_reference_price_passes():
    def fake_bars(symbol, lookback_days, **kwargs):
        return BarsResult(ok=True, bars=[DailyBar(close=100.0, volume=1_000_000.0)])

    rule = _rule(max_usd=5000, bars_fetcher=fake_bars)

    outcome = rule.check("place_stock_order", {"symbol": "AAPL", "side": "buy", "qty": "10"}, {})

    assert not outcome.triggered


def test_stock_market_order_reference_price_fetch_failure_fails_closed():
    def failing_bars(symbol, lookback_days, **kwargs):
        return BarsResult(ok=False, reason="HTTP 401 fetching bars for AAPL: Unauthorized")

    rule = _rule(max_usd=1000, bars_fetcher=failing_bars)

    outcome = rule.check("place_stock_order", {"symbol": "AAPL", "side": "buy", "qty": "10"}, {})

    assert outcome.triggered
    assert "insufficient market data" in outcome.reason


def test_stock_market_order_without_symbol_falls_through_to_skip():
    def fake_bars(symbol, lookback_days, **kwargs):
        raise AssertionError("must not be called without a symbol")

    rule = _rule(max_usd=1000, bars_fetcher=fake_bars)

    outcome = rule.check("place_stock_order", {"side": "buy", "qty": "10"}, {})

    assert not outcome.triggered


def test_option_market_order_does_not_use_stock_reference_price_fallback():
    # place_option_order is deliberately NOT in stock_tool_match (see this
    # module's docstring) -- a qty-only option market order must still just
    # skip, not attempt to price an OCC symbol via fetch_daily_bars.
    def fake_bars(symbol, lookback_days, **kwargs):
        raise AssertionError("must not be called for an option order")

    rule = _rule(max_usd=1000, bars_fetcher=fake_bars)

    outcome = rule.check(
        "place_option_order", {"symbol": "AAPL250321C00150000", "side": "buy", "qty": "1"}, {}
    )

    assert not outcome.triggered


# --- dynamic cap (max_pct_of_equity), with max_usd as the static fallback --


def test_max_pct_of_equity_unset_always_uses_max_usd():
    rule = _rule(max_usd=1000)  # max_pct_of_equity defaults to None

    outcome = rule.check(
        "place_order", {"qty": 10, "limit_price": 150}, {"account_equity": 1_000_000.0}
    )

    assert outcome.triggered  # $1,500 > static $1,000, unaffected by the huge equity


def test_max_pct_of_equity_computes_dynamic_cap_when_equity_present():
    rule = _rule(max_usd=1000, max_pct_of_equity=0.05)

    # 5% of $100,000 equity = $5,000 cap; $4,999.90 notional passes.
    outcome = rule.check(
        "place_order", {"qty": 10, "limit_price": 499.99}, {"account_equity": 100_000.0}
    )

    assert not outcome.triggered


def test_max_pct_of_equity_over_dynamic_cap_triggers_with_pct_in_reason():
    rule = _rule(max_usd=1000, max_pct_of_equity=0.05)

    outcome = rule.check(
        "place_order", {"qty": 10, "limit_price": 600.0}, {"account_equity": 100_000.0}
    )

    assert outcome.triggered
    assert "5.0% of equity" in outcome.reason


def test_max_pct_of_equity_falls_back_to_max_usd_when_equity_missing():
    # Deliberately NOT fail-closed on missing equity -- see this module's
    # own "DYNAMIC CAP, WITH A STATIC FALLBACK" docstring section: notional
    # sits first in evaluation order, so failing closed here would mask
    # every other rule's reason during an account-data outage.
    rule = _rule(max_usd=1000, max_pct_of_equity=0.05)

    outcome = rule.check("place_order", {"qty": 10, "limit_price": 150}, {})

    assert outcome.triggered  # $1,500 > static $1,000 fallback
    assert "of equity" not in outcome.reason


def test_max_pct_of_equity_falls_back_when_equity_non_numeric():
    rule = _rule(max_usd=1000, max_pct_of_equity=0.05)

    outcome = rule.check(
        "place_order", {"qty": 10, "limit_price": 150}, {"account_equity": "not-a-number"}
    )

    assert outcome.triggered
    assert "of equity" not in outcome.reason
