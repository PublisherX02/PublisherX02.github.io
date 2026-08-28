"""unrecognized_tool_catchall — default-deny backstop for any tool call that
is neither on the read-only whitelist nor matched by any other configured
rule's tool_match pattern.

Motivation (conformance audit, section A2): every other rule in this engine
only evaluates a call substantively if its `tool_match`/`place_tool_match`
pattern matches the incoming tool name. A tool name that matches nothing --
because it's a real Alpaca action this repo has never wired a rule for, or a
future Alpaca tool addition/rename, or an adversarially-injected fake name --
gets an unconditional `allow` from every other rule (each one's `check()`
returns `RuleOutcome(False)` simply because its pattern didn't match, which
is indistinguishable from "matched and found fine"). Demonstrated live
during the audit: a fabricated tool name carrying a $999,999,999 argument
was forwarded upstream with `reason: "no rule triggered"`.

This rule closes that gap by being the opposite of every other rule here:
instead of pattern-matching to decide whether to look closer, it pattern-
matches to decide whether to back off. Anything on `read_only_whitelist`
(exact match, case-insensitive -- these are genuinely inert calls: account/
market/watchlist/docs *reads*) is left alone. Anything matching one of
`covered_patterns` is left alone too, on the assumption that whatever rule
owns that pattern has already had its say. Everything else is hard-blocked.

Must be the *last* rule in the configured list: `PolicyEngine.evaluate`
short-circuits on the first triggered hard rule, so as long as this rule is
last, by the time it runs every earlier rule has already had a chance to
recognize and clear the call on its own terms -- this rule only ever sees
calls nothing else claimed.

`covered_patterns` intentionally duplicates the `tool_match`/
`place_tool_match` values already declared on the other rules in the same
policy file rather than introspecting them at runtime, to keep this rule's
behavior fully readable from its own YAML block and to avoid coupling it to
the internals of every other rule type. It must be kept in sync by hand when
another rule's tool_match changes or a new pattern-matched rule is added --
see policies/default.yaml's comment above this rule's entry.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from firewall.rules._util import matches_any
from firewall.rules.base import Rule, RuleConfig, RuleOutcome


class _Params(BaseModel):
    read_only_whitelist: list[str] = []
    covered_patterns: list[str] = []


class UnrecognizedToolCatchallRule(Rule):
    def __init__(self, config: RuleConfig) -> None:
        super().__init__(config)
        self.cfg = _Params.model_validate(config.params)
        self._whitelist_lower = {name.lower() for name in self.cfg.read_only_whitelist}

    def check(
        self, tool_name: str, arguments: dict[str, Any], state: dict[str, Any]
    ) -> RuleOutcome:
        if tool_name.lower() in self._whitelist_lower:
            return RuleOutcome(False)

        if matches_any(tool_name, self.cfg.covered_patterns):
            return RuleOutcome(False)

        return RuleOutcome(
            True,
            f"unrecognized tool {tool_name!r} — not covered by any policy rule, "
            "failing closed",
        )


def validate_catchall_coverage(rules: list[dict[str, Any]]) -> None:
    """Cross-checks unrecognized_tool_catchall's covered_patterns and read_only_whitelist
    against every other rule's actual configured tool_match patterns.

    Raises ValueError with a detailed explanation if:
    1. Any rule configures a tool matching pattern not covered by catchall.covered_patterns.
    2. catchall.covered_patterns contains an orphaned pattern not used by any rule.
    3. Any action tool (e.g. place_*, cancel_*, close_*, replace_*, liquidate) appears on read_only_whitelist.
    """
    catchall_rule: dict[str, Any] | None = None
    for r in rules:
        if r.get("type") == "unrecognized_tool_catchall":
            catchall_rule = r
            break

    if catchall_rule is None:
        raise ValueError("Policy configuration missing 'unrecognized_tool_catchall' rule")

    catchall_params = catchall_rule.get("params", {}) if "params" in catchall_rule else catchall_rule
    covered_patterns = catchall_params.get("covered_patterns", [])
    read_only_whitelist = catchall_params.get("read_only_whitelist", [])

    # Collect all tool matching patterns from other rules
    rule_patterns: dict[str, list[str]] = {}
    for r in rules:
        if r.get("type") == "unrecognized_tool_catchall":
            continue
        rule_id = r.get("id", "<unknown>")
        params = r.get("params", {}) if "params" in r else r
        patterns: list[str] = []
        for k, v in params.items():
            if (k.endswith("tool_match") or k.endswith("_match") or k == "tool_match") and isinstance(v, list):
                for item in v:
                    if isinstance(item, str):
                        patterns.append(item)
        if patterns:
            rule_patterns[rule_id] = patterns

    # 1. Check for uncovered rule patterns
    uncovered: list[str] = []
    for rule_id, patterns in rule_patterns.items():
        for pat in patterns:
            if not matches_any(pat, covered_patterns):
                uncovered.append(f"rule '{rule_id}' pattern {pat!r}")

    # 2. Check for orphaned covered_patterns
    all_rule_patterns = [pat for patterns in rule_patterns.values() for pat in patterns]
    orphaned: list[str] = []
    for cov in covered_patterns:
        if not any(cov.lower() in p.lower() or p.lower() in cov.lower() for p in all_rule_patterns):
            orphaned.append(cov)

    # 3. Check for action tools inappropriately placed on read_only_whitelist
    action_prefixes = ("place_", "cancel_", "close_", "liquidate", "replace_", "delete_", "create_", "update_")
    action_tool_in_whitelist: list[str] = []
    for tool in read_only_whitelist:
        if any(tool.lower().startswith(p) for p in action_prefixes):
            action_tool_in_whitelist.append(tool)

    errors = []
    if uncovered:
        errors.append(f"Uncovered rule patterns not matched by covered_patterns: {', '.join(uncovered)}")
    if orphaned:
        errors.append(f"Orphaned covered_patterns not matching any rule: {', '.join(orphaned)}")
    if action_tool_in_whitelist:
        errors.append(f"Action tools improperly listed on read_only_whitelist: {', '.join(action_tool_in_whitelist)}")

    if errors:
        raise ValueError("Catchall pattern coverage drift detected:\n" + "\n".join(f"- {e}" for e in errors))

