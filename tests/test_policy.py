"""Tests for the PolicyEngine's load-time validation and evaluation order."""

import json
import logging
from unittest.mock import patch

import pytest

from firewall.audit import AuditLogWriter, compute_record_hash, verify_chain
from firewall.market_data import BarsResult, DailyBar, OptionQuote, OptionQuoteResult
from firewall.order_history import OrderHistory
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
    assert len(engine.rules) == 21


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


def test_near_dated_single_leg_option_order_hits_expiry_floor_not_allowlist():
    """A single-leg place_option_order's `symbol` is always OCC-format,
    which never matches symbol-allowlist's plain-ticker entries -- every
    single-leg option order hard-blocks there regardless of expiry (see
    AUDIT.md section H2). This test does NOT prove option-expiry-floor
    changes the allow/block OUTCOME for single-leg orders -- it doesn't;
    symbol-allowlist would have blocked this call either way. It proves
    which RULE_ID and REASON get reported: option-expiry-floor must run
    FIRST so a near-dated order gets its own specific, actionable reason
    ("pick a later expiry") instead of symbol-allowlist's generic
    rejection -- verified live before choosing this rule ordering, not
    assumed (see default.yaml's own comment on option-expiry-floor's
    placement, and the multi-leg test below for the one case where this
    ordering DOES change the outcome, not just the reason)."""
    engine = PolicyEngine.from_yaml("policies/default.yaml")

    now = 1785542400.0  # 2026-08-01T00:00:00Z, fixed for determinism
    verdict = engine.evaluate(
        "place_option_order",
        # AAPL is on the default allowlist, but as an OCC string it would
        # never match -- 2026-08-04 is 3 calendar days from `now`, inside
        # the default 7-day floor.
        {"symbol": "AAPL260804P00220000", "side": "buy", "qty": "1"},
        {"now": now},
    )

    assert verdict.decision == "hard_block"
    assert verdict.rule_id == "option-expiry-floor"
    assert "contract expiration within 7 days" in verdict.reason


def test_audit_scenario_priceless_multi_leg_order_hard_blocks():
    """Reproduce audit scenario (3): multi-leg option order with no parent
    limit_price must hard-block upfront instead of allowing unassessed risk."""
    engine = PolicyEngine.from_yaml("policies/default.yaml")
    verdict = engine.evaluate(
        "place_option_order",
        {
            "qty": "10",
            "order_class": "mleg",
            "legs": [
                {"symbol": "AAPL991218C00150000", "ratio_qty": "1", "side": "buy"},
                {"symbol": "AAPL991218C00160000", "ratio_qty": "1", "side": "buy"},
            ],
        },
        {},
    )
    assert verdict.decision == "hard_block"
    assert verdict.rule_id == "unsupported-order-shape"
    assert verdict.reason == (
        "bracket/OCO/multi-leg order shapes are not yet risk-assessed "
        "by this firewall and are blocked until support exists."
    )


def test_wide_spread_single_leg_market_order_hits_spread_guard_not_allowlist():
    """Same reachability story as option-expiry-floor above, same scope
    of claim: symbol-allowlist hard-blocks every single-leg option order
    unconditionally (OCC symbol never matches a plain ticker), so
    option-spread-guard must run FIRST to ever execute at all for this
    shape -- verified live before choosing this placement (see
    default.yaml's own comment on option-spread-guard's placement). This
    does NOT change the allow/block outcome for single-leg orders
    (already blocked either way by symbol-allowlist); it changes which
    rule reports, and proves the rule's market-data path actually runs
    through the real policy, not just in isolation."""
    engine = PolicyEngine.from_yaml("policies/default.yaml")

    with patch("firewall.rules.option_spread_guard.fetch_option_latest_quote") as fake:
        fake.return_value = OptionQuoteResult(ok=True, quote=OptionQuote(bid=0.80, ask=1.00))
        verdict = engine.evaluate(
            "place_option_order",
            # AAPL is on the default allowlist, but as an OCC string it
            # would never match -- expiry is far out (2099) so
            # option-expiry-floor (which runs first) doesn't intercept it.
            {
                "symbol": "AAPL991231P00220000",
                "side": "buy",
                "qty": "1",
                "type": "market",
            },
            {},
        )

    assert verdict.decision == "hard_block"
    assert verdict.rule_id == "option-spread-guard"
    assert "relative spread 20.0% exceeds maximum 15.0%" in verdict.reason
    assert fake.called


