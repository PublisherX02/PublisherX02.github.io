"""Tests for the cvar_gate rule."""

import pytest

from firewall.market_data import BarsResult, DailyBar
from firewall.rules.base import RuleConfig
from firewall.rules.cvar_gate import (
    CVaRGateRule,
    compute_cvar,
    get_cvar_from_distribution,
)


def _bars_result(closes: list[float]) -> BarsResult:
    return BarsResult(ok=True, bars=[DailyBar(close=c, volume=1000.0) for c in closes])


def _rule(result: BarsResult, **params):
    config = RuleConfig.model_validate(
        {
            "id": "test-cvar-gate",
            "type": "cvar_gate",
            "severity": "hard",
            "regulation_ref": None,
            **params,
        }
    )
    calls: list[tuple[str, int]] = []

    def fake_fetcher(symbol: str, lookback_days: int) -> BarsResult:
        calls.append((symbol, lookback_days))
        return result

    return CVaRGateRule(config, bars_fetcher=fake_fetcher), calls


def _flat_bars(n: int, price: float = 100.0) -> BarsResult:
    return _bars_result([price] * n)


# --- compute_cvar (historical simulation) --------------------------------


def test_compute_cvar_averages_worst_tail():
    pnl_series = [-100, -50, -20, -10, 0, 10, 20, 50, 100]

    cvar = compute_cvar(pnl_series, alpha=0.8)

    # (1 - 0.8) * 9 = 1.8 -> ceil -> 2 worst outcomes: -100, -50.
    assert cvar == pytest.approx(-75.0)


def test_compute_cvar_empty_series_returns_none():
    assert compute_cvar([], alpha=0.95) is None


def test_compute_cvar_tail_size_is_never_zero():
    cvar = compute_cvar([5.0], alpha=1.0)

    assert cvar == pytest.approx(5.0)


# --- get_cvar_from_distribution (forecast distribution, not wired in) ---


def test_get_cvar_from_distribution_within_single_bucket():
    quantiles = [(0.5, -10.0), (0.3, 0.0), (0.2, 10.0)]

    cvar = get_cvar_from_distribution(quantiles, alpha=0.8)

    # Worst 20% probability mass falls entirely inside the 50%-weighted
    # -10 bucket.
    assert cvar == pytest.approx(-10.0)


def test_get_cvar_from_distribution_spans_multiple_buckets():
    quantiles = [(0.1, -50.0), (0.1, -30.0), (0.3, -10.0), (0.5, 20.0)]

    cvar = get_cvar_from_distribution(quantiles, alpha=0.85)

    # Worst 15% mass target is crossed partway through the -30 bucket, but
    # buckets are consumed whole (never split): the -50 bucket (0.1) plus
    # the entire -30 bucket (0.1) = 0.2 covered mass.
    assert cvar == pytest.approx((-50 * 0.1 + -30 * 0.1) / 0.2)


def test_get_cvar_from_distribution_matches_compute_cvar_on_uniform_weights():
    # The whole-point of get_cvar_from_distribution: another source's
    # distribution should be able to feed the same calculation as the
    # historical-simulation path. On n equally-weighted samples, both
    # functions must agree exactly.
    returns = [-100.0, -50.0, -20.0, -10.0, 0.0, 10.0, 20.0, 50.0, 100.0]
    n = len(returns)
    quantiles = [(1 / n, r) for r in returns]

    for alpha in (0.5, 0.8, 0.9, 0.95):
        assert get_cvar_from_distribution(quantiles, alpha=alpha) == pytest.approx(
            compute_cvar(returns, alpha=alpha)
        )


def test_get_cvar_from_distribution_normalizes_unnormalized_weights():
    quantiles = [(2.0, -10.0), (2.0, 0.0), (2.0, 10.0)]

    cvar = get_cvar_from_distribution(quantiles, alpha=0.8)

    assert cvar == pytest.approx(-10.0)


def test_get_cvar_from_distribution_default_alpha_is_usable():
    cvar = get_cvar_from_distribution([(0.5, -10.0), (0.5, 10.0)])

    assert cvar is not None


def test_get_cvar_from_distribution_empty_returns_none():
    assert get_cvar_from_distribution([]) is None


def test_get_cvar_from_distribution_zero_total_probability_returns_none():
    assert get_cvar_from_distribution([(0.0, -10.0)]) is None


# --- CVaRGateRule ----------------------------------------------------------


def test_no_tool_match_skips_and_does_not_fetch():
    rule, calls = _rule(_bars_result([100.0, 101.0]), cvar_max_loss_pct_of_equity=0.01)

    outcome = rule.check("get_account", {}, {})

    assert not outcome.triggered
    assert calls == []


def test_missing_symbol_skips_and_does_not_fetch():
    rule, calls = _rule(_bars_result([100.0, 101.0]), cvar_max_loss_pct_of_equity=0.01)

    outcome = rule.check("place_order", {"qty": 10, "limit_price": 100}, {})

    assert not outcome.triggered
    assert calls == []


def test_missing_notional_skips_and_does_not_fetch():
    rule, calls = _rule(_bars_result([100.0, 101.0]), cvar_max_loss_pct_of_equity=0.01)

    outcome = rule.check("place_order", {"symbol": "AAPL"}, {})

    assert not outcome.triggered
    assert calls == []


