"""Tests for the PolicyEngine's load-time validation and evaluation order."""

import json
import logging

import pytest

from firewall.audit import AuditLogWriter, verify_chain
from firewall.pnl_history import PnLHistory
from firewall.policy import PolicyEngine, Verdict, Warning
from firewall.rules.base import RuleConfig
from firewall.rules.cooldown_after_loss import CooldownAfterLossRule
from firewall.rules.cvar_gate import CVaRGateRule
from firewall.rules.notional_cap import NotionalCapRule
from firewall.rules.pct_of_adv import PctOfAdvRule


def test_from_yaml_loads_default_policy():
    engine = PolicyEngine.from_yaml("policies/default.yaml")

    assert engine.version
    assert len(engine.rules) == 13


def test_gtc_order_hard_blocked_with_correct_reason_even_without_account_equity():
    """GTC rejection must win even when a later market-data-dependent rule
    (cvar-gate) would also fail closed on missing account_equity -- the
    agent needs the GTC-specific reason to know how to resubmit, not a
    generic fail-closed message from an unrelated rule.
    """
    engine = PolicyEngine.from_yaml("policies/default.yaml")

    verdict = engine.evaluate(
        "place_order",
        {"symbol": "AAPL", "qty": 1, "limit_price": 100, "time_in_force": "gtc"},
        {},
    )

    assert verdict.decision == "hard_block"
    assert verdict.rule_id == "gtc-restriction"
    assert verdict.reason == (
        "GTC orders are restricted by policy; resubmit as day or ioc."
    )


def test_cooldown_after_loss_writes_distinct_entry_and_exit_audit_records(tmp_path):
    """Entering and exiting the cooldown must land as their own audit
    records (verdict state_entered/state_exited) tagged with the rule's
    id, separate from the per-call hard_block records -- that's what lets
    a dashboard render cooldown as one continuous state instead of
    reconstructing it from a stream of individual blocks."""
    log_path = tmp_path / "audit.jsonl"
    writer = AuditLogWriter(log_path, session_id="test-session")

    config = RuleConfig.model_validate(
        {
            "id": "cooldown-after-loss",
            "type": "cooldown_after_loss",
            "severity": "hard",
            "regulation_ref": "SEC Rule 15c3-5(c)(1)(i)",
            "cooldown_loss_threshold": 500,
            "cooldown_loss_window": 300,
            "cooldown_duration_seconds": 900,
        }
    )
    rule = CooldownAfterLossRule(config)
    engine = PolicyEngine(rules=[rule], version="test", audit_writer=writer)

    history = PnLHistory()
    history.record(timestamp=0, pnl_usd=-600)

    entering = engine.evaluate(
        "place_order", {"symbol": "AAPL"}, {"now": 100, "pnl_history": history}
    )
    still_cooling = engine.evaluate(
        "place_order", {"symbol": "AAPL"}, {"now": 200, "pnl_history": history}
    )
    exiting = engine.evaluate(
        "place_order", {"symbol": "AAPL"}, {"now": 1001, "pnl_history": history}
    )

    assert entering.decision == "hard_block"
    assert entering.rule_id == "cooldown-after-loss"
    assert still_cooling.decision == "hard_block"
    assert exiting.decision == "allow"

    # PolicyEngine.evaluate() now audit-logs ordinary hard_block verdicts
    # itself (see test_ordinary_hard_block_writes_a_single_audit_record),
    # so each of the two hard_block calls ("entering", "still_cooling")
    # gets its own hard_block record in addition to the state_events
    # side-channel. "exiting" resolves to "allow", which evaluate() does
    # NOT log itself (forwarded/upstream_status aren't known until the
    # caller actually forwards the call -- see record_call_outcome), so it
    # contributes only its state_exited transition record here.
    records = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    verdicts = [r["verdict"] for r in records]
    assert verdicts == ["state_entered", "hard_block", "hard_block", "state_exited"]

    entered_record = next(r for r in records if r["verdict"] == "state_entered")
    assert entered_record["rule_id"] == "cooldown-after-loss"

    exited_record = next(r for r in records if r["verdict"] == "state_exited")
    assert exited_record["rule_id"] == "cooldown-after-loss"

    ok, bad_index = verify_chain(log_path)
    assert ok is True
    assert bad_index is None


# --- ordinary (non-exception) verdicts are also audited -------------------


