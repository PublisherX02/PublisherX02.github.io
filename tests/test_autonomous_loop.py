import pytest

from autonomous_loop import LoopConfig, cycle_command, run_loop
from firewall.account_data import AccountPnLResult, MarketClockResult


def test_loop_defaults_to_dry_run_and_options_off():
    command = cycle_command(LoopConfig(max_cycles=1))
    assert "--dry-run" in command
    assert "--no-overlay" in command


def test_execute_loop_requires_expected_account_identity():
    with pytest.raises(ValueError, match="expected-account-id"):
        run_loop(LoopConfig(dry_run=False, max_cycles=1))


def test_execute_loop_propagates_explicit_execute_and_account_pin():
    command = cycle_command(LoopConfig(
        dry_run=False, max_cycles=1, expected_account_id="competition-paper"
    ))
    assert "--execute" in command
    assert "--dry-run" not in command
    assert command[command.index("--expected-account-id") + 1] == "competition-paper"


def test_loop_waits_for_open_market_then_runs_bounded_cycles(tmp_path):
    clocks = iter([
        MarketClockResult(ok=True, is_open=False),
        MarketClockResult(ok=True, is_open=True),
        MarketClockResult(ok=True, is_open=True),
    ])
    commands = []
    sleeps = []
    completed = run_loop(
        LoopConfig(max_cycles=2, interval_seconds=7, closed_market_poll_seconds=11,
                   heartbeat_file=tmp_path / "heartbeat.json"),
        clock_fetcher=lambda: next(clocks),
        account_fetcher=lambda: AccountPnLResult(ok=True, account_id="paper-test"),
        cycle_runner=lambda command: commands.append(command) or 0,
        sleep=sleeps.append,
    )
    assert completed == 2
    assert len(commands) == 2
    assert sleeps == [11, 7]

    import json
    heartbeat = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))
    assert heartbeat["pid"] > 0
    assert heartbeat["mode"] == "dry_run"
    assert heartbeat["account_id"] == "paper-test"
    assert "last_cycle" in heartbeat
    assert "next_cycle" in heartbeat
    assert "reconciliation_status" in heartbeat
    assert heartbeat["next_cycle"] is None
    assert "stopped_at" in heartbeat


def test_failed_cycle_heartbeat_preserves_real_failure_status(monkeypatch, tmp_path):
    import json
    import autonomous_loop

    monkeypatch.setattr(
        autonomous_loop,
        "load_cycle_state",
        lambda: {
            "cycle_id": "failed-cycle",
            "status": "failed",
            "updated_at": "2026-09-01T15:00:00+00:00",
            "result": {"ok": False, "reason": "open-order reconciliation failed"},
        },
    )
    completed = run_loop(
        LoopConfig(max_cycles=1, heartbeat_file=tmp_path / "heartbeat.json"),
        clock_fetcher=lambda: MarketClockResult(ok=True, is_open=True),
        account_fetcher=lambda: AccountPnLResult(ok=True, account_id="paper-test"),
        cycle_runner=lambda command: 1,
        sleep=lambda seconds: None,
    )

    heartbeat = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))
    assert completed == 1
    assert heartbeat["status"] == "failed"
    assert heartbeat["reconciliation_status"] == "failed"
    assert heartbeat["last_cycle"]["status"] == "failed"
    assert heartbeat["last_cycle"]["exit_code"] == 1
    assert heartbeat["last_cycle"]["failure_reason"] == "open-order reconciliation failed"