def test_missing_account_equity_fails_closed_without_fetching():
    rule, calls = _rule(_bars_result([100.0, 101.0]), cvar_max_loss_pct_of_equity=0.01)

    outcome = rule.check(
        "place_order", {"symbol": "AAPL", "qty": 10, "limit_price": 100}, {}
    )

    assert outcome.triggered
    assert "insufficient account state" in outcome.reason
    assert "failing closed" in outcome.reason
    assert calls == []  # no fetch attempted -- equity is checked first


def test_non_numeric_account_equity_fails_closed_without_fetching():
    rule, calls = _rule(_bars_result([100.0, 101.0]), cvar_max_loss_pct_of_equity=0.01)

    outcome = rule.check(
        "place_order",
        {"symbol": "AAPL", "qty": 10, "limit_price": 100},
        {"account_equity": "not-a-number"},
    )

    assert outcome.triggered
    assert "insufficient account state" in outcome.reason
    assert "failing closed" in outcome.reason
    assert calls == []


def test_too_few_bars_fails_closed():
    rule, _ = _rule(_bars_result([100.0]), cvar_max_loss_pct_of_equity=0.01)

    outcome = rule.check(
        "place_order",
        {"symbol": "AAPL", "qty": 10, "limit_price": 100},
        {"account_equity": 10_000.0},
    )

    assert outcome.triggered
    assert "insufficient market data" in outcome.reason
    assert "failing closed" in outcome.reason


def test_failed_fetch_fails_closed_with_specific_reason():
    failure = BarsResult(ok=False, reason="timed out after 5.0s fetching bars for AAPL")
    rule, _ = _rule(failure, cvar_max_loss_pct_of_equity=0.01)

    outcome = rule.check(
        "place_order",
        {"symbol": "AAPL", "qty": 10, "limit_price": 100},
        {"account_equity": 10_000.0},
    )

    assert outcome.triggered
    assert "insufficient market data" in outcome.reason
    assert "failing closed" in outcome.reason
    assert "timed out after 5.0s fetching bars for AAPL" in outcome.reason


def test_string_typed_qty_and_price_is_assessed_not_skipped():
    # Conformance-audit finding A4 (live-execution shape): a string-typed
    # qty/limit_price (Alpaca's real place_stock_order schema) must be
    # parsed into a real notional and risk-assessed, not silently skipped
    # as if the order couldn't be sized.
    bars = _bars_result([100.0] * 29 + [50.0])
    rule, calls = _rule(
        bars,
        cvar_max_loss_pct_of_equity=0.01,
        cvar_alpha=0.9,
        cvar_lookback_days=45,
    )

    outcome = rule.check(
        "place_stock_order",
        {"symbol": "AAPL", "qty": "100", "limit_price": "100.00"},
        {"account_equity": 100.0},
    )

    assert outcome.triggered
    assert calls == [("AAPL", 45)]


def test_within_max_loss_does_not_trigger():
    # Flat prices -> zero daily returns -> zero CVaR, under any positive cap.
    rule, calls = _rule(_flat_bars(30), cvar_max_loss_pct_of_equity=0.01)

    outcome = rule.check(
        "place_order",
        {"symbol": "AAPL", "qty": 100, "limit_price": 100},
        {"account_equity": 10_000.0},
    )

    assert not outcome.triggered
    assert calls == [("AAPL", 90)]  # cvar_lookback_days default


def test_exceeding_max_loss_triggers_hard_block_with_details_in_reason():
    # A single sharp drop in an otherwise flat series dominates the tail.
    bars = _bars_result([100.0] * 29 + [50.0])
    rule, _ = _rule(
        bars,
        cvar_max_loss_pct_of_equity=0.01,
        cvar_alpha=0.9,
        cvar_lookback_days=45,
    )

    outcome = rule.check(
        "place_order",
        {"symbol": "AAPL", "qty": 100, "limit_price": 100},
        {"account_equity": 100.0},  # max_loss_usd = 0.01 * 100.0 = 1.0
    )

    assert outcome.triggered
    assert "CVaR" in outcome.reason
    assert "45-day" in outcome.reason
    assert "alpha=0.9" in outcome.reason
    assert "AAPL" in outcome.reason


def test_lookback_days_config_is_passed_to_fetcher():
    rule, calls = _rule(
        _flat_bars(10), cvar_max_loss_pct_of_equity=0.01, cvar_lookback_days=30
    )

    rule.check(
        "place_order",
        {"symbol": "MSFT", "qty": 1, "limit_price": 10},
        {"account_equity": 10_000.0},
    )

    assert calls == [("MSFT", 30)]


def test_sell_order_is_exempt_as_deleveraging():
    rule, calls = _rule(
        BarsResult(ok=False, reason="must not fetch"),
        cvar_max_loss_pct_of_equity=0.01,
    )
    outcome = rule.check(
        "place_stock_order",
        {"symbol": "AAPL", "side": "sell", "qty": "10", "limit_price": "100"},
        {},
    )
    assert not outcome.triggered
    assert calls == []
