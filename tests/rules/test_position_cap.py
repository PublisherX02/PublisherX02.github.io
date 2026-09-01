"""Tests for the position_cap rule."""

from firewall.market_data import BarsResult, DailyBar
from firewall.order_history import OrderHistory
from firewall.rules.base import RuleConfig
from firewall.rules.position_cap import PositionCapRule


def _rule(bars_fetcher=None, **params) -> PositionCapRule:
    config = RuleConfig.model_validate(
        {
            "id": "test-position-cap",
            "type": "position_cap",
            "severity": "hard",
            "regulation_ref": "SEC Rule 15c3-5(c)(1)(i)",
            **params,
        }
    )
    return PositionCapRule(config, bars_fetcher=bars_fetcher)


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
    # Only replace_order_by_id is in sizing_tool_match by default, and
    # "place_order" isn't "place_stock_order" so the reference-price
    # fallback below doesn't apply to it either.
    rule = _rule(max_usd_per_symbol=1000)

    outcome = rule.check("place_order", {"symbol": "AAPL", "side": "buy", "qty": 10}, {})

    assert not outcome.triggered


# --- reference-price fallback for plain place_stock_order market orders --
# see notional_cap.py's module docstring for why this exists.


def test_stock_market_order_over_cap_via_reference_price():
    def fake_bars(symbol, lookback_days, **kwargs):
        return BarsResult(ok=True, bars=[DailyBar(close=600.0, volume=1_000_000.0)])

    rule = _rule(max_usd_per_symbol=1000, bars_fetcher=fake_bars)
    state = {"positions": {"AAPL": 0}}

    # qty=10 * reference close $600 = $6,000, over the $1,000 cap.
    outcome = rule.check("place_stock_order", {"symbol": "AAPL", "side": "buy", "qty": "10"}, state)

    assert outcome.triggered
    assert "6,000.00" in outcome.reason


def test_stock_market_order_reference_price_adds_to_existing_exposure():
    def fake_bars(symbol, lookback_days, **kwargs):
        return BarsResult(ok=True, bars=[DailyBar(close=100.0, volume=1_000_000.0)])

    rule = _rule(max_usd_per_symbol=10000, bars_fetcher=fake_bars)
    state = {"positions": {"AAPL": 9500}}

    # current 9500 + reference notional 1000 (10 * 100) = 10500, over cap.
    outcome = rule.check("place_stock_order", {"symbol": "AAPL", "side": "buy", "qty": "10"}, state)

    assert outcome.triggered


def test_stock_market_order_reference_price_fetch_failure_fails_closed():
    def failing_bars(symbol, lookback_days, **kwargs):
        return BarsResult(ok=False, reason="HTTP 401 fetching bars for AAPL: Unauthorized")

    rule = _rule(max_usd_per_symbol=1000, bars_fetcher=failing_bars)

    outcome = rule.check("place_stock_order", {"symbol": "AAPL", "side": "buy", "qty": "10"}, {})

    assert outcome.triggered
    assert "insufficient market data" in outcome.reason


def test_option_market_order_does_not_use_stock_reference_price_fallback():
    def fake_bars(symbol, lookback_days, **kwargs):
        raise AssertionError("must not be called for an option order")

    rule = _rule(max_usd_per_symbol=1000, bars_fetcher=fake_bars)

    outcome = rule.check(
        "place_option_order", {"symbol": "AAPL250321C00150000", "side": "buy", "qty": "1"}, {}
    )

    assert not outcome.triggered


# --- cross-chunk exposure: in-flight orders since the positions snapshot
# was fetched must be added on top of it (see this module's own
# "CROSS-CHUNK EXPOSURE" docstring section) --------------------------------


def _history_with(*, timestamp, symbol="AAPL", side="buy", qty, price, outcome="filled"):
    history = OrderHistory()
    history.record(
        timestamp=timestamp,
        tool="place_stock_order",
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        order_id="test-order",
        outcome=outcome,
    )
    return history


def test_in_flight_priced_order_after_snapshot_is_added_to_baseline():
    rule = _rule(max_usd_per_symbol=10000)
    history = _history_with(timestamp=100.0, qty=10, price=500.0)  # $5,000, priced
    state = {
        "positions": {"AAPL": 4000.0},
        "positions_fetched_at": 50.0,  # snapshot predates the in-flight order
        "order_history": history,
    }

    # baseline 4000 (snapshot) + 5000 (in-flight, priced) + 2000 (this
    # order) = 11000, over the 10000 cap. Without the in-flight add this
    # would wrongly pass at 4000 + 2000 = 6000.
    outcome = rule.check(
        "place_stock_order", {"symbol": "AAPL", "side": "buy", "qty": "4", "limit_price": "500"}, state
    )

    assert outcome.triggered


def test_in_flight_order_before_snapshot_is_not_double_counted():
    rule = _rule(max_usd_per_symbol=10000)
    # This order predates the snapshot fetch -- already reflected in it.
    history = _history_with(timestamp=10.0, qty=10, price=500.0)
    state = {
        "positions": {"AAPL": 5000.0},
        "positions_fetched_at": 50.0,
        "order_history": history,
    }

    # baseline 5000 (already includes the earlier order) + 2000 (this
    # order) = 7000, under the 10000 cap.
    outcome = rule.check(
        "place_stock_order", {"symbol": "AAPL", "side": "buy", "qty": "4", "limit_price": "500"}, state
    )

    assert not outcome.triggered


