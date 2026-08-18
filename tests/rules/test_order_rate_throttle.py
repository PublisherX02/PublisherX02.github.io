"""Tests for the order_rate_throttle rule."""

from firewall.order_history import OrderHistory
from firewall.rules.base import RuleConfig
from firewall.rules.order_rate_throttle import OrderRateThrottleRule


def _rule(**params) -> OrderRateThrottleRule:
    config = RuleConfig.model_validate(
        {
            "id": "test-throttle",
            "type": "order_rate_throttle",
            "severity": "hard",
            "regulation_ref": "SEC Rule 15c3-5(c)(1)(ii)",
            "max_orders": 5,
            "window_seconds": 60,
            **params,
        }
    )
    return OrderRateThrottleRule(config)


def _history_with_orders(count: int, spacing: float) -> OrderHistory:
    history = OrderHistory()
    for i in range(count):
        history.record(
            timestamp=i * spacing,
            tool="place_stock_order",
            symbol="AAPL",
            side="buy",
            qty=1,
            price=100,
            order_id=f"order-{i}",
            outcome="open",
        )
    return history


def test_breaching_the_rate_triggers_and_latches_paused_until_reset():
    rule = _rule()
    # 5 orders already placed within the last 60s; this 6th call would be
    # the 6th order in the window -- breaches max_orders=5.
    history = _history_with_orders(5, spacing=1.0)
    outcome = rule.check("place_stock_order", {}, {"order_history": history, "now": 10.0})

    assert outcome.triggered

    # Stays paused even for a call well outside the original window, with
    # an empty history -- the pause itself is what's blocking, not the count.
    later_outcome = rule.check(
        "place_stock_order", {}, {"order_history": OrderHistory(), "now": 10_000.0}
    )
    assert later_outcome.triggered

    rule.reset()
    reset_outcome = rule.check(
        "place_stock_order", {}, {"order_history": OrderHistory(), "now": 10_001.0}
    )
    assert not reset_outcome.triggered


def test_orders_spread_beyond_the_window_do_not_trigger():
    rule = _rule()
    # 5 orders total (t=0,60,120,180,240), but spread one per minute --
    # with a 60s window and now=245, only the order at t=240 is "recent".
    # High overall volume, but the rolling rate itself is fine.
    history = _history_with_orders(5, spacing=60.0)

    outcome = rule.check("place_stock_order", {}, {"order_history": history, "now": 245.0})

    assert not outcome.triggered
