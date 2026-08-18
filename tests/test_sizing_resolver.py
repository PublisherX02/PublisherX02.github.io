"""Tests for sizing_resolver.resolve_size -- the single composer of every
sizing constraint into one final_size + audit-log-ready breakdown.
"""

from __future__ import annotations

import math

import pytest

from firewall.market_data import BarsResult, DailyBar
from firewall.sizing_resolver import resolve_size

# Mirrors policies/default.yaml's real values for the four required rule
# ids, so tests exercise the actual production policy file by default:
#   notional-cap-single-order: max_usd = 5000
#   position-cap-per-symbol:   max_usd_per_symbol = 20000
#   cvar-gate:                 cvar_lookback_days=90, cvar_alpha=0.95,
#                               cvar_max_loss_pct_of_equity=0.02
#   pct-of-adv:                adv_lookback_days=30, max_percent_of_adv=0.01

_FLAT_CLOSE = 100.0
_FLAT_VOLUME = 1_000_000.0


def _flat_bars(n: int, close: float = _FLAT_CLOSE, volume: float = _FLAT_VOLUME) -> BarsResult:
    return BarsResult(ok=True, bars=[DailyBar(close=close, volume=volume) for _ in range(n)])


def _dip_then_flat_bars(n: int, dip_pct: float = 0.10) -> BarsResult:
    """One sharp drop, then flat -- guarantees a real (negative) CVaR per
    unit notional so cvar_max_size comes out finite in tests."""
    bars = [DailyBar(close=_FLAT_CLOSE, volume=_FLAT_VOLUME) for _ in range(n - 1)]
    bars.append(DailyBar(close=_FLAT_CLOSE * (1 - dip_pct), volume=_FLAT_VOLUME))
    return BarsResult(ok=True, bars=bars)


def _fetcher(cvar_result: BarsResult, adv_result: BarsResult):
    def fetch(symbol: str, lookback_days: int) -> BarsResult:
        # policies/default.yaml: cvar-gate uses 90d, pct-of-adv uses 30d.
        return cvar_result if lookback_days == 90 else adv_result

    return fetch


def _account_state(**overrides) -> dict:
    state = {"account_equity": 100_000.0, "positions": {}}
    state.update(overrides)
    return state


def test_proposed_notional_binds_when_smallest():
    fetch = _fetcher(_dip_then_flat_bars(90), _flat_bars(30))
    result = resolve_size("AAPL", 10.0, _account_state(), bars_fetcher=fetch)

    assert result.final_size == 10.0
    assert result.binding == ["proposed_notional"]


def test_notional_cap_binds():
    fetch = _fetcher(_dip_then_flat_bars(90), _flat_bars(30))
    result = resolve_size(
        "AAPL", 1_000_000.0, _account_state(), bars_fetcher=fetch
    )

    assert result.final_size == 5000.0
    assert "notional_cap" in result.binding


def test_position_cap_nets_current_exposure():
    fetch = _fetcher(_dip_then_flat_bars(90), _flat_bars(30))
    # position_cap = 20,000 - 19,000 current exposure = 1,000, the tightest
    # constraint once we set proposed_notional/notional_cap out of the way.
    result = resolve_size(
        "AAPL",
        900.0,
        _account_state(positions={"AAPL": 19_000.0}),
        bars_fetcher=fetch,
    )

    assert result.constraints["position_cap"] == pytest.approx(1000.0)


def test_position_cap_floors_at_zero_when_already_over_cap():
    fetch = _fetcher(_dip_then_flat_bars(90), _flat_bars(30))
    result = resolve_size(
        "AAPL",
        500.0,
        _account_state(positions={"AAPL": 25_000.0}),
        bars_fetcher=fetch,
    )

    assert result.constraints["position_cap"] == 0.0
    assert result.final_size == 0.0
    assert "position_cap" in result.binding


def test_cvar_max_size_fails_closed_on_missing_bars():
    fetch = _fetcher(BarsResult(ok=False, reason="timed out"), _flat_bars(30))
    result = resolve_size("AAPL", 500.0, _account_state(), bars_fetcher=fetch)

    assert result.constraints["cvar_max_size"] == 0.0
    assert result.final_size == 0.0
    assert "cvar_max_size" in result.binding
    assert "timed out" in result.notes["cvar_max_size"]


def test_cvar_max_size_fails_closed_on_missing_equity():
    fetch = _fetcher(_dip_then_flat_bars(90), _flat_bars(30))
    state = _account_state()
    del state["account_equity"]
    result = resolve_size("AAPL", 500.0, state, bars_fetcher=fetch)

    assert result.constraints["cvar_max_size"] == 0.0
    assert "account_equity" in result.notes["cvar_max_size"]


def test_cvar_max_size_unconstrained_when_no_downside_in_sample():
    # Flat prices -> zero daily returns -> CVaR == 0, not negative.
    fetch = _fetcher(_flat_bars(90), _flat_bars(30))
    result = resolve_size("AAPL", 500.0, _account_state(), bars_fetcher=fetch)

    assert math.isinf(result.constraints["cvar_max_size"])
    assert "unconstrained" in result.notes["cvar_max_size"]