def test_in_flight_unpriced_order_uses_reference_price_fallback():
    def fake_bars(symbol, lookback_days, **kwargs):
        return BarsResult(ok=True, bars=[DailyBar(close=500.0, volume=1_000_000.0)])

    rule = _rule(max_usd_per_symbol=10000, bars_fetcher=fake_bars)
    # A plain qty-only market order, no price recorded (the real shape
    # core_strategy.py's chunks submit -- see notional_cap.py's module
    # docstring for why an order can never carry limit_price).
    history = _history_with(timestamp=100.0, qty=10, price=None)
    state = {
        "positions": {"AAPL": 0.0},
        "positions_fetched_at": 50.0,
        "order_history": history,
    }

    # in-flight: 10 * $500 (reference price) = $5,000. + this order's
    # 4 * $500 = $2,000 => $7,000, under the $10,000 cap but proves the
    # fallback actually ran (would be $0 + $2,000 without it).
    outcome = rule.check(
        "place_stock_order", {"symbol": "AAPL", "side": "buy", "qty": "4", "limit_price": "500"}, state
    )
    assert not outcome.triggered

    rule2 = _rule(max_usd_per_symbol=6000, bars_fetcher=fake_bars)
    outcome2 = rule2.check(
        "place_stock_order", {"symbol": "AAPL", "side": "buy", "qty": "4", "limit_price": "500"}, state
    )
    assert outcome2.triggered


def test_in_flight_different_symbol_is_not_counted():
    history = _history_with(timestamp=100.0, symbol="MSFT", qty=100, price=500.0)
    state = {
        "positions": {"AAPL": 0.0},
        "positions_fetched_at": 50.0,
        "order_history": history,
    }
    rule = _rule(max_usd_per_symbol=6000)

    outcome = rule.check(
        "place_stock_order", {"symbol": "AAPL", "side": "buy", "qty": "4", "limit_price": "500"}, state
    )

    assert not outcome.triggered


def test_in_flight_sell_is_not_counted():
    history = _history_with(timestamp=100.0, side="sell", qty=10, price=500.0)
    state = {
        "positions": {"AAPL": 0.0},
        "positions_fetched_at": 50.0,
        "order_history": history,
    }
    rule = _rule(max_usd_per_symbol=6000)

    outcome = rule.check(
        "place_stock_order", {"symbol": "AAPL", "side": "buy", "qty": "4", "limit_price": "500"}, state
    )

    assert not outcome.triggered


def test_in_flight_cancelled_order_is_not_counted():
    history = _history_with(timestamp=100.0, qty=10, price=500.0, outcome="cancelled")
    state = {
        "positions": {"AAPL": 0.0},
        "positions_fetched_at": 50.0,
        "order_history": history,
    }
    rule = _rule(max_usd_per_symbol=6000)

    outcome = rule.check(
        "place_stock_order", {"symbol": "AAPL", "side": "buy", "qty": "4", "limit_price": "500"}, state
    )

    assert not outcome.triggered


def test_missing_positions_fetched_at_skips_in_flight_reconciliation():
    # No successful positions fetch to reconcile against -- treated the
    # same as position_cap's own pre-existing missing-positions posture
    # (fail open, not closed).
    history = _history_with(timestamp=100.0, qty=100, price=500.0)
    state = {"order_history": history}  # no "positions", no "positions_fetched_at"
    rule = _rule(max_usd_per_symbol=6000)

    outcome = rule.check(
        "place_stock_order", {"symbol": "AAPL", "side": "buy", "qty": "4", "limit_price": "500"}, state
    )

    assert not outcome.triggered


# --- dynamic cap (max_pct_of_equity), with max_usd_per_symbol as the
# static fallback -- see notional_cap.py's identical test section --------


def test_max_pct_of_equity_unset_always_uses_max_usd_per_symbol():
    rule = _rule(max_usd_per_symbol=1000)
    state = {"positions": {"AAPL": 0}, "account_equity": 1_000_000.0}

    outcome = rule.check(
        "place_order", {"symbol": "AAPL", "side": "buy", "qty": 10, "limit_price": 150}, state
    )

    assert outcome.triggered  # $1,500 > static $1,000, unaffected by the huge equity


def test_max_pct_of_equity_computes_dynamic_cap_when_equity_present():
    rule = _rule(max_usd_per_symbol=1000, max_pct_of_equity=0.25)
    state = {"positions": {"AAPL": 0}, "account_equity": 100_000.0}

    # 25% of $100,000 = $25,000 cap; $20,000 notional passes.
    outcome = rule.check(
        "place_order", {"symbol": "AAPL", "side": "buy", "qty": 100, "limit_price": 200.0}, state
    )

    assert not outcome.triggered


def test_max_pct_of_equity_over_dynamic_cap_triggers_with_pct_in_reason():
    rule = _rule(max_usd_per_symbol=1000, max_pct_of_equity=0.25)
    state = {"positions": {"AAPL": 20000.0}, "account_equity": 100_000.0}

    outcome = rule.check(
        "place_order", {"symbol": "AAPL", "side": "buy", "qty": 100, "limit_price": 200.0}, state
    )

    assert outcome.triggered
    assert "25.0% of equity" in outcome.reason


def test_max_pct_of_equity_falls_back_to_max_usd_per_symbol_when_equity_missing():
    rule = _rule(max_usd_per_symbol=1000, max_pct_of_equity=0.25)
    state = {"positions": {"AAPL": 0}}

    outcome = rule.check(
        "place_order", {"symbol": "AAPL", "side": "buy", "qty": 10, "limit_price": 150}, state
    )

    assert outcome.triggered  # $1,500 > static $1,000 fallback
    assert "of equity" not in outcome.reason
