"""Tests for the place_cancel_ratio rule."""

from firewall.order_history import OrderHistory
from firewall.rules.base import RuleConfig
from firewall.rules.place_cancel_ratio import PlaceCancelRatioRule


def _rule(**params) -> PlaceCancelRatioRule:
    config = RuleConfig.model_validate(
        {
            "id": "test-place-cancel-ratio",
            "type": "place_cancel_ratio",
            "severity": "hard",
            "regulation_ref": None,
            "ratio_threshold": 0.8,
            "window_seconds": 300,
            "min_orders_in_window": 5,
            **params,
        }
    )
    return PlaceCancelRatioRule(config)


def _history_with_outcomes(outcomes: list[str]) -> OrderHistory:
    history = OrderHistory()
    for i, outcome in enumerate(outcomes):
        history.record(
            timestamp=float(i),
            tool="place_stock_order",
            symbol="AAPL",
            side="buy",
            qty=1,
            price=100 + i,
            order_id=f"order-{i}",
            outcome=outcome,
        )
    return history


def test_high_cancel_ratio_with_enough_samples_triggers():
    # 5 placed, all cancelled unfilled -> ratio 1.0, above the 0.8
    # threshold, and 5 >= min_orders_in_window=5.
    history = _history_with_outcomes(["cancelled"] * 5)
    rule = _rule()

    outcome = rule.check(
        "place_stock_order",
        {"symbol": "AAPL", "side": "buy", "qty": 1, "limit_price": 100},
        {"order_history": history, "now": 10.0},
    )

    assert outcome.triggered
    assert "order-0" in outcome.reason  # evidence order_ids present


def test_single_cancelled_order_below_sample_size_does_not_trigger():
    # A trader cancels their one and only order because of a fat-fingered
    # price -- ratio is 100% but the sample size (1) is far below
    # min_orders_in_window=5, so this legitimate correction must not be
    # flagged as spoofing.
    history = _history_with_outcomes(["cancelled"])
    rule = _rule()

    outcome = rule.check(
        "place_stock_order",
        {"symbol": "AAPL", "side": "buy", "qty": 1, "limit_price": 100},
        {"order_history": history, "now": 1.0},
    )

    assert not outcome.triggered
