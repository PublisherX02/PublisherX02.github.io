"""Rule engine.

Loads a policy config (see policies/default.yaml) and evaluates the
configured rules (see firewall/rules/) against incoming MCP tool calls.
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Literal

import yaml
from pydantic import BaseModel

from firewall.audit import AuditEvent, AuditLogWriter, UpstreamStatus, compute_record_hash
from firewall.market_data import BarsResult
from firewall.rules import RULE_TYPES, Rule, RuleConfig, validate_catchall_coverage
from firewall.rules.cvar_gate import CVaRGateRule
from firewall.rules.notional_cap import NotionalCapRule
from firewall.rules.pct_of_adv import PctOfAdvRule
from firewall.rules.position_cap import PositionCapRule

logger = logging.getLogger(__name__)

BarsFetcher = Callable[[str, int], BarsResult]

# Rule classes whose __init__ accepts a `bars_fetcher` kwarg (all four
# fetch historical daily bars via the same shared firewall.market_data.
# fetch_daily_bars helper, defaulting to the real one when none is given).
# Explicit set, not introspection: matches this codebase's existing
# `matches_any`-style convention of stating exactly what's covered rather
# than inferring it from a method signature.
_BARS_FETCHER_AWARE_RULE_TYPES: tuple[type[Rule], ...] = (
    CVaRGateRule,
    PctOfAdvRule,
    NotionalCapRule,
    PositionCapRule,
)

_OCC_OPTION_SYMBOL = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$", re.IGNORECASE)
_OPTION_ORDERS_DISABLED_RULE_ID = "option-orders-disabled"
_OPTION_ORDERS_DISABLED_REASON = (
    "option orders are unconditionally disabled at the firewall boundary "
    "until their upstream schema and risk controls are rebuilt and verified."
)


class PolicyConfig(BaseModel):
    """Parsed representation of a policy YAML file."""

    version: str
    rules: list[dict[str, Any]] = []


class Warning(BaseModel):
    """A soft-severity rule that fired but did not block the call."""

    rule_id: str
    regulation_ref: str | None
    reason: str


class Verdict(BaseModel):
    """Result of evaluating a tool call against the policy.

    `rule_id`/`regulation_ref`/`reason` describe the hard block that fired
    (None when the decision is "allow"). Soft-severity rules never block --
    they're collected in `warnings` alongside an "allow" decision.
    """

    decision: Literal["allow", "hard_block"]
    rule_id: str | None = None
    regulation_ref: str | None = None
    reason: str | None = None
    warnings: list[Warning] = []
    informational_notes: list[str] = []


def _is_unsupported_order_shape(arguments: dict[str, Any]) -> bool:
    """Recognize unsupported order structures by shape, not field values.

    Presence is deliberate: malformed, empty, or partially populated child
    structures must fail closed instead of falling through merely because a
    caller omitted the fields a downstream sizing rule happens to inspect.
    """
    if not arguments:
        return False
    order_class = arguments.get("order_class")
    order_class_str = order_class.strip().casefold() if isinstance(order_class, str) else ""
    if order_class_str in ("bracket", "oco", "oto", "mleg"):
        return True
    if "legs" in arguments:
        return True
    if "take_profit" in arguments or "stop_loss" in arguments:
        return True
    return False


def _is_option_order_call(tool_name: str, arguments: dict[str, Any]) -> bool:
    """Recognize both named and disguised option-order calls.

    This is deliberately independent of the strategy and configured rule set:
    options stay disabled even when a caller uses another order tool name or
    constructs either the current legs-based schema or the legacy OCC-symbol
    schema.
    """
    normalized_tool_name = str(tool_name).strip().lower()
    if "option" in normalized_tool_name and "order" in normalized_tool_name:
        return True
    if not arguments:
        return False
    if "legs" in arguments:
        return True
    order_class = arguments.get("order_class")
    if isinstance(order_class, str) and order_class.strip().lower() == "mleg":
        return True
    symbol = arguments.get("symbol")
    return isinstance(symbol, str) and _OCC_OPTION_SYMBOL.fullmatch(symbol.strip()) is not None


def _describe_verdict(verdict: Verdict) -> tuple[str, str | None, str | None, str]:
    """Shared by record_call_pending/record_call_outcome: an allow verdict
    with warnings audit-logs as "soft_block" attributed to its first
    warning (matching record_call_outcome's pre-existing behavior);
    without warnings it's a plain "allow"."""
    if verdict.warnings:
        first = verdict.warnings[0]
        reason = "; ".join(f"{w.rule_id}: {w.reason}" for w in verdict.warnings)
        return "soft_block", first.rule_id, first.regulation_ref, reason
    return "allow", None, None, "no rule triggered"


class PolicyEngine:
    """Evaluates MCP tool calls against a set of loaded rules."""

    def __init__(
        self,
        rules: list[Rule],
        version: str,
        audit_writer: AuditLogWriter | None = None,
    ) -> None:
        self.rules = rules
        self.version = version
        self.audit_writer = audit_writer

    @classmethod
    def from_yaml(
        cls,
        path: Path | str,
        audit_writer: AuditLogWriter | None = None,
        bars_fetcher: BarsFetcher | None = None,
    ) -> "PolicyEngine":
        """`bars_fetcher`, when given, is passed to every constructed rule
        whose class is in `_BARS_FETCHER_AWARE_RULE_TYPES` (cvar_gate,
        pct_of_adv, notional_cap, position_cap) -- a single shared
        injection point so a test (or a future caller) can supply one fake
        historical-bars source for every market-data-dependent rule at
        once, instead of monkeypatching each rule module's own imported
        `fetch_daily_bars` name separately. Rule classes not in that set
        are constructed exactly as before (`rule_cls(rule_config)`),
        regardless of this argument. `None` (the default) means every
        bars-aware rule falls back to its own default -- the real
        `firewall.market_data.fetch_daily_bars` -- unchanged from before
        this parameter existed.
        """
        path = Path(path)
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw_rules = raw.get("rules", [])
        if any(rule.get("type") == "unrecognized_tool_catchall" for rule in raw_rules):
            validate_catchall_coverage(raw_rules)
        config = PolicyConfig.model_validate(raw)

        rules: list[Rule] = []
        for rule_dict in config.rules:
            rule_type = rule_dict.get("type")
            rule_cls = RULE_TYPES.get(rule_type)
            if rule_cls is None:
                raise ValueError(
                    f"Unknown rule type {rule_type!r} in rule {rule_dict.get('id')!r}. "
                    f"Known types: {sorted(RULE_TYPES)}"
                )
            rule_config = RuleConfig.model_validate(rule_dict)
            if bars_fetcher is not None and rule_cls in _BARS_FETCHER_AWARE_RULE_TYPES:
                rules.append(rule_cls(rule_config, bars_fetcher=bars_fetcher))
            else:
                rules.append(rule_cls(rule_config))

        return cls(rules=rules, version=config.version, audit_writer=audit_writer)

    def evaluate(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        state: dict[str, Any] | None = None,
    ) -> Verdict:
        """Evaluate rules in order. The first triggered hard rule wins and
        short-circuits evaluation; triggered soft rules are collected as
        warnings and evaluation continues.

        Any `RuleOutcome.state_events` a rule returns are written to
        `audit_writer` immediately, regardless of whether the outcome for
        this call is triggered -- they record a stateful rule's own state
        transitions (e.g. cooldown entered/exited), not the call's verdict.

        A rule that raises is never allowed to propagate out of here and
        never results in the call being forwarded: any unexpected exception
        from `rule.check()` -- from this rule or any future one -- is
        caught, logged with its full stack trace, and turned into an
        immediate hard block. Fail closed, not open.
        """
        state = state if state is not None else {}
        warnings: list[Warning] = []
        informational_notes: list[str] = []

        if _is_option_order_call(tool_name, arguments):
            if self.audit_writer is not None:
                self.audit_writer.append(
                    tool_name=tool_name,
                    arguments=arguments,
                    verdict="hard_block",
                    reason=_OPTION_ORDERS_DISABLED_REASON,
                    forwarded=False,
                    upstream_status="not_forwarded",
                    rule_id=_OPTION_ORDERS_DISABLED_RULE_ID,
                    regulation_ref=None,
                )
            return Verdict(
                decision="hard_block",
                rule_id=_OPTION_ORDERS_DISABLED_RULE_ID,
                regulation_ref=None,
                reason=_OPTION_ORDERS_DISABLED_REASON,
                warnings=warnings,
            )

        if _is_unsupported_order_shape(arguments):
            reason = (
                "bracket/OCO/multi-leg order shapes are not yet risk-assessed "
                "by this firewall and are blocked until support exists."
            )
            rule_id = "unsupported-order-shape"
            if self.audit_writer is not None:
                self.audit_writer.append(
                    tool_name=tool_name,
                    arguments=arguments,
                    verdict="hard_block",
                    reason=reason,
                    forwarded=False,
                    upstream_status="not_forwarded",
                    rule_id=rule_id,
                    regulation_ref=None,
                )
            return Verdict(
                decision="hard_block",
                rule_id=rule_id,
                regulation_ref=None,
                reason=reason,
                warnings=warnings,
            )

        for rule in self.rules:
            if not rule.enabled:
                continue

            try:
                outcome = rule.check(tool_name, arguments, state)
            except Exception as exc:
                logger.exception(
                    "rule evaluation error in rule_id=%r type=%r -- failing closed",
                    rule.id,
                    rule.type,
                )
                if self.audit_writer is not None:
                    self.audit_writer.append(
                        tool_name=tool_name,
                        arguments=arguments,
                        verdict="hard_block",
                        reason=f"rule evaluation error — failing closed: {exc}",
                        forwarded=False,
                        upstream_status="not_forwarded",
                        rule_id=rule.id,
                        regulation_ref=None,
                    )
                return Verdict(
                    decision="hard_block",
                    rule_id=rule.id,
                    regulation_ref=rule.regulation_ref,
                    reason=f"rule evaluation error — failing closed: {rule.id}",
                    warnings=warnings,
                )

            if outcome.state_events and self.audit_writer is not None:
                for event_verdict, event_reason in outcome.state_events:
                    self.audit_writer.append(
                        tool_name=tool_name,
                        arguments=arguments,
                        verdict=event_verdict,
                        reason=event_reason,
                        forwarded=False,
                        upstream_status="not_forwarded",
                        rule_id=rule.id,
                        regulation_ref=rule.regulation_ref,
                    )
            for event_verdict, event_reason in outcome.state_events:
                if event_verdict == "info":
                    informational_notes.append(event_reason)

            if not outcome.triggered:
                continue

            if rule.severity == "hard":
                if self.audit_writer is not None:
                    self.audit_writer.append(
                        tool_name=tool_name,
                        arguments=arguments,
                        verdict="hard_block",
                        reason=outcome.reason,
                        forwarded=False,
                        upstream_status="not_forwarded",
                        rule_id=rule.id,
                        regulation_ref=rule.regulation_ref,
                    )
                return Verdict(
                    decision="hard_block",
                    rule_id=rule.id,
                    regulation_ref=rule.regulation_ref,
                    reason=outcome.reason,
                    warnings=warnings,
                )

            warnings.append(
                Warning(
                    rule_id=rule.id,
                    regulation_ref=rule.regulation_ref,
                    reason=outcome.reason,
                )
            )

        return Verdict(
            decision="allow",
            warnings=warnings,
            informational_notes=informational_notes,
        )

    def record_call_pending(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        verdict: Verdict,
    ) -> AuditEvent | None:
        """Write a "pending" audit record for an allow/soft_block verdict,
        before the caller (the proxy) has attempted to forward the call
        upstream.

        This exists so a process crash between deciding to forward a call
        and actually recording its outcome (see `record_call_outcome`)
        still leaves a durable record that the call was attempted, rather
        than losing it entirely. The record is written with
        `forwarded=None`/`upstream_status="pending"` since neither is
        knowable yet, and is never mutated afterward -- the matching
        `record_call_outcome` call writes a second, separate record
        instead (see `find_unresolved_pending` in firewall.audit for how
        the two are matched up, and what it means if the second one never
        arrives).

        Returns the written `AuditEvent`, which the caller must pass as
        `pending=` to the matching `record_call_outcome` call so the two
        records can be linked by `call_id`. Returns None (writes nothing)
        for hard_block verdicts -- `evaluate()` already logged those
        synchronously, with no forwarding attempt to wait on -- and when
        no audit_writer is configured.
        """
        if verdict.decision == "hard_block":
            return None
        if self.audit_writer is None:
            return None

        verdict_label, rule_id, regulation_ref, reason = _describe_verdict(verdict)
        return self.audit_writer.append(
            tool_name=tool_name,
            arguments=arguments,
            verdict=verdict_label,
            reason=reason,
            forwarded=None,
            upstream_status="pending",
            rule_id=rule_id,
            regulation_ref=regulation_ref,
            call_id=str(uuid.uuid4()),
        )

    def record_call_outcome(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        verdict: Verdict,
        *,
        forwarded: bool,
        upstream_status: UpstreamStatus,
        pending: AuditEvent | None = None,
    ) -> None:
        """Write the audit record for a call `evaluate()` allowed (with or
        without soft-rule warnings), once the caller has actually
        attempted to forward it upstream.

        `evaluate()` cannot write this record itself: for a hard_block it
        knows the outcome is final without any forwarding attempt (so it
        writes its own record immediately, see above), but for an allow --
        soft-blocked or not -- whether the call was actually forwarded and
        what upstream_status resulted is only known to the caller (the
        proxy), after `evaluate()` has already returned.

        `pending`, if given, is the `AuditEvent` returned by an earlier
        `record_call_pending` call for this same call: this record then
        carries the same `call_id` and a `pending_hash` referencing it, so
        the pair can be matched up without mutating either record (see
        `record_call_pending`). Omitting `pending` still writes a normal,
        unlinked outcome record -- for callers that don't need the
        crash-safety guarantee, and for backward compatibility.

        No-op for hard_block verdicts (already logged by `evaluate()`) so
        callers can call this unconditionally after every `evaluate()`
        call without special-casing the decision themselves. No-op if no
        audit_writer is configured.
        """
        if verdict.decision == "hard_block":
            return
        if self.audit_writer is None:
            return

        verdict_label, rule_id, regulation_ref, reason = _describe_verdict(verdict)
        self.audit_writer.append(
            tool_name=tool_name,
            arguments=arguments,
            verdict=verdict_label,
            reason=reason,
            forwarded=forwarded,
            upstream_status=upstream_status,
            rule_id=rule_id,
            regulation_ref=regulation_ref,
            call_id=pending.call_id if pending is not None else None,
            pending_hash=compute_record_hash(pending) if pending is not None else None,
        )

    def reset(self, rule_id: str) -> None:
        """Clear sticky state (e.g. a tripped killswitch) on one rule."""
        for rule in self.rules:
            if rule.id == rule_id:
                rule.reset()
                return
        raise KeyError(f"Unknown rule id: {rule_id!r}")
