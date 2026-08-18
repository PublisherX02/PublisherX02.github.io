"""Tests for the cooldown_after_loss rule."""

from firewall.pnl_history import PnLHistory
from firewall.rules.base import RuleConfig
from firewall.rules.cooldown_after_loss import CooldownAfterLossRule


def _rule(**params) -> CooldownAfterLossRule:
    config = RuleConfig.model_validate(
        {
            "id": "test-cooldown-after-loss",
            "type": "cooldown_after_loss",
            "severity": "hard",
            "regulation_ref": None,
            "cooldown_loss_threshold": 500,
            **params,
        }
    )
    return CooldownAfterLossRule(config)


def _history_with_loss(pnl_usd: float, timestamp: float = 0) -> PnLHistory:
    history = PnLHistory()
    history.record(timestamp=timestamp, pnl_usd=pnl_usd)
    return history


def test_no_cooldown_when_history_absent():
    rule = _rule()

    outcome = rule.check("place_order", {}, {"now": 100})

    assert not outcome.triggered


def test_no_cooldown_when_loss_within_window_is_below_threshold():
    rule = _rule(cooldown_loss_window=300)
    history = _history_with_loss(-499, timestamp=0)

    outcome = rule.check("place_order", {}, {"now": 100, "pnl_history": history})

    assert not outcome.triggered


def test_cooldown_triggers_when_loss_within_window_exceeds_threshold():
    rule = _rule(cooldown_loss_window=300)
    history = _history_with_loss(-501, timestamp=0)

    outcome = rule.check("place_order", {}, {"now": 100, "pnl_history": history})

    assert outcome.triggered


def test_gains_offset_losses_in_the_net_window_total():
    rule = _rule(cooldown_loss_window=300)
    history = PnLHistory()
    history.record(timestamp=0, pnl_usd=-600)
    history.record(timestamp=10, pnl_usd=200)

    outcome = rule.check("place_order", {}, {"now": 100, "pnl_history": history})

    # net loss is -400, below the 500 threshold
    assert not outcome.triggered


def test_loss_outside_window_does_not_count():
    rule = _rule(cooldown_loss_window=300)
    history = _history_with_loss(-1000, timestamp=0)

    outcome = rule.check("place_order", {}, {"now": 1000, "pnl_history": history})

    assert not outcome.triggered


def test_cooldown_blocks_order_placement_once_tripped():
    rule = _rule(cooldown_loss_window=300, cooldown_duration_seconds=900)
    history = _history_with_loss(-501, timestamp=0)

    rule.check("place_order", {}, {"now": 100, "pnl_history": history})
    outcome = rule.check("place_order", {}, {"now": 200, "pnl_history": history})

    assert outcome.triggered


def test_cooldown_blocks_calls_that_did_not_themselves_trigger_it():
    """Realized loss is driven by prior fills, not the fields on the call
    being evaluated right now -- once tripped, ANY order-placement call is
    blocked regardless of what its own arguments look like."""
    rule = _rule(cooldown_loss_window=300, cooldown_duration_seconds=900)
    history = _history_with_loss(-501, timestamp=0)
    rule.check("get_account", {}, {"now": 100, "pnl_history": history})

    outcome = rule.check("place_order", {"symbol": "AAPL"}, {"now": 150, "pnl_history": history})

    assert outcome.triggered


def test_non_order_calls_are_not_blocked_even_during_cooldown():
    rule = _rule(cooldown_loss_window=300, cooldown_duration_seconds=900)
    history = _history_with_loss(-501, timestamp=0)
    rule.check("place_order", {}, {"now": 100, "pnl_history": history})

    outcome = rule.check("get_quote", {}, {"now": 200, "pnl_history": history})

    assert not outcome.triggered


def test_cooldown_expires_after_duration_elapses():
    rule = _rule(cooldown_loss_window=300, cooldown_duration_seconds=900)
    history = _history_with_loss(-501, timestamp=0)
    rule.check("place_order", {}, {"now": 100, "pnl_history": history})

    # cooldown entered at t=100, lasts 900s -> still active at 999
    still_active = rule.check("place_order", {}, {"now": 999, "pnl_history": history})
    # but expired by 1001
    expired = rule.check("place_order", {}, {"now": 1001, "pnl_history": history})

    assert still_active.triggered
    assert not expired.triggered


def test_entering_cooldown_emits_a_state_entered_event():
    rule = _rule(cooldown_loss_window=300)
    history = _history_with_loss(-501, timestamp=0)

    outcome = rule.check("place_order", {}, {"now": 100, "pnl_history": history})

    assert len(outcome.state_events) == 1
    assert outcome.state_events[0][0] == "state_entered"


def test_no_state_event_on_a_call_that_stays_in_steady_cooldown():
    rule = _rule(cooldown_loss_window=300, cooldown_duration_seconds=900)
    history = _history_with_loss(-501, timestamp=0)
    rule.check("place_order", {}, {"now": 100, "pnl_history": history})

    outcome = rule.check("place_order", {}, {"now": 200, "pnl_history": history})

    assert outcome.state_events == []


def test_exiting_cooldown_emits_a_state_exited_event():
    rule = _rule(cooldown_loss_window=300, cooldown_duration_seconds=900)
    history = _history_with_loss(-501, timestamp=0)
    rule.check("place_order", {}, {"now": 100, "pnl_history": history})

    outcome = rule.check("place_order", {}, {"now": 1001, "pnl_history": history})

    assert outcome.state_events[0][0] == "state_exited"


def test_reset_clears_the_cooldown():
    rule = _rule(cooldown_loss_window=300, cooldown_duration_seconds=900)
    history = _history_with_loss(-501, timestamp=0)
    rule.check("place_order", {}, {"now": 100, "pnl_history": history})

    rule.reset()
    # now=500 is past the 300s loss window relative to the t=0 loss event,
    # so nothing re-triggers the cooldown on the very next check.
    outcome = rule.check("place_order", {}, {"now": 500, "pnl_history": history})

    assert not outcome.triggered


def test_reset_emits_a_paired_state_exited_on_the_next_check():
    """reset() can't write an audit record itself -- it has no `state`/`now`
    to attach one to -- so it must defer the closing state_exited to the
    next check() call rather than leaving the entered span unclosed."""
    rule = _rule(cooldown_loss_window=300, cooldown_duration_seconds=900)
    history = _history_with_loss(-501, timestamp=0)
    rule.check("place_order", {}, {"now": 100, "pnl_history": history})

    rule.reset()
    outcome = rule.check("place_order", {}, {"now": 500, "pnl_history": history})

    assert outcome.state_events == [("state_exited", "cooldown ended: manually reset")]


def test_reset_then_immediate_retrigger_still_pairs_exit_before_new_entry():
    """If the loss that tripped the cooldown is still inside the window
    when reset() happens, the very next check() both closes the old span
    and opens a new one -- both events must be reported, in that order, so
    the audit stream never shows two state_entered in a row."""
    rule = _rule(cooldown_loss_window=300, cooldown_duration_seconds=900)
    history = _history_with_loss(-501, timestamp=0)
    rule.check("place_order", {}, {"now": 100, "pnl_history": history})

    rule.reset()
    # now=150: still within the 300s window of the t=0 loss, so the same
    # loss data re-trips the cooldown immediately.
    outcome = rule.check("place_order", {}, {"now": 150, "pnl_history": history})

    assert [event[0] for event in outcome.state_events] == [
        "state_exited",
        "state_entered",
    ]
    assert outcome.triggered
