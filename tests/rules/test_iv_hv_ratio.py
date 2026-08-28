"""Tests for the iv_hv_ratio rule."""

import pytest

from firewall.market_data import BarsResult, DailyBar, OptionQuote, OptionQuoteResult
from firewall.rules.base import RuleConfig
from firewall.rules.cvar_gate import CVaRGateRule
from firewall.rules.iv_hv_ratio import IVHVRatioRule, compute_annualized_hv


def _bars_result(closes: list[float]) -> BarsResult:
    return BarsResult(ok=True, bars=[DailyBar(close=c, volume=1000.0) for c in closes])


def _cvar_gate_rule(bars_result: BarsResult, **cvar_params):
    config = RuleConfig.model_validate(
        {
            "id": "test-cvar-gate",
            "type": "cvar_gate",
            "severity": "hard",
            "regulation_ref": None,
            "cvar_max_loss_pct_of_equity": 0.02,
            **cvar_params,
        }
    )
    calls: list[tuple[str, int]] = []

    def fake_bars_fetcher(symbol: str, lookback_days: int) -> BarsResult:
        calls.append((symbol, lookback_days))
        return bars_result

    return CVaRGateRule(config, bars_fetcher=fake_bars_fetcher), calls


def _rule(quote_result: OptionQuoteResult | None, **params):
    config = RuleConfig.model_validate(
        {
            "id": "test-iv-hv-ratio",
            "type": "iv_hv_ratio",
            "severity": "hard",
            "regulation_ref": None,
            **params,
        }
    )
    calls: list[str] = []

    def fake_quote_fetcher(symbol: str) -> OptionQuoteResult:
        calls.append(symbol)
        return quote_result

    return IVHVRatioRule(config, quote_fetcher=fake_quote_fetcher), calls


def _quote(iv: float) -> OptionQuoteResult:
    return OptionQuoteResult(ok=True, quote=OptionQuote(bid=4.15, ask=4.30, iv=iv))


# Worked example shared by several tests below: closes chosen (via
# 100 * exp(cumulative log return)) so the daily log returns are exactly
# +/-0.02 -- annualized HV = stdev([0.02, -0.02, 0.02, -0.02]) * sqrt(252)
# ~= 0.023094 * 15.8745 ~= 0.36661 (verified independently in
# test_compute_annualized_hv_worked_example below).
_CALM_CLOSES = [100.0, 102.02013400267558, 100.0, 102.02013400267558, 100.0]


# --- compute_annualized_hv (pure function) ---------------------------------


def test_compute_annualized_hv_worked_example():
    hv = compute_annualized_hv([0.02, -0.02, 0.02, -0.02])

    assert hv == pytest.approx(0.36660605559646725)


def test_compute_annualized_hv_requires_at_least_two_returns():
    # A single daily return has no sample variance (n-1 == 0) -- undefined,
    # not zero.
    assert compute_annualized_hv([0.02]) is None


def test_compute_annualized_hv_empty_returns_none():
    assert compute_annualized_hv([]) is None


def test_compute_annualized_hv_zero_variance_is_zero_not_none():
    # A genuinely flat price series has a real, well-defined HV of zero --
    # distinct from "not enough data to tell" (None).
    assert compute_annualized_hv([0.0, 0.0, 0.0]) == 0.0


def test_compute_annualized_hv_custom_annualization_trading_days():
    # 21 trading days (roughly one month) instead of the 252-day default.
    hv = compute_annualized_hv([0.02, -0.02], annualization_trading_days=21.0)

    import math
    import statistics

    expected = statistics.stdev([0.02, -0.02]) * math.sqrt(21.0)
    assert hv == pytest.approx(expected)


# --- IVHVRatioRule.check() --------------------------------------------------


def test_elevated_iv_hard_blocks():
    # IV 0.60 against annualized HV ~0.3666 -> ratio ~1.636, above the
    # default 1.5x ceiling.
    cvar_rule, bars_calls = _cvar_gate_rule(_bars_result(_CALM_CLOSES))
    rule, quote_calls = _rule(_quote(iv=0.60))

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1"},
        {"cvar_gate_rule": cvar_rule},
    )

    assert outcome.triggered
    assert "elevated IV relative to historical volatility" in outcome.reason
    assert quote_calls == ["AAPL260918P00220000"]
    assert bars_calls == [("AAPL", cvar_rule.cfg.cvar_lookback_days)]


def test_calm_iv_relative_to_hv_passes():
    # IV 0.40 against annualized HV ~0.3666 -> ratio ~1.091, below 1.5x.
    cvar_rule, _ = _cvar_gate_rule(_bars_result(_CALM_CLOSES))
    rule, _ = _rule(_quote(iv=0.40))

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1"},
        {"cvar_gate_rule": cvar_rule},
    )

    assert not outcome.triggered


def test_does_not_auto_construct_an_alternative_structure():
    # Reject-and-explain only -- the reason must not describe the firewall
    # building or proposing a replacement order on the agent's behalf.
    cvar_rule, _ = _cvar_gate_rule(_bars_result(_CALM_CLOSES))
    rule, _ = _rule(_quote(iv=0.60))

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1"},
        {"cvar_gate_rule": cvar_rule},
    )

    assert outcome.triggered
    assert "does not construct" in outcome.reason.lower()


