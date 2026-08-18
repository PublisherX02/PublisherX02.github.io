"""Tests for PnLHistory's rolling window behavior."""

from firewall.pnl_history import PnLHistory


def test_since_returns_only_events_within_window():
    history = PnLHistory()
    history.record(timestamp=0, pnl_usd=-100)
    history.record(timestamp=50, pnl_usd=-200)
    history.record(timestamp=100, pnl_usd=-300)

    recent = history.since(now=100, window_seconds=60)

    assert [e.pnl_usd for e in recent] == [-200, -300]


def test_record_returns_the_recorded_event():
    history = PnLHistory()

    event = history.record(timestamp=10, pnl_usd=-50)

    assert event.timestamp == 10
    assert event.pnl_usd == -50
    assert list(history) == [event]
