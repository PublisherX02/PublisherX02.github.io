"""Rule engine.

Loads a policy config (see policies/default.yaml) and evaluates the
configured rules (see firewall/rules/) against incoming MCP tool calls.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel

from firewall.audit import AuditLogWriter, UpstreamStatus
from firewall.rules import RULE_TYPES, Rule, RuleConfig

logger = logging.getLogger(__name__)


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
        cls, path: Path | str, audit_writer: AuditLogWriter | None = None
    ) -> "PolicyEngine":
        path = Path(path)
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
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

        return Verdict(decision="allow", warnings=warnings)

    def record_call_outcome(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        verdict: Verdict,
        *,
        forwarded: bool,
        upstream_status: UpstreamStatus,
    ) -> None:
        """Write the single audit record for a call `evaluate()` allowed
        (with or without soft-rule warnings), once the caller has actually
        attempted to forward it upstream.

        `evaluate()` cannot write this record itself: for a hard_block it
        knows the outcome is final without any forwarding attempt (so it
        writes its own record immediately, see above), but for an allow --
        soft-blocked or not -- whether the call was actually forwarded and
        what upstream_status resulted is only known to the caller (the
        proxy), after `evaluate()` has already returned.

        No-op for hard_block verdicts (already logged by `evaluate()`) so
        callers can call this unconditionally after every `evaluate()`
        call without special-casing the decision themselves. No-op if no
        audit_writer is configured.
        """
        if verdict.decision == "hard_block":
            return
        if self.audit_writer is None:
            return

        if verdict.warnings:
            first = verdict.warnings[0]
            reason = "; ".join(f"{w.rule_id}: {w.reason}" for w in verdict.warnings)
            self.audit_writer.append(
                tool_name=tool_name,
                arguments=arguments,
                verdict="soft_block",
                reason=reason,
                forwarded=forwarded,
                upstream_status=upstream_status,
                rule_id=first.rule_id,
                regulation_ref=first.regulation_ref,
            )
        else:
            self.audit_writer.append(
                tool_name=tool_name,
                arguments=arguments,
                verdict="allow",
                reason="no rule triggered",
                forwarded=forwarded,
                upstream_status=upstream_status,
                rule_id=None,
                regulation_ref=None,
            )

    def reset(self, rule_id: str) -> None:
        """Clear sticky state (e.g. a tripped killswitch) on one rule."""
        for rule in self.rules:
            if rule.id == rule_id:
                rule.reset()
                return
        raise KeyError(f"Unknown rule id: {rule_id!r}")
