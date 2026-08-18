"""Tests for the drawdown_killswitch rule."""

from firewall.rules.base import RuleConfig
from firewall.rules.drawdown_killswitch import DrawdownKillswitchRule


def _rule(**params) -> DrawdownKillswitchRule:
    config = RuleConfig.model_validate(
        {
            "id": "test-drawdown-killswitch",
            "type": "drawdown_killswitch",
            "severity": "hard",
            "regulation_ref": "SEC Rule 15c3-5(c)(1)(i)",
            **params,
        }
    )
    return DrawdownKillswitchRule(config)


def test_pnl_below_threshold_trips_and_blocks_orders():
    rule = _rule(session_pnl_threshold_usd=-1000)

    outcome = rule.check("place_order", {}, {"session_pnl_usd": -1500})

    assert outcome.triggered


def test_pnl_just_above_threshold_does_not_trip():
    rule = _rule(session_pnl_threshold_usd=-1000)

    outcome = rule.check("place_order", {}, {"session_pnl_usd": -999})

    assert not outcome.triggered


def test_tripped_killswitch_stays_tripped_until_explicit_reset():
    rule = _rule(session_pnl_threshold_usd=-1000)

    rule.check("get_account", {}, {"session_pnl_usd": -2000})  # trips it
    outcome = rule.check("place_order", {}, {"session_pnl_usd": 500})  # pnl recovered
    assert outcome.triggered  # still tripped -- recovery doesn't clear the latch

    rule.reset()
    outcome = rule.check("place_order", {}, {"session_pnl_usd": 500})
    assert not outcome.triggered