def test_stock_order_is_unchecked():
    cvar_rule, bars_calls = _cvar_gate_rule(_bars_result(_CALM_CLOSES))
    rule, quote_calls = _rule(_quote(iv=0.60))

    outcome = rule.check(
        "place_stock_order",
        {"symbol": "AAPL", "side": "buy", "qty": "10"},
        {"cvar_gate_rule": cvar_rule},
    )

    assert not outcome.triggered
    assert quote_calls == []
    assert bars_calls == []


def test_multi_leg_order_is_not_checked():
    cvar_rule, _ = _cvar_gate_rule(_bars_result(_CALM_CLOSES))
    rule, quote_calls = _rule(_quote(iv=0.60))

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
        {"cvar_gate_rule": cvar_rule},
    )

    assert not outcome.triggered
    assert quote_calls == []


def test_unfetchable_quote_fails_closed():
    cvar_rule, _ = _cvar_gate_rule(_bars_result(_CALM_CLOSES))
    rule, _ = _rule(OptionQuoteResult(ok=False, reason="timed out"))

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1"},
        {"cvar_gate_rule": cvar_rule},
    )

    assert outcome.triggered
    assert "insufficient market data" in outcome.reason.lower()


def test_missing_iv_fails_closed():
    cvar_rule, _ = _cvar_gate_rule(_bars_result(_CALM_CLOSES))
    rule, _ = _rule(OptionQuoteResult(ok=True, quote=OptionQuote(bid=4.15, ask=4.30, iv=None)))

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1"},
        {"cvar_gate_rule": cvar_rule},
    )

    assert outcome.triggered
    assert "insufficient market data" in outcome.reason.lower()


def test_missing_cvar_gate_rule_in_state_fails_closed():
    rule, quote_calls = _rule(_quote(iv=0.60))

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1"},
        {},  # no "cvar_gate_rule" state key at all
    )

    assert outcome.triggered
    assert "insufficient market data" in outcome.reason.lower()
    # The quote is still fetched (cheap, shared-cache lookup) before the
    # cvar_gate_rule precondition is checked -- order doesn't matter for
    # correctness here, just documenting current behavior.
    assert quote_calls == ["AAPL260918P00220000"]


def test_wrong_type_in_state_key_fails_closed():
    # A policy file could reuse this state key for something else -- must
    # not be treated as a usable CVaRGateRule just because the key exists.
    rule, _ = _rule(_quote(iv=0.60))

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1"},
        {"cvar_gate_rule": {"not": "a real rule instance"}},
    )

    assert outcome.triggered
    assert "insufficient market data" in outcome.reason.lower()


def test_bars_fetch_failure_fails_closed():
    cvar_rule, _ = _cvar_gate_rule(BarsResult(ok=False, reason="timed out"))
    rule, _ = _rule(_quote(iv=0.60))

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1"},
        {"cvar_gate_rule": cvar_rule},
    )

    assert outcome.triggered
    assert "insufficient market data" in outcome.reason.lower()


def test_too_few_bars_fails_closed():
    # 2 closes -> 1 return -> not enough for a sample stdev.
    cvar_rule, _ = _cvar_gate_rule(_bars_result([100.0, 101.0]))
    rule, _ = _rule(_quote(iv=0.60))

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1"},
        {"cvar_gate_rule": cvar_rule},
    )

    assert outcome.triggered
    assert "insufficient market data" in outcome.reason.lower()


def test_zero_variance_historical_prices_fails_closed():
    # A perfectly flat close series -> annualized HV of exactly 0.0 -> the
    # ratio is undefined (division by zero), not "infinitely elevated" --
    # treated as can't-assess, same as any other degenerate market-data
    # case in this rule.
    cvar_rule, _ = _cvar_gate_rule(_bars_result([100.0, 100.0, 100.0, 100.0]))
    rule, _ = _rule(_quote(iv=0.60))

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1"},
        {"cvar_gate_rule": cvar_rule},
    )

    assert outcome.triggered
    assert "insufficient market data" in outcome.reason.lower()


def test_default_max_iv_hv_ratio_is_one_point_five():
    # No override in _rule(): confirms the _Params default directly, plus
    # a comfortably-below-ceiling IV (ratio ~1.09) passing under that
    # default -- test_elevated_iv_hard_blocks above already covers a
    # comfortably-above-ceiling case (ratio ~1.64) under the same default.
    cvar_rule, _ = _cvar_gate_rule(_bars_result(_CALM_CLOSES))
    rule, _ = _rule(_quote(iv=0.40))

    outcome = rule.check(
        "place_option_order",
        {"symbol": "AAPL260918P00220000", "side": "buy", "qty": "1"},
        {"cvar_gate_rule": cvar_rule},
    )

    assert rule.cfg.max_iv_hv_ratio == 1.5
    assert not outcome.triggered
