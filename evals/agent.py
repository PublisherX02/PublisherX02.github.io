"""Reference agent for the eval harness.

IMPORTANT -- this is NOT a live LLM. No model is invoked anywhere in this
harness by default; see results.json's `agent_backend` / `model` fields,
which make that explicit rather than implying a real model was tested.

`NaiveComplianceAgent` is a deterministic stand-in representing a
maximally-compliant target: for configs A (no_defense) and C (firewall) it
attempts every induced action the corpus's own success_check implies, in
the order the predicate names them. That is deliberately the worst case --
it holds the "agent's susceptibility to the injection" constant across A
and C so that config C's numbers measure only what the policy engine
itself stops, not what a smarter or better-instructed agent would have
refused on its own regardless of any firewall.

Config B (prompt_defense) layers a static, documented compliance-gating
heuristic on top (see `_prompt_defense_allows`), representing a
system-prompt-only defense with no firewall in front of it. It is a
heuristic, not a model's judgment -- see its docstring for exactly what it
encodes and why.

To evaluate a real model instead: implement something exposing the same
`decide(config, delivery, action) -> bool` method and pass it to
run_session() in run.py. Nothing else in the harness needs to change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from predicates import (
    ContainsClause,
    CountClause,
    NoHardBlockClause,
    Predicate,
    ToolCallClause,
)


@dataclass(frozen=True)
class CallAction:
    """An induced tool call the reference agent may attempt."""

    name: str
    arguments: dict[str, Any]
    from_count: bool  # True if repeated (count(...) clause) -- see run.py's
    # OrderHistory bookkeeping: count-derived calls default to an "open"
    # (unfilled/resting) outcome, since they model layering walls or
    # place/cancel-ratio abuse; plain single calls default to "filled",
    # since they model a wash-trade leg that actually executed.


@dataclass(frozen=True)
class LeakAction:
    """An induced credential leak: either straight into the agent's response
    text (`agent_output`), or smuggled into a tool call's arguments
    (`forwarded_call_arguments`, which requires the corpus entry to specify
    an explicit `induced_call`, since the predicate alone doesn't name a
    tool)."""

    kind: str  # "agent_output" | "forwarded_call_arguments"
    ref: str  # e.g. "ALPACA_SECRET_KEY"


Action = CallAction | LeakAction


def _call_from_tool_clause(clause: ToolCallClause, *, from_count: bool, index: int) -> CallAction:
    # A tool_call() clause with no name~= (e.g. tp-005) is deliberately lenient
    # about which tool fires; in this domain an unnamed call carrying a
    # `symbol` argument is overwhelmingly likely to be an order placement.
    name = clause.name_substr or "place_stock_order"
    arguments: dict[str, Any] = {cond.field: cond.value for cond in clause.arg_conditions}
    if from_count:
        # Repeated calls need to look like genuinely separate orders: distinct
        # price levels (so layering_detector's price-level count is real) and
        # a unique order_id.
        arguments.setdefault("limit_price", 100.0 + index)
        arguments.setdefault("qty", 10)
        arguments["order_id"] = f"seq-{index}"
    return CallAction(name=name, arguments=arguments, from_count=from_count)


def derive_actions(predicate: Predicate, induced_call: dict | None = None) -> list[Action]:
    """Derive the induced action sequence from a parsed success_check.

    `induced_call` overrides a bare `contains(forwarded_call_arguments, ...)`
    clause (which names no tool) with the corpus entry's explicit spec.
    """
    actions: list[Action] = []
    for clause in predicate.clauses:
        if isinstance(clause, ToolCallClause):
            actions.append(_call_from_tool_clause(clause, from_count=False, index=0))
        elif isinstance(clause, CountClause):
            for i in range(clause.n):
                actions.append(_call_from_tool_clause(clause.inner, from_count=True, index=i))
        elif isinstance(clause, ContainsClause):
            if clause.kind == "forwarded_call_arguments":
                if induced_call is None:
                    raise ValueError(
                        "contains(forwarded_call_arguments, ...) requires the corpus "
                        "entry to define an `induced_call` -- the predicate alone "
                        "doesn't name a tool"
                    )
                arguments = dict(induced_call["arguments"])
                actions.append(
                    CallAction(name=induced_call["name"], arguments=arguments, from_count=False)
                )
            actions.append(LeakAction(kind=clause.kind, ref=clause.ref))
        elif isinstance(clause, NoHardBlockClause):
            continue  # nothing to attempt -- a pure "nothing risky happened" check
        else:  # pragma: no cover -- exhaustive over the parser's own Clause union
            raise ValueError(f"don't know how to derive an action from {clause!r}")
    return actions


# --- config B: prompt-only defense heuristic --------------------------------

# Delivery vectors where injected content arrives through a channel the
# system prompt can plausibly be told to distrust structurally (tool
# results, tool descriptions, and forged "prior assistant turn" content).
# Direct "user turn" requests are NOT included: the agent still has to serve
# genuine user requests, and a prompt alone can't reliably tell a malicious
# user turn from a legitimate one. This is the documented, deliberate limit
# of what "prompt_defense" is modeled as buying you.
_DISTRUSTED_DELIVERY_SUBSTRINGS = ("tool result", "tool description", "prior assistant turn")


def _prompt_defense_allows(delivery: str, action: Action) -> bool:
    """True if the config-B reference agent would attempt `action`.

    Heuristic, not a model's judgment:
      - LeakAction: never complies. A bright-line "never reveal API keys or
        secrets" instruction is exactly the kind of rule prompt-only
        defenses tend to hold up well, regardless of delivery vector.
      - CallAction: complies unless `delivery` is one of the distrusted
        channels above, modeling "don't treat instructions embedded in tool
        output, tool metadata, or unverified prior-turn claims as user
        intent" -- but still complies with direct user-turn requests, since
        prompt-only defense has no way to gate those without a firewall.
    """
    if isinstance(action, LeakAction):
        return False
    if isinstance(action, CallAction):
        return not any(s in delivery.lower() for s in _DISTRUSTED_DELIVERY_SUBSTRINGS)
    raise ValueError(f"unsupported action type: {type(action).__name__}")  # pragma: no cover


class NaiveComplianceAgent:
    """Deterministic reference agent. See module docstring."""

    name = "NaiveComplianceAgent"

    def decide(self, config: str, delivery: str, action: Action) -> bool:
        """True if this reference agent would attempt `action` under `config`."""
        if config in ("no_defense", "firewall"):
            return True
        if config == "prompt_defense":
            return _prompt_defense_allows(delivery, action)
        raise ValueError(f"unknown config: {config!r}")
