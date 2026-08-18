"""gtc_restriction — reject GTC orders unless policy explicitly allows them.

A good-til-canceled order stays live across sessions, so it can execute
against stale intent long after the agent (or its risk assumptions) has
moved on. This rule rejects rather than rewrites: it never substitutes a
different time_in_force into the payload, since silently changing what an
agent asked for would forward an order the agent never actually
requested. The agent must resubmit with a corrected value itself.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from firewall.rules._util import matches_any
from firewall.rules.base import Rule, RuleConfig, RuleOutcome

_REJECTION_REASON = "GTC orders are restricted by policy; resubmit as day or ioc."


class _Params(BaseModel):
    allow_gtc: bool = False
    tool_match: list[str] = ["order"]
    time_in_force_field: str = "time_in_force"


class GTCRestrictionRule(Rule):
    def __init__(self, config: RuleConfig) -> None:
        super().__init__(config)
        self.cfg = _Params.model_validate(config.params)

    def check(
        self, tool_name: str, arguments: dict[str, Any], state: dict[str, Any]
    ) -> RuleOutcome:
        if not matches_any(tool_name, self.cfg.tool_match):
            return RuleOutcome(False)

        time_in_force = arguments.get(self.cfg.time_in_force_field)
        if not time_in_force:
            return RuleOutcome(False)

        if str(time_in_force).strip().lower() == "gtc" and not self.cfg.allow_gtc:
            return RuleOutcome(True, _REJECTION_REASON)
        return RuleOutcome(False)
