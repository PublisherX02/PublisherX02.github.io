"""Tests for the hedge_cost_cap rule."""

from firewall.market_data import OptionQuote, OptionQuoteResult
from firewall.rules.base import RuleConfig
from firewall.rules.hedge_cost_cap import HedgeCostCapRule


def _rule(quote_result: OptionQuoteResult | None, **params):
    config = RuleConfig.model_validate(
        {
            "id": "test-hedge-cost-cap",
            "type": "hedge_cost_cap",
            "severity": "hard",
            "regulation_ref": None,
            "max_pct_of_equity": 0.02,
            **params,
        }
    )
    calls: list[str] = []

    def fake_fetcher(symbol: str) -> OptionQuoteResult:
        calls.append(symbol)
        return quote_result

    return HedgeCostCapRule(config, quote_fetcher=fake_fetcher), calls


def _quote(ask: float, bid: float = 0.01) -> OptionQuoteResult:
    return OptionQuoteResult(ok=True, quote=OptionQuote(bid=bid, ask=ask))


_ARGS = {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "2"}
_STATE = {"account_equity": 100_000.0}


def test_hedge_cost_within_cap_passes():
    # cost = 4.00 * 100 * 2 = 800; cap = 100_000 * 0.02 = 2000.
    rule, calls = _rule(_quote(ask=4.00), max_pct_of_equity=0.02)

    outcome = rule.check("place_option_order", _ARGS, _STATE)

    assert not outcome.triggered
    assert calls == ["AAPL260918P00220000"]


def test_hedge_cost_exceeding_cap_hard_blocks():
    # cost = 15.00 * 100 * 2 = 3000; cap = 100_000 * 0.02 = 2000.
    rule, _ = _rule(_quote(ask=15.00), max_pct_of_equity=0.02)

    outcome = rule.check("place_option_order", _ARGS, _STATE)

    assert outcome.triggered
    assert "proposed hedge costs $3,000.00" in outcome.reason
    assert "3.0% of NAV" in outcome.reason
    assert "exceeds maximum 2.0%" in outcome.reason
    assert "reduce quantity or select a cheaper strike" in outcome.reason


def test_hedge_cost_exactly_at_cap_passes():
    # cost = 10.00 * 100 * 2 = 2000, exactly equal to the cap -- strict ">"
    # like every other threshold rule in this package.
    rule, _ = _rule(_quote(ask=10.00), max_pct_of_equity=0.02)

    outcome = rule.check("place_option_order", _ARGS, _STATE)

    assert not outcome.triggered


def test_uses_ask_not_bid():
    # A wide bid/ask (this rule doesn't care about spread, only cost) --
    # proves the formula reads .ask, not .bid or a mid-price.
    rule, _ = _rule(_quote(ask=15.00, bid=1.00), max_pct_of_equity=0.02)

    outcome = rule.check("place_option_order", _ARGS, _STATE)

    assert outcome.triggered  # 15.00 * 100 * 2 = 3000 > 2000 cap


def test_sell_side_is_unchecked():
    # Selling receives premium, it doesn't spend it -- "cost" framing
    # doesn't apply, and option_sell_guard separately hard-blocks sells.
    rule, calls = _rule(_quote(ask=999.00), max_pct_of_equity=0.02)

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "sell", "qty": "2"},
        _STATE,
    )

    assert not outcome.triggered
    assert calls == []


def test_default_side_is_buy():
    rule, _ = _rule(_quote(ask=15.00), max_pct_of_equity=0.02)

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "qty": "2"},  # no "side" field
        _STATE,
    )

    assert outcome.triggered  # same as the explicit side="buy" case


def test_stock_order_is_unchecked():
    rule, calls = _rule(_quote(ask=15.00), max_pct_of_equity=0.02)

    outcome = rule.check(
        "place_stock_order",
        {"symbol": "AAPL", "side": "buy", "qty": "10"},
        _STATE,
    )

    assert not outcome.triggered
    assert calls == []


def test_multi_leg_order_is_unchecked():
    rule, calls = _rule(_quote(ask=15.00), max_pct_of_equity=0.02)

    outcome = rule.check(
        "place_option_order",
        {
            "qty": "10",
            "order_class": "mleg",
            "legs": [
                {"symbol": "AAPL260918C00150000", "ratio_qty": "1", "side": "buy"},
                {"symbol": "AAPL260918C00160000", "ratio_qty": "1", "side": "sell"},
            ],
        },
        _STATE,
    )

    assert not outcome.triggered
    assert calls == []


def test_missing_qty_fails_closed():
    rule, _ = _rule(_quote(ask=15.00), max_pct_of_equity=0.02)

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy"},
        _STATE,
    )

    assert outcome.triggered
    assert "cannot compute hedge cost" in outcome.reason.lower()


def test_unparseable_qty_fails_closed():
    rule, _ = _rule(_quote(ask=15.00), max_pct_of_equity=0.02)

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "not-a-number"},
        _STATE,
    )

    assert outcome.triggered
    assert "cannot compute hedge cost" in outcome.reason.lower()


def test_string_typed_qty_is_parsed():
    rule, _ = _rule(_quote(ask=15.00), max_pct_of_equity=0.02)

    outcome = rule.check("place_option_order", _ARGS, _STATE)  # qty is "2" (string)

    assert outcome.triggered  # 15.00 * 100 * 2 = 3000 > 2000 cap


def test_unfetchable_quote_fails_closed():
    rule, _ = _rule(OptionQuoteResult(ok=False, reason="timed out"), max_pct_of_equity=0.02)

    outcome = rule.check("place_option_order", _ARGS, _STATE)

    assert outcome.triggered
    assert "insufficient market data" in outcome.reason.lower()


def test_missing_equity_fails_closed():
    rule, _ = _rule(_quote(ask=4.00), max_pct_of_equity=0.02)

    outcome = rule.check("place_option_order", _ARGS, {})  # no account_equity

    assert outcome.triggered
    assert "insufficient account state" in outcome.reason.lower()


def test_non_numeric_equity_fails_closed():
    rule, _ = _rule(_quote(ask=4.00), max_pct_of_equity=0.02)

    outcome = rule.check(
        "place_option_order", _ARGS, {"account_equity": "not-a-number"}
    )

    assert outcome.triggered
    assert "insufficient account state" in outcome.reason.lower()