def test_audit_scenario_stock_bracket_order_hard_blocks():
    """Reproduce audit scenario (1): stock bracket order with take_profit
    and stop_loss parameters must hard-block upfront."""
    engine = PolicyEngine.from_yaml("policies/default.yaml")
    verdict = engine.evaluate(
        "place_stock_order",
        {
            "symbol": "AAPL",
            "qty": "10",
            "limit_price": "50.00",
            "order_class": "bracket",
            "take_profit": {"limit_price": "9999999.00"},
            "stop_loss": {"stop_price": "0.01"},
        },
        {},
    )
    assert verdict.decision == "hard_block"
    assert verdict.rule_id == "unsupported-order-shape"
    assert verdict.reason == (
        "bracket/OCO/multi-leg order shapes are not yet risk-assessed "
        "by this firewall and are blocked until support exists."
    )


def test_audit_scenario_oco_order_hard_blocks():
    """Reproduce audit scenario (2): OCO order shape must hard-block upfront."""
    engine = PolicyEngine.from_yaml("policies/default.yaml")
    verdict = engine.evaluate(
        "place_stock_order",
        {
            "symbol": "AAPL",
            "qty": "10",
            "limit_price": "50.00",
            "order_class": "oco",
        },
        {},
    )
    assert verdict.decision == "hard_block"
    assert verdict.rule_id == "unsupported-order-shape"
    assert verdict.reason == (
        "bracket/OCO/multi-leg order shapes are not yet risk-assessed "
        "by this firewall and are blocked until support exists."
    )


def test_oto_order_hard_blocks():
    """OTO order shape must hard-block upfront."""
    engine = PolicyEngine.from_yaml("policies/default.yaml")
    verdict = engine.evaluate(
        "place_stock_order",
        {
            "symbol": "AAPL",
            "qty": "10",
            "limit_price": "50.00",
            "order_class": "oto",
        },
        {},
    )
    assert verdict.decision == "hard_block"
    assert verdict.rule_id == "unsupported-order-shape"
    assert verdict.reason == (
        "bracket/OCO/multi-leg order shapes are not yet risk-assessed "
        "by this firewall and are blocked until support exists."
    )


def test_option_sell_order_hits_sell_guard_regardless_of_origin():
    """Proves option-sell-guard is a live, registered RULE_TYPES entry
    evaluated by PolicyEngine.evaluate() for a bare place_option_order
    call with no hedge-trigger involved anywhere -- not validation embedded
    inside hedge_proposal.py's own proposal step (which has no validation
    logic of its own at all)."""
    engine = PolicyEngine.from_yaml("policies/default.yaml")

    verdict = engine.evaluate(
        "place_option_order",
        {"symbol": "AAPL991231P00220000", "side": "sell", "qty": "1"},
        {},
    )

    assert verdict.decision == "hard_block"
    assert verdict.rule_id == "option-sell-guard"


def test_thin_delta_single_leg_option_order_hits_net_delta_floor():
    """Same discriminating proof as the spread-guard/expiry-floor
    reachability tests above, for net-delta-floor specifically: this rule
    existed as tested code for days before this change but was never
    registered in RULE_TYPES or listed in policies/default.yaml (see
    AUDIT.md and README's now-updated "What this does not do") -- it could
    not have fired for ANY call, hedge-triggered or otherwise. This proves
    it now does, through the real policy, with no hedge-trigger involved.

    type: "limit" (not the default "market") deliberately keeps
    option-spread-guard from evaluating this call at all -- isolating
    net-delta-floor as the rule under test, not a coincidence of ordering.
    """
    engine = PolicyEngine.from_yaml("policies/default.yaml")

    with patch("firewall.rules.net_delta_floor.fetch_option_latest_quote") as fake:
        fake.return_value = OptionQuoteResult(
            ok=True, quote=OptionQuote(bid=4.15, ask=4.30, delta=-0.05)
        )
        verdict = engine.evaluate(
            "place_option_order",
            {
                "symbol": "AAPL991231P00220000",
                "side": "buy",
                "qty": "1",
                "type": "limit",
                "limit_price": "4.30",
            },
            {},
        )

    assert verdict.decision == "hard_block"
    assert verdict.rule_id == "net-delta-floor"
    assert "minimum structural threshold" in verdict.reason
    assert fake.called