def test_ordinary_hard_block_writes_a_single_audit_record(tmp_path):
    """A hard_block from a normal (non-flaky) rule is never forwarded, so
    evaluate() can and must write its audit record itself -- there's no
    caller round-trip to wait for, unlike the allow/soft_block case (see
    record_call_outcome below)."""
    log_path = tmp_path / "audit.jsonl"
    writer = AuditLogWriter(log_path, session_id="test-session")
    config = RuleConfig.model_validate(
        {
            "id": "cap-a",
            "type": "notional_cap",
            "severity": "hard",
            "regulation_ref": "SEC Rule 15c3-5(c)(1)(i)",
            "max_usd": 100,
        }
    )
    engine = PolicyEngine(rules=[NotionalCapRule(config)], version="test", audit_writer=writer)

    verdict = engine.evaluate(
        "place_order", {"symbol": "TSLA", "qty": 10, "limit_price": 50}, {}
    )

    assert verdict.decision == "hard_block"

    records = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    record = records[0]
    assert record["verdict"] == "hard_block"
    assert record["rule_id"] == "cap-a"
    assert record["regulation_ref"] == "SEC Rule 15c3-5(c)(1)(i)"
    assert record["reason"] == verdict.reason
    assert record["forwarded"] is False
    assert record["upstream_status"] == "not_forwarded"

    ok, bad_index = verify_chain(log_path)
    assert ok is True
    assert bad_index is None


def test_record_call_outcome_writes_allow_record_with_caller_supplied_status(tmp_path):
    """record_call_outcome is how a caller (the proxy) reports the outcome
    of a call evaluate() allowed, once forwarding has actually been
    attempted -- forwarded/upstream_status aren't knowable inside
    evaluate() itself."""
    log_path = tmp_path / "audit.jsonl"
    writer = AuditLogWriter(log_path, session_id="test-session")
    engine = PolicyEngine(rules=[], version="test", audit_writer=writer)

    verdict = engine.evaluate("get_account", {}, {})
    assert verdict.decision == "allow"

    engine.record_call_outcome(
        "get_account", {}, verdict, forwarded=True, upstream_status="ok"
    )

    records = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    record = records[0]
    assert record["verdict"] == "allow"
    assert record["rule_id"] is None
    assert record["forwarded"] is True
    assert record["upstream_status"] == "ok"


