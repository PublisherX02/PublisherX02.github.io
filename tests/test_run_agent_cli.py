"""Permanent CLI safety and process-exit contract tests."""

import asyncio

import pytest

import run_agent
from firewall.account_data import AccountPnLResult, PositionsResult


class _Lock:
    def __init__(self, cycle_id):
        self.cycle_id = cycle_id

    def acquire(self):
        return None

    def release(self):
        return None


def _isolate_cycle_state(monkeypatch):
    monkeypatch.setattr(run_agent, "load_cycle_state", lambda: None)
    monkeypatch.setattr(run_agent, "write_cycle_state", lambda *a, **k: None)
    monkeypatch.setattr(run_agent, "CycleLock", _Lock)


def test_no_execute_flag_defaults_to_mutation_proof_dry_run(monkeypatch):
    _isolate_cycle_state(monkeypatch)
    observed = {}

    class Runner:
        def __init__(self, **kwargs):
            observed.update(kwargs)

        async def execute_cycle(self):
            return {"ok": True, "reconciliation_status": "verified"}

    monkeypatch.setattr(run_agent, "HumanReadableCycleRunner", Runner)
    code = asyncio.run(run_agent.main_async(["--no-overlay"]))

    assert code == 0
    assert observed["dry_run"] is True


def test_execute_without_expected_account_id_refuses_before_cycle(monkeypatch):
    monkeypatch.delenv("ALPACA_EXPECTED_ACCOUNT_ID", raising=False)
    monkeypatch.setattr(
        run_agent,
        "HumanReadableCycleRunner",
        lambda **kwargs: pytest.fail("runner must not be constructed"),
    )

    assert asyncio.run(run_agent.main_async(["--execute"])) != 0


def test_execute_with_wrong_account_id_returns_nonzero(monkeypatch):
    _isolate_cycle_state(monkeypatch)

    class WrongAccountRunner:
        def __init__(self, **kwargs):
            assert kwargs["dry_run"] is False
            assert kwargs["expected_account_id"] == "expected-paper"

        async def execute_cycle(self):
            return {"ok": False, "reason": "paper account identity mismatch"}

    monkeypatch.setattr(run_agent, "HumanReadableCycleRunner", WrongAccountRunner)
    code = asyncio.run(run_agent.main_async([
        "--execute", "--expected-account-id", "expected-paper", "--no-overlay"
    ]))
    assert code != 0


def test_runner_itself_detects_wrong_broker_account(monkeypatch):
    engine = type("Engine", (), {"audit_writer": None})()
    monkeypatch.setattr(run_agent, "_default_policy_engine", lambda: engine)
    monkeypatch.setattr(run_agent, "build_proxy", lambda **kwargs: object())
    monkeypatch.setattr(run_agent, "LifecycleJournal", lambda: object())
    monkeypatch.setattr(
        run_agent.core_strategy,
        "_read_dynamic_policy_config",
        lambda ignored: {"account_equity": 100_000.0},
    )
    monkeypatch.setattr(
        run_agent.account_data,
        "fetch_session_pnl",
        lambda: AccountPnLResult(
            ok=True, equity=100_000.0, session_pnl_usd=0.0,
            account_id="actual-paper",
        ),
    )

    runner = run_agent.HumanReadableCycleRunner(
        expected_account_id="expected-paper", dry_run=False
    )
    result = asyncio.run(runner.execute_cycle())
    assert result == {"ok": False, "reason": "paper account identity mismatch"}


@pytest.mark.parametrize("reason", [
    "open-order reconciliation failed",
    "post-cycle position outcome is unverified",
])
def test_safety_refusal_results_return_nonzero(monkeypatch, reason):
    _isolate_cycle_state(monkeypatch)

    class RefusingRunner:
        def __init__(self, **kwargs):
            pass

        async def execute_cycle(self):
            return {"ok": False, "reason": reason}

    monkeypatch.setattr(run_agent, "HumanReadableCycleRunner", RefusingRunner)
    assert asyncio.run(run_agent.main_async(["--no-overlay"])) != 0


def test_stale_cycle_refusal_returns_nonzero(monkeypatch):
    monkeypatch.setattr(
        run_agent, "load_cycle_state",
        lambda: {"cycle_id": "stale", "status": "running"},
    )
    monkeypatch.setattr(run_agent, "CycleLock", _Lock)
    assert asyncio.run(run_agent.main_async(["--no-overlay"])) != 0


def test_policy_initialization_failure_returns_nonzero(monkeypatch):
    _isolate_cycle_state(monkeypatch)

    class FailingRunner:
        def __init__(self, **kwargs):
            raise ValueError("invalid policy configuration")

    monkeypatch.setattr(run_agent, "HumanReadableCycleRunner", FailingRunner)
    assert asyncio.run(run_agent.main_async(["--no-overlay"])) != 0


def test_preflight_wrong_account_returns_nonzero(monkeypatch):
    monkeypatch.setattr(
        run_agent.account_data, "fetch_session_pnl",
        lambda **kwargs: AccountPnLResult(ok=True, account_id="actual-paper", equity=1),
    )
    monkeypatch.setattr(
        run_agent.account_data, "fetch_positions",
        lambda **kwargs: PositionsResult(ok=True, positions={}),
    )
    code = asyncio.run(run_agent.main_async([
        "--preflight-only", "--expected-account-id", "expected-paper"
    ]))
    assert code != 0