def test_expensive_option_buy_hits_hedge_cost_cap():
    """Same discriminating proof as above, for hedge-cost-cap: a brand new
    rule, evaluated through the real policy for a bare place_option_order
    call with no hedge-trigger involved anywhere.

    Uses a CALL with a strongly positive delta (0.50) so net-delta-floor's
    own checks pass cleanly (structural floor 0.15 cleared; net delta
    0 + 1*0.50*100 = +50, not below the 0.0 floor) and don't intercept this
    call first -- isolating hedge-cost-cap as the rule under test.
    """
    engine = PolicyEngine.from_yaml("policies/default.yaml")

    quote = OptionQuoteResult(ok=True, quote=OptionQuote(bid=14.80, ask=15.00, delta=0.50))
    with patch("firewall.rules.net_delta_floor.fetch_option_latest_quote", return_value=quote), \
         patch("firewall.rules.hedge_cost_cap.fetch_option_latest_quote", return_value=quote) as fake_cost:
        verdict = engine.evaluate(
            "place_option_order",
            {
                "symbol": "AAPL991231C00220000",
                "side": "buy",
                "qty": "2",
                "type": "limit",
                "limit_price": "15.00",
            },
            {"account_equity": 100_000.0},
        )

    assert verdict.decision == "hard_block"
    assert verdict.rule_id == "hedge-cost-cap"
    assert "proposed hedge costs $3,000.00" in verdict.reason
    assert fake_cost.called


def test_call_buy_hits_regime_guard_when_state_is_wired(monkeypatch):
    """Same discriminating proof as the other new-rule reachability tests
    above, for hedge-regime-call-guard: proves it's a live, registered
    RULE_TYPES entry evaluated by PolicyEngine.evaluate() for a bare
    place_option_order call with no hedge-trigger involved anywhere.

    Manually wires state["hedge_proposal_rule"]/state["drawdown_killswitch_
    rule"] from the real loaded policy's own rule instances -- exactly what
    FirewallMiddleware would need to do (a disclosed, not-yet-built wiring
    gap; see this rule's own module docstring) -- to prove the rule fires
    correctly once that wiring exists, not just in isolated unit tests.

    The fake bars fetcher below only serves bars for "AAPL" (the flagged
    position's own ticker, from order_history) and fails for anything else
    -- proving compute_proposal()'s drawdown path resolves bars for the
    FLAGGED stock symbol, not the incoming call's own OCC symbol
    ("AAPL991218C00150000"). A fetcher that ignored its `symbol` argument
    would pass this assertion for the wrong reason.
    """
    import firewall.rules.hedge_proposal as hedge_proposal_module

    fake_bars = BarsResult(
        ok=True,
        bars=[DailyBar(close=150.0, volume=1000.0), DailyBar(close=151.0, volume=1000.0)],
    )

    def fake_fetch_daily_bars(symbol: str, lookback: int) -> BarsResult:
        if symbol != "AAPL":
            return BarsResult(ok=False, reason=f"no such symbol: {symbol!r}")
        return fake_bars

    monkeypatch.setattr(hedge_proposal_module, "fetch_daily_bars", fake_fetch_daily_bars)

    engine = PolicyEngine.from_yaml("policies/default.yaml")
    hedge_rule = next(r for r in engine.rules if r.id == "hedge-proposal")
    drawdown_rule = next(r for r in engine.rules if r.id == "drawdown-killswitch")

    history = OrderHistory()
    history.record(
        timestamp=1.0, tool="place_stock_order", symbol="AAPL", side="buy",
        qty=100, price=150.0, order_id="1", outcome="filled",
    )

    verdict = engine.evaluate(
        "place_option_order",
        {"symbol": "AAPL991218C00150000", "side": "buy", "qty": "1"},
        {
            "session_pnl_usd": -5000.0,  # well past drawdown-killswitch's default -1000 threshold
            "order_history": history,
            "hedge_proposal_rule": hedge_rule,
            "drawdown_killswitch_rule": drawdown_rule,
        },
    )

    assert verdict.decision == "hard_block"
    assert verdict.rule_id == "hedge-regime-call-guard"
    assert "authorized hedging structures are limited to protective puts or collars" in verdict.reason


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


# --- record_call_pending: crash-safe logging for allow/soft_block calls ---