def test_record_call_outcome_writes_soft_block_record_for_warnings(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    writer = AuditLogWriter(log_path, session_id="test-session")
    engine = PolicyEngine(rules=[], version="test", audit_writer=writer)

    verdict = Verdict(
        decision="allow",
        warnings=[
            Warning(rule_id="cap-soft", regulation_ref="SEC Rule 15c3-5(c)(1)(i)", reason="over cap"),
        ],
    )

    engine.record_call_outcome(
        "place_order", {"qty": 10}, verdict, forwarded=True, upstream_status="ok"
    )

    records = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    record = records[0]
    assert record["verdict"] == "soft_block"
    assert record["rule_id"] == "cap-soft"
    assert record["regulation_ref"] == "SEC Rule 15c3-5(c)(1)(i)"
    assert "over cap" in record["reason"]
    assert record["forwarded"] is True
    assert record["upstream_status"] == "ok"


def test_record_call_outcome_does_not_drop_a_second_warning(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    writer = AuditLogWriter(log_path, session_id="test-session")
    engine = PolicyEngine(rules=[], version="test", audit_writer=writer)

    verdict = Verdict(
        decision="allow",
        warnings=[
            Warning(rule_id="cap-soft", regulation_ref=None, reason="over cap"),
            Warning(rule_id="wash-soft", regulation_ref=None, reason="looks like a wash trade"),
        ],
    )

    engine.record_call_outcome(
        "place_order", {"qty": 10}, verdict, forwarded=True, upstream_status="ok"
    )

    records = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    assert "over cap" in records[0]["reason"]
    assert "looks like a wash trade" in records[0]["reason"]


def test_record_call_outcome_is_a_noop_for_hard_block_verdicts(tmp_path):
    """A hard_block was already logged by evaluate() itself; calling
    record_call_outcome again for the same verdict must not double-log
    it -- the caller shouldn't have to remember to special-case this."""
    log_path = tmp_path / "audit.jsonl"
    writer = AuditLogWriter(log_path, session_id="test-session")
    config = RuleConfig.model_validate(
        {
            "id": "cap-a",
            "type": "notional_cap",
            "severity": "hard",
            "regulation_ref": "SEC Rule 15c3-5(c)(1)(i)",
            "max_usd": 100,
        }
    )
    engine = PolicyEngine(rules=[NotionalCapRule(config)], version="test", audit_writer=writer)

    verdict = engine.evaluate(
        "place_order", {"symbol": "TSLA", "qty": 10, "limit_price": 50}, {}
    )
    assert verdict.decision == "hard_block"

    engine.record_call_outcome(
        "place_order",
        {"symbol": "TSLA", "qty": 10, "limit_price": 50},
        verdict,
        forwarded=False,
        upstream_status="not_forwarded",
    )

    records = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1  # not 2


def test_record_call_outcome_is_a_noop_without_an_audit_writer():
    engine = PolicyEngine(rules=[], version="test")
    verdict = engine.evaluate("get_account", {}, {})

    # Must not raise even though there's no audit_writer to append to.
    engine.record_call_outcome(
        "get_account", {}, verdict, forwarded=True, upstream_status="ok"
    )


def test_unknown_rule_type_raises_at_load_time(tmp_path):
    bad_policy = tmp_path / "bad.yaml"
    bad_policy.write_text(
        """
version: "0.0.1"
rules:
  - id: bogus
    type: does_not_exist
    severity: hard
    regulation_ref: "N/A"
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        PolicyEngine.from_yaml(bad_policy)


def test_first_hard_block_short_circuits(tmp_path):
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        """
version: "0.0.1"
rules:
  - id: cap-a
    type: notional_cap
    severity: hard
    regulation_ref: "SEC Rule 15c3-5(c)(1)(i)"
    max_usd: 100
  - id: allowlist-b
    type: symbol_allowlist
    severity: hard
    regulation_ref: "SEC Rule 15c3-5(c)(2)(ii)"
    allowed_symbols: ["AAPL"]
""",
        encoding="utf-8",
    )
    engine = PolicyEngine.from_yaml(policy)

    # Violates both rules; the first one in file order must win.
    verdict = engine.evaluate(
        "place_order", {"symbol": "TSLA", "qty": 10, "limit_price": 50}, {}
    )

    assert verdict.decision == "hard_block"
    assert verdict.rule_id == "cap-a"


def test_soft_blocks_are_collected_as_warnings_alongside_allow(tmp_path):
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        """
version: "0.0.1"
rules:
  - id: cap-soft
    type: notional_cap
    severity: soft
    regulation_ref: "SEC Rule 15c3-5(c)(1)(i)"
    max_usd: 100
""",
        encoding="utf-8",
    )
    engine = PolicyEngine.from_yaml(policy)

    verdict = engine.evaluate("place_order", {"qty": 10, "limit_price": 50}, {})

    assert verdict.decision == "allow"
    assert len(verdict.warnings) == 1
    assert verdict.warnings[0].rule_id == "cap-soft"


# --- fail-closed on unexpected exceptions from rule.check() ---------------


def test_rule_exception_does_not_propagate_and_fails_closed(monkeypatch):
    config = RuleConfig.model_validate(
        {
            "id": "flaky-rule",
            "type": "notional_cap",
            "severity": "hard",
            "regulation_ref": "SEC Rule 15c3-5(c)(1)(i)",
            "max_usd": 1000,
        }
    )
    rule = NotionalCapRule(config)

    def _raise(*args, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(rule, "check", _raise)
    engine = PolicyEngine(rules=[rule], version="test")

    verdict = engine.evaluate("place_order", {"qty": 1, "limit_price": 1}, {})

    assert verdict.decision == "hard_block"
    assert verdict.rule_id == "flaky-rule"
    assert verdict.reason == "rule evaluation error — failing closed: flaky-rule"


def test_cvar_gate_exception_does_not_propagate_and_fails_closed(monkeypatch):
    config = RuleConfig.model_validate(
        {
            "id": "cvar-flaky",
            "type": "cvar_gate",
            "severity": "hard",
            "regulation_ref": None,
            "cvar_max_loss_pct_of_equity": 0.02,
        }
    )
    rule = CVaRGateRule(config)

    def _raise(*args, **kwargs):
        raise RuntimeError("simulated bug in bars fetcher")

    monkeypatch.setattr(rule, "_bars_fetcher", _raise)
    engine = PolicyEngine(rules=[rule], version="test")

    verdict = engine.evaluate(
        "place_order",
        {"symbol": "AAPL", "qty": 10, "limit_price": 100},
        {"account_equity": 10_000.0},
    )

    assert verdict.decision == "hard_block"
    assert verdict.rule_id == "cvar-flaky"
    assert verdict.reason == "rule evaluation error — failing closed: cvar-flaky"


def test_pct_of_adv_exception_does_not_propagate_and_fails_closed(monkeypatch):
    config = RuleConfig.model_validate(
        {
            "id": "adv-flaky",
            "type": "pct_of_adv",
            "severity": "hard",
            "regulation_ref": None,
            "max_percent_of_adv": 0.1,
        }
    )
    rule = PctOfAdvRule(config)

    def _raise(*args, **kwargs):
        raise RuntimeError("simulated bug in bars fetcher")

    monkeypatch.setattr(rule, "_bars_fetcher", _raise)
    engine = PolicyEngine(rules=[rule], version="test")

    verdict = engine.evaluate(
        "place_order", {"symbol": "AAPL", "qty": 10, "limit_price": 100.0}, {}
    )

    assert verdict.decision == "hard_block"
    assert verdict.rule_id == "adv-flaky"
    assert verdict.reason == "rule evaluation error — failing closed: adv-flaky"


def test_exception_in_second_rule_still_fails_closed_after_earlier_allow(monkeypatch):
    # An exception must fail closed even when earlier rules in the same
    # evaluation didn't block -- the exception, not the prior allows, must
    # decide the outcome.
    passthrough_config = RuleConfig.model_validate(
        {
            "id": "cap-a",
            "type": "notional_cap",
            "severity": "hard",
            "regulation_ref": "SEC Rule 15c3-5(c)(1)(i)",
            "max_usd": 1_000_000,
        }
    )
    flaky_config = RuleConfig.model_validate(
        {
            "id": "flaky-rule",
            "type": "notional_cap",
            "severity": "hard",
            "regulation_ref": "SEC Rule 15c3-5(c)(1)(i)",
            "max_usd": 1_000_000,
        }
    )
    flaky_rule = NotionalCapRule(flaky_config)
    monkeypatch.setattr(
        flaky_rule, "check", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom"))
    )
    engine = PolicyEngine(
        rules=[NotionalCapRule(passthrough_config), flaky_rule], version="test"
    )

    verdict = engine.evaluate("place_order", {"qty": 1, "limit_price": 1}, {})

    assert verdict.decision == "hard_block"
    assert verdict.rule_id == "flaky-rule"


# --- exception handling writes a real audit record, not just app logs ----


def test_rule_exception_logs_and_writes_a_verifiable_audit_record(
    tmp_path, monkeypatch, caplog
):
    log_path = tmp_path / "audit.jsonl"
    writer = AuditLogWriter(log_path, session_id="test-session")

    # A normal record first, so the chain has more than one link -- a
    # single-record file verifies trivially from genesis and wouldn't prove
    # the exception-path record actually chained onto what came before it.
    writer.append(
        tool_name="get_account",
        arguments={},
        verdict="allow",
        reason="no rules triggered",
        forwarded=True,
        upstream_status="ok",
    )

    config = RuleConfig.model_validate(
        {
            "id": "flaky-rule",
            "type": "notional_cap",
            "severity": "hard",
            "regulation_ref": "SEC Rule 15c3-5(c)(1)(i)",
            "max_usd": 1000,
        }
    )
    rule = NotionalCapRule(config)

    def _raise(*args, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(rule, "check", _raise)
    engine = PolicyEngine(rules=[rule], version="test", audit_writer=writer)

    with caplog.at_level(logging.ERROR, logger="firewall.policy"):
        verdict = engine.evaluate("place_order", {"qty": 1, "limit_price": 1}, {})

    assert verdict.decision == "hard_block"
    assert verdict.rule_id == "flaky-rule"

    # The full traceback lands in application logs...
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.ERROR
    assert caplog.records[0].exc_info is not None

    # ...and a concise, separate record lands in the real audit chain.
    import json

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    last_record = json.loads(lines[-1])
    assert last_record["verdict"] == "hard_block"
    assert last_record["rule_id"] == "flaky-rule"
    assert last_record["regulation_ref"] is None
    assert last_record["forwarded"] is False
    assert last_record["reason"] == "rule evaluation error — failing closed: boom"
    assert "Traceback" not in last_record["reason"]

    ok, bad_index = verify_chain(log_path)
    assert ok is True
    assert bad_index is None
