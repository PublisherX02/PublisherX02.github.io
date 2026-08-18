"""Tests for the pct_of_adv rule."""

from firewall.market_data import BarsResult, DailyBar
from firewall.rules.base import RuleConfig
from firewall.rules.pct_of_adv import PctOfAdvRule


def _bars_result(volumes: list[float], close: float = 100.0) -> BarsResult:
    return BarsResult(
        ok=True, bars=[DailyBar(close=close, volume=v) for v in volumes]
    )


def _rule(result: BarsResult, **params):
    config = RuleConfig.model_validate(
        {
            "id": "test-pct-of-adv",
            "type": "pct_of_adv",
            "severity": "hard",
            "regulation_ref": None,
            **params,
        }
    )
    calls: list[tuple[str, int]] = []

    def fake_fetcher(symbol: str, lookback_days: int) -> BarsResult:
        calls.append((symbol, lookback_days))
        return result

    return PctOfAdvRule(config, bars_fetcher=fake_fetcher), calls


def test_no_tool_match_skips_and_does_not_fetch():
    rule, calls = _rule(_bars_result([1000.0, 1000.0]), max_percent_of_adv=0.1)

    outcome = rule.check("get_account", {}, {})

    assert not outcome.triggered
    assert calls == []


def test_missing_symbol_skips_and_does_not_fetch():
    rule, calls = _rule(_bars_result([1000.0, 1000.0]), max_percent_of_adv=0.1)

    outcome = rule.check("place_order", {"qty": 10, "limit_price": 100.0}, {})

    assert not outcome.triggered
    assert calls == []


def test_missing_notional_skips_and_does_not_fetch():
    # Neither an explicit notional field nor qty+price -- can't be priced.
    rule, calls = _rule(_bars_result([1000.0, 1000.0]), max_percent_of_adv=0.1)

    outcome = rule.check("place_order", {"symbol": "AAPL", "qty": 10}, {})

    assert not outcome.triggered
    assert calls == []


def test_too_few_bars_fails_closed():
    rule, _ = _rule(_bars_result([1000.0]), max_percent_of_adv=0.1)

    outcome = rule.check(
        "place_order", {"symbol": "AAPL", "qty": 10, "limit_price": 100.0}, {}
    )

    assert outcome.triggered
    assert "insufficient market data to assess risk — failing closed" in outcome.reason
    assert "insufficient volume history to assess liquidity risk" in outcome.reason


def test_failed_fetch_fails_closed_with_specific_reason():
    failure = BarsResult(ok=False, reason="HTTP 401 fetching bars for AAPL: Unauthorized")
    rule, _ = _rule(failure, max_percent_of_adv=0.1)

    outcome = rule.check(
        "place_order", {"symbol": "AAPL", "qty": 10, "limit_price": 100.0}, {}
    )

    assert outcome.triggered
    assert "insufficient market data to assess risk — failing closed" in outcome.reason
    assert "insufficient volume history to assess liquidity risk" in outcome.reason
    assert "HTTP 401 fetching bars for AAPL: Unauthorized" in outcome.reason


def test_zero_average_volume_fails_closed():
    rule, _ = _rule(_bars_result([0.0, 0.0, 0.0]), max_percent_of_adv=0.1)

    outcome = rule.check(
        "place_order", {"symbol": "AAPL", "qty": 10, "limit_price": 100.0}, {}
    )

    assert outcome.triggered
    assert "insufficient market data to assess risk — failing closed" in outcome.reason
    assert "insufficient volume history to assess liquidity risk" in outcome.reason


def test_non_positive_close_price_fails_closed():
    rule, _ = _rule(_bars_result([1000.0, 1000.0], close=0.0), max_percent_of_adv=0.1)

    outcome = rule.check(
        "place_order", {"symbol": "AAPL", "qty": 10, "limit_price": 100.0}, {}
    )

    assert outcome.triggered
    assert "insufficient market data to assess risk — failing closed" in outcome.reason
    assert "insufficient volume history to assess liquidity risk" in outcome.reason


def test_within_cap_does_not_trigger():
    # ADV = 100,000 shares; order is $500,000 notional / $100 current price
    # = 5,000 shares -> 5% of ADV, under a 10% cap.
    rule, calls = _rule(_bars_result([100_000.0] * 20), max_percent_of_adv=0.10)

    outcome = rule.check(
        "place_order", {"symbol": "AAPL", "qty": 5_000, "limit_price": 100.0}, {}
    )

    assert not outcome.triggered
    assert calls == [("AAPL", 30)]  # adv_lookback_days default


def test_exceeding_cap_triggers_hard_block_with_exact_reason():
    # ADV = 100,000 shares; order is $2,000,000 notional / $100 current
    # price = 20,000 shares -> 20% of ADV, over a 10% cap.
    rule, _ = _rule(_bars_result([100_000.0] * 20), max_percent_of_adv=0.10)

    outcome = rule.check(
        "place_order", {"symbol": "AAPL", "qty": 20_000, "limit_price": 100.0}, {}
    )

    assert outcome.triggered
    assert outcome.reason == (
        "Order exceeds 20.0% of 30-day ADV (100,000 shares) — "
        "high slippage/market-impact risk."
    )


def test_notional_field_used_when_present_instead_of_qty_times_price():
    # Explicit notional overrides qty*price: $1,000,000 / $100 current
    # price = 10,000 shares -> 10% of a 100,000-share ADV, over a 5% cap.
    rule, _ = _rule(_bars_result([100_000.0] * 20), max_percent_of_adv=0.05)

    outcome = rule.check(
        "place_order",
        {"symbol": "AAPL", "notional": 1_000_000.0, "qty": 1, "limit_price": 1.0},
        {},
    )

    assert outcome.triggered
    assert "10.0%" in outcome.reason


def test_lookback_days_config_is_passed_to_fetcher():
    rule, calls = _rule(
        _bars_result([1000.0] * 10), max_percent_of_adv=0.5, adv_lookback_days=15
    )

    rule.check("place_order", {"symbol": "MSFT", "qty": 1, "limit_price": 10.0}, {})

    assert calls == [("MSFT", 15)]
