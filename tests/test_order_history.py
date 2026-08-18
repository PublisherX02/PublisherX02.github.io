"""Tests for OrderHistory's rolling window behavior."""

from firewall.order_history import OrderHistory


def _record(history: OrderHistory, timestamp: float, order_id: str, outcome: str = "open") -> None:
    history.record(
        timestamp=timestamp,
        tool="place_order",
        symbol="AAPL",
        side="buy",
        qty=1,
        price=100,
        order_id=order_id,
        outcome=outcome,
    )


def test_since_returns_only_events_within_window():
    history = OrderHistory()
    _record(history, 0, "a")
    _record(history, 50, "b")
    _record(history, 100, "c")

    recent = history.since(now=100, window_seconds=60)

    assert {e.order_id for e in recent} == {"b", "c"}


def test_update_outcome_mutates_the_matching_event_in_place():
    history = OrderHistory()
    _record(history, 0, "a", outcome="open")

    history.update_outcome("a", "cancelled")

    assert list(history)[0].outcome == "cancelled"
