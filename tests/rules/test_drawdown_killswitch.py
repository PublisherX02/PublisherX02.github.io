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


def _tripped_state(held_qty=10):
    return {
        "session_pnl_usd": -1500,
        "exposure_snapshot": {
            "ok": True,
            "authoritative": True,
            "positions": {"AAPL": held_qty},
        },
    }


def test_tripped_allows_exact_broker_confirmed_long_equity_sell_with_audit_event():
    outcome = _rule(session_pnl_threshold_usd=-1000).check(
        "place_stock_order", {"symbol": "AAPL", "side": "sell", "qty": "10"},
        _tripped_state(),
    )
    assert not outcome.triggered
    assert outcome.state_events == [(
        "info",
        "DELEVERAGING_EXCEPTION_ALLOW: drawdown killswitch is tripped; "
        "plain-equity sell for AAPL is provably exposure-reducing "
        "(broker_confirmed_held_qty=10, order_qty=10)",
    )]


def test_tripped_blocks_sell_that_would_flip_long_short():
    outcome = _rule(session_pnl_threshold_usd=-1000).check(
        "place_stock_order", {"symbol": "AAPL", "side": "sell", "qty": "11"},
        _tripped_state(),
    )
    assert outcome.triggered


def test_tripped_blocks_sell_without_long_holding():
    outcome = _rule(session_pnl_threshold_usd=-1000).check(
        "place_stock_order", {"symbol": "MSFT", "side": "sell", "qty": "1"},
        _tripped_state(),
    )
    assert outcome.triggered


def test_tripped_rule_does_not_exempt_option_sell():
    outcome = _rule(session_pnl_threshold_usd=-1000).check(
        "place_option_order",
        {"symbol": "AAPL260918C00100000", "side": "sell", "qty": "1"},
        _tripped_state(),
    )
    assert outcome.triggered


def test_tripped_still_blocks_buy():
    outcome = _rule(session_pnl_threshold_usd=-1000).check(
        "place_stock_order", {"symbol": "AAPL", "side": "buy", "qty": "1"},
        _tripped_state(),
    )
    assert outcome.triggered


def test_deleveraging_requires_authoritative_snapshot():
    state = _tripped_state()
    state["exposure_snapshot"]["authoritative"] = False
    outcome = _rule(session_pnl_threshold_usd=-1000).check(
        "place_stock_order", {"symbol": "AAPL", "side": "sell", "qty": "1"}, state,
    )
    assert outcome.triggered