def test_record_call_pending_writes_a_pending_record_before_forwarding(tmp_path):
    """record_call_pending is how a caller (the proxy) logs that it is
    about to attempt forwarding an allowed call, before it actually does
    so -- so a crash mid-forward still leaves a record the call was
    attempted, even though its outcome is then unknown."""
    log_path = tmp_path / "audit.jsonl"
    writer = AuditLogWriter(log_path, session_id="test-session")
    engine = PolicyEngine(rules=[], version="test", audit_writer=writer)

    verdict = engine.evaluate("get_account", {}, {})
    assert verdict.decision == "allow"

    pending = engine.record_call_pending("get_account", {}, verdict)

    assert pending is not None
    assert pending.verdict == "allow"
    assert pending.forwarded is None
    assert pending.upstream_status == "pending"
    assert pending.call_id is not None

    records = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    assert records[0]["forwarded"] is None
    assert records[0]["upstream_status"] == "pending"


def test_record_call_pending_writes_soft_block_reason_for_warnings(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    writer = AuditLogWriter(log_path, session_id="test-session")
    engine = PolicyEngine(rules=[], version="test", audit_writer=writer)

    verdict = Verdict(
        decision="allow",
        warnings=[
            Warning(rule_id="cap-soft", regulation_ref="SEC Rule 15c3-5(c)(1)(i)", reason="over cap"),
        ],
    )

    pending = engine.record_call_pending("place_order", {"qty": 10}, verdict)

    assert pending.verdict == "soft_block"
    assert pending.rule_id == "cap-soft"
    assert "over cap" in pending.reason
    assert pending.forwarded is None
    assert pending.upstream_status == "pending"


def test_record_call_pending_is_a_noop_for_hard_block_verdicts(tmp_path):
    """A hard_block is never forwarded, so there's nothing to mark
    pending -- evaluate() already logged its own final record."""
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

    pending = engine.record_call_pending(
        "place_order", {"symbol": "TSLA", "qty": 10, "limit_price": 50}, verdict
    )

    assert pending is None
    records = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1  # only evaluate()'s own hard_block record


def test_record_call_pending_is_a_noop_without_an_audit_writer():
    engine = PolicyEngine(rules=[], version="test")
    verdict = engine.evaluate("get_account", {}, {})

    pending = engine.record_call_pending("get_account", {}, verdict)

    assert pending is None


def test_record_call_outcome_links_to_its_pending_record(tmp_path):
    """The outcome record must carry the same call_id as its pending
    record and an explicit pending_hash reference to it -- two linked
    records, not one edited record."""
    log_path = tmp_path / "audit.jsonl"
    writer = AuditLogWriter(log_path, session_id="test-session")
    engine = PolicyEngine(rules=[], version="test", audit_writer=writer)

    verdict = engine.evaluate("get_account", {}, {})
    pending = engine.record_call_pending("get_account", {}, verdict)

    engine.record_call_outcome(
        "get_account", {}, verdict, forwarded=True, upstream_status="ok", pending=pending
    )

    records = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 2
    pending_record, outcome_record = records

    assert pending_record["upstream_status"] == "pending"
    assert pending_record["forwarded"] is None

    assert outcome_record["call_id"] == pending_record["call_id"]
    assert outcome_record["pending_hash"] == compute_record_hash(pending)
    assert outcome_record["forwarded"] is True
    assert outcome_record["upstream_status"] == "ok"

    # The pending record's own bytes are untouched -- confirmed by the
    # chain still verifying end to end.
    ok, bad_index = verify_chain(log_path)
    assert ok is True
    assert bad_index is None


def test_record_call_outcome_without_pending_still_works(tmp_path):
    """Backward-compatible: a caller that never calls record_call_pending
    (e.g. an existing test, or a future caller that doesn't need the
    crash-safety guarantee) still gets a normal outcome record, just
    without a call_id/pending_hash link."""
    log_path = tmp_path / "audit.jsonl"
    writer = AuditLogWriter(log_path, session_id="test-session")
    engine = PolicyEngine(rules=[], version="test", audit_writer=writer)

    verdict = engine.evaluate("get_account", {}, {})
    engine.record_call_outcome(
        "get_account", {}, verdict, forwarded=True, upstream_status="ok"
    )

    records = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    assert records[0]["call_id"] is None
    assert records[0]["pending_hash"] is None


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
