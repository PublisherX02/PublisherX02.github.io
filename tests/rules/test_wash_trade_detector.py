"""Tests for the wash_trade_detector rule."""

from firewall.order_history import OrderHistory
from firewall.rules.base import RuleConfig
from firewall.rules.wash_trade_detector import WashTradeDetectorRule


def _rule(**params) -> WashTradeDetectorRule:
    config = RuleConfig.model_validate(
        {
            "id": "test-wash-trade",
            "type": "wash_trade_detector",
            "severity": "soft",
            "regulation_ref": "FINRA Rule 5210.02",
            "window_seconds": 300,
            "qty_tolerance": 0.01,
            **params,
        }
    )
    return WashTradeDetectorRule(config)


def _history_with_filled_buy(qty: float) -> OrderHistory:
    history = OrderHistory()
    history.record(
        timestamp=0.0,
        tool="place_stock_order",
        symbol="AAPL",
        side="buy",
        qty=qty,
        price=100.0,
        order_id="buy-1",
        outcome="filled",
    )
    return history


def test_offsetting_sell_of_matching_size_triggers():
    history = _history_with_filled_buy(qty=100)
    rule = _rule()

    outcome = rule.check(
        "place_stock_order",
        {"symbol": "AAPL", "side": "sell", "qty": 100},
        {"order_history": history, "now": 30.0},
    )

    assert outcome.triggered
    assert "buy-1" in outcome.reason  # evidence


def test_sell_of_meaningfully_different_size_does_not_trigger():
    # A trader trims a small part of a filled position -- same symbol,
    # opposite side, within the window, but the size is nowhere near a
    # full offset, so this must not be flagged as a wash trade.
    history = _history_with_filled_buy(qty=100)
    rule = _rule()

    outcome = rule.check(
        "place_stock_order",
        {"symbol": "AAPL", "side": "sell", "qty": 10},
        {"order_history": history, "now": 30.0},
    )

    assert not outcome.triggered
