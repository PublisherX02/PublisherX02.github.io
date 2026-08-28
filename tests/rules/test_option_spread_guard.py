"""Tests for the option_spread_guard rule."""

from firewall.market_data import OptionQuote, OptionQuoteResult
from firewall.rules.base import RuleConfig
from firewall.rules.option_spread_guard import OptionSpreadGuardRule


def _rule(quote_result: OptionQuoteResult | None, **params):
    config = RuleConfig.model_validate(
        {
            "id": "test-option-spread-guard",
            "type": "option_spread_guard",
            "severity": "hard",
            "regulation_ref": "SEC Rule 15c3-5(c)(1)(ii)",
            **params,
        }
    )
    calls: list[str] = []

    def fake_fetcher(symbol: str) -> OptionQuoteResult:
        calls.append(symbol)
        return quote_result

    return OptionSpreadGuardRule(config, quote_fetcher=fake_fetcher), calls


def _quote(bid: float, ask: float) -> OptionQuoteResult:
    return OptionQuoteResult(ok=True, quote=OptionQuote(bid=bid, ask=ask))


def test_market_order_wide_spread_hard_blocks():
    # relative spread = (ask - bid) / ask = (1.00 - 0.80) / 1.00 = 0.20
    rule, calls = _rule(_quote(bid=0.80, ask=1.00), max_relative_spread=0.15)

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1", "type": "market"},
        {},
    )

    assert outcome.triggered
    assert "relative spread 20%" in outcome.reason or "relative spread 20.0%" in outcome.reason
    assert "maximum 15%" in outcome.reason or "maximum 15.0%" in outcome.reason
    assert "high slippage risk" in outcome.reason
    assert "use a limit order or a more liquid strike" in outcome.reason
    assert calls == ["AAPL260918P00220000"]


def test_market_order_tight_spread_passes():
    # relative spread = (1.02 - 1.00) / 1.02 ~= 1.96%
    rule, _ = _rule(_quote(bid=1.00, ask=1.02), max_relative_spread=0.15)

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1", "type": "market"},
        {},
    )

    assert not outcome.triggered


def test_spread_exactly_at_threshold_passes():
    # (ask - bid) / ask == 0.15 exactly -- "exceeds" must mean strictly over.
    # Round numbers deliberately (not realistic option prices): 0.85/1.00
    # lands on 0.15000000000000002 due to binary float representation,
    # which would make this test flaky-by-construction; 85.0/100.0
    # divides to exactly 0.15 in IEEE 754 double precision.
    rule, _ = _rule(_quote(bid=85.0, ask=100.0), max_relative_spread=0.15)

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1", "type": "market"},
        {},
    )

    assert not outcome.triggered


def test_default_order_type_is_market():
    # Real place_option_order schema: `type` defaults to "market" when
    # omitted (verified against the live inputSchema) -- must be treated
    # as a market order, not skipped as "not specified."
    rule, _ = _rule(_quote(bid=0.80, ask=1.00), max_relative_spread=0.15)

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1"},
        {},
    )

    assert outcome.triggered


def test_limit_order_wide_spread_is_not_checked():
    # Limit orders already state their own price protection -- this rule
    # is scoped to market orders only.
    rule, calls = _rule(_quote(bid=0.80, ask=1.00), max_relative_spread=0.15)

    outcome = rule.check(
        "place_option_order",
        {
            "symbol": "AAPL260918P00220000",
            "side": "buy",
            "qty": "1",
            "type": "limit",
            "limit_price": "0.90",
        },
        {},
    )

    assert not outcome.triggered
    assert calls == []  # no market-data call made at all for a limit order


def test_stock_order_is_unchecked():
    rule, calls = _rule(_quote(bid=0.80, ask=1.00), max_relative_spread=0.15)

    outcome = rule.check(
        "place_stock_order", {"symbol": "AAPL", "side": "buy", "qty": "10", "type": "market"}, {}
    )

    assert not outcome.triggered
    assert calls == []


def test_multi_leg_order_is_not_checked():
    # Deliberately out of scope -- see module docstring: a single quote
    # fetch keyed on the parent `symbol`, which multi-leg orders don't
    # carry, and checking N legs' spreads is a different, undecided
    # question (which leg's spread governs an aggregate net-debit market
    # order?) this rule doesn't answer.
    rule, calls = _rule(_quote(bid=0.80, ask=1.00), max_relative_spread=0.15)

    outcome = rule.check(
        "place_option_order",
        {
            "qty": "10",
            "type": "market",
            "order_class": "mleg",
            "legs": [
                {"symbol": "AAPL260918C00150000", "ratio_qty": "1", "side": "buy"},
                {"symbol": "AAPL260918C00160000", "ratio_qty": "1", "side": "sell"},
            ],
        },
        {},
    )

    assert not outcome.triggered
    assert calls == []


def test_unfetchable_quote_fails_closed():
    rule, _ = _rule(
        OptionQuoteResult(ok=False, reason="timed out"), max_relative_spread=0.15
    )

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1", "type": "market"},
        {},
    )

    assert outcome.triggered
    assert "insufficient market data" in outcome.reason.lower()
    assert "timed out" in outcome.reason


def test_default_threshold_is_fifteen_percent():
    rule, _ = _rule(_quote(bid=0.80, ask=1.00))  # 20% spread, no override

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1", "type": "market"},
        {},
    )

    assert outcome.triggered