def test_pct_of_adv_cap_fails_closed_on_missing_bars():
    fetch = _fetcher(_dip_then_flat_bars(90), BarsResult(ok=False, reason="HTTP 401"))
    result = resolve_size("AAPL", 500.0, _account_state(), bars_fetcher=fetch)

    assert result.constraints["pct_of_adv_cap"] == 0.0
    assert result.final_size == 0.0
    assert "HTTP 401" in result.notes["pct_of_adv_cap"]


def test_pct_of_adv_cap_fails_closed_on_zero_adv():
    fetch = _fetcher(_dip_then_flat_bars(90), _flat_bars(30, volume=0.0))
    result = resolve_size("AAPL", 500.0, _account_state(), bars_fetcher=fetch)

    assert result.constraints["pct_of_adv_cap"] == 0.0
    assert "zero" in result.notes["pct_of_adv_cap"]


def test_pct_of_adv_cap_computed_correctly():
    # ADV = 1,000,000 shares, price = $100 -> cap = 0.01 * 1,000,000 * 100
    # = $1,000,000.
    fetch = _fetcher(_dip_then_flat_bars(90), _flat_bars(30))
    result = resolve_size("AAPL", 500.0, _account_state(), bars_fetcher=fetch)

    assert result.constraints["pct_of_adv_cap"] == pytest.approx(1_000_000.0)


def test_kelly_unconstrained_when_no_forecast_supplied():
    fetch = _fetcher(_dip_then_flat_bars(90), _flat_bars(30))
    result = resolve_size("AAPL", 500.0, _account_state(), bars_fetcher=fetch)

    assert math.isinf(result.constraints["kelly_suggested_size"])
    assert "no forecast supplied" in result.notes["kelly_suggested_size"]
    assert "kelly_suggested_size" not in result.binding


def test_kelly_finite_when_forecast_and_equity_present():
    fetch = _fetcher(_dip_then_flat_bars(90), _flat_bars(30))
    # mu=0.10, sigma_squared=0.04 -> f_star=2.5, f_used=0.25*2.5=0.625,
    # kelly_suggested_size = 0.625 * 100,000 = 62,500.
    result = resolve_size(
        "AAPL",
        500.0,
        _account_state(mu=0.10, sigma_squared=0.04),
        bars_fetcher=fetch,
    )

    assert result.constraints["kelly_suggested_size"] == pytest.approx(62_500.0)


def test_kelly_fails_closed_when_forecast_supplied_but_equity_missing():
    fetch = _fetcher(_dip_then_flat_bars(90), _flat_bars(30))
    state = _account_state(mu=0.10, sigma_squared=0.04)
    del state["account_equity"]
    result = resolve_size("AAPL", 500.0, state, bars_fetcher=fetch)

    assert result.constraints["kelly_suggested_size"] == 0.0
    assert "account_equity" in result.notes["kelly_suggested_size"]


def test_multiple_binding_constraints_are_all_reported():
    fetch = _fetcher(BarsResult(ok=False, reason="down"), BarsResult(ok=False, reason="down"))
    result = resolve_size("AAPL", 500.0, _account_state(), bars_fetcher=fetch)

    assert result.final_size == 0.0
    assert set(result.binding) == {"cvar_max_size", "pct_of_adv_cap"}


def test_reason_string_names_binding_constraint_and_all_values():
    fetch = _fetcher(_dip_then_flat_bars(90), _flat_bars(30))
    result = resolve_size("AAPL", 1_000_000.0, _account_state(), bars_fetcher=fetch)

    assert "notional_cap" in result.reason
    assert "$5,000.00" in result.reason
    assert "binding constraint(s): notional_cap" in result.reason


def test_missing_required_rule_id_raises(tmp_path):
    policy = tmp_path / "incomplete.yaml"
    policy.write_text(
        """
version: "0.0.1"
rules:
  - id: notional-cap-single-order
    type: notional_cap
    severity: hard
    regulation_ref: "SEC Rule 15c3-5(c)(1)(i)"
    max_usd: 5000
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing"):
        resolve_size("AAPL", 500.0, _account_state(), policy_path=policy)


def test_disabled_required_rule_id_raises(tmp_path):
    policy = tmp_path / "disabled.yaml"
    policy.write_text(
        """
version: "0.0.1"
rules:
  - id: notional-cap-single-order
    type: notional_cap
    enabled: false
    severity: hard
    regulation_ref: "SEC Rule 15c3-5(c)(1)(i)"
    max_usd: 5000
  - id: position-cap-per-symbol
    type: position_cap
    severity: hard
    regulation_ref: "SEC Rule 15c3-5(c)(1)(i)"
    max_usd_per_symbol: 20000
  - id: cvar-gate
    type: cvar_gate
    severity: hard
    regulation_ref: null
    cvar_max_loss_pct_of_equity: 0.02
  - id: pct-of-adv
    type: pct_of_adv
    severity: hard
    regulation_ref: null
    max_percent_of_adv: 0.01
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="disabled"):
        resolve_size("AAPL", 500.0, _account_state(), policy_path=policy)


def test_uses_real_default_policy_by_default():
    # No policy_path override -- proves resolve_size reads
    # policies/default.yaml successfully end to end.
    fetch = _fetcher(_dip_then_flat_bars(90), _flat_bars(30))
    result = resolve_size("AAPL", 100.0, _account_state(), bars_fetcher=fetch)

    assert result.constraints["notional_cap"] == 5000.0
    assert result.constraints["position_cap"] == 20000.0
