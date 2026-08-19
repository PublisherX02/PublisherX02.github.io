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
