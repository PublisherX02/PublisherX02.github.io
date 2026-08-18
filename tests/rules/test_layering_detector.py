"""Tests for the layering_detector rule."""

from firewall.order_history import OrderHistory
from firewall.rules.base import RuleConfig
from firewall.rules.layering_detector import LayeringDetectorRule


def _rule(**params) -> LayeringDetectorRule:
    config = RuleConfig.model_validate(
        {
            "id": "test-layering",
            "type": "layering_detector",
            "severity": "soft",
            "regulation_ref": "FINRA Rule 5210; 15 U.S.C. 78i(a)(2); 17 CFR 240.10b-5; FINRA Rule 2020",
            "window_seconds": 30,
            "min_distinct_price_levels": 3,
            **params,
        }
    )
    return LayeringDetectorRule(config)


def _history_with_buy_wall(prices: list[float], outcome: str) -> OrderHistory:
    history = OrderHistory()
    for i, price in enumerate(prices):
        history.record(
            timestamp=float(i),
            tool="place_stock_order",
            symbol="AAPL",
            side="buy",
            qty=10,
            price=price,
            order_id=f"buy-{i}",
            outcome=outcome,
        )
    return history


def test_unfilled_wall_followed_by_opposite_side_order_triggers():
    history = _history_with_buy_wall([100.0, 100.5, 101.0], outcome="open")
    rule = _rule()

    outcome = rule.check(
        "place_stock_order",
        {"symbol": "AAPL", "side": "sell", "qty": 30, "limit_price": 101.5},
        {"order_history": history, "now": 5.0},
    )

    assert outcome.triggered
    assert all(f"buy-{i}" in outcome.reason for i in range(3))  # evidence


def test_filled_scale_in_followed_by_exit_does_not_trigger():
    # A trader legitimately scales into a position at three different
    # prices, all of which actually FILL, then exits with a sell. This
    # superficially resembles layering (multi-price, opposite-side
    # follow-up) but the earlier orders were real executions, not a
    # resting decoy wall, so it must not be flagged.
    history = _history_with_buy_wall([100.0, 100.5, 101.0], outcome="filled")
    rule = _rule()

    outcome = rule.check(
        "place_stock_order",
        {"symbol": "AAPL", "side": "sell", "qty": 30, "limit_price": 101.5},
        {"order_history": history, "now": 5.0},
    )

    assert not outcome.triggered
