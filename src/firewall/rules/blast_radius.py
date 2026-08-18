"""blast_radius — hard block bulk tools (cancel-all, close-all, liquidate)
unless a human_approved flag is present in state.

Regulation: SEC Rule 15c3-5(b); FINRA Regulatory Notice 15-09 (pre-trade
controls must prevent erroneous or disorderly bulk actions).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from firewall.rules._util import matches_any
from firewall.rules.base import Rule, RuleConfig, RuleOutcome


class _Params(BaseModel):
    tool_match: list[str] = ["cancel_all", "close_all", "liquidate"]
    approval_state_key: str = "human_approved"


class BlastRadiusRule(Rule):
    def __init__(self, config: RuleConfig) -> None:
        super().__init__(config)
        self.cfg = _Params.model_validate(config.params)

    def check(
        self, tool_name: str, arguments: dict[str, Any], state: dict[str, Any]
    ) -> RuleOutcome:
        if not matches_any(tool_name, self.cfg.tool_match):
            return RuleOutcome(False)

        if state.get(self.cfg.approval_state_key) is True:
            return RuleOutcome(False)

        return RuleOutcome(
            True,
            f"bulk tool {tool_name!r} requires human approval before it is forwarded",
        )
