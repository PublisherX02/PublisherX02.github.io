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
