"""drawdown_killswitch — hard block ALL order placement once session PnL
drops below a threshold. Once tripped, stays tripped until explicit reset.

Regulation: SEC Rule 15c3-5(c)(1)(i) (pre-trade controls must include an
automated halt on breach of a defined loss limit).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from firewall.rules._util import matches_any
from firewall.rules.base import Rule, RuleConfig, RuleOutcome


class _Params(BaseModel):
    session_pnl_threshold_usd: float
    tool_match: list[str] = ["order"]
    session_pnl_state_key: str = "session_pnl_usd"


class DrawdownKillswitchRule(Rule):
    def __init__(self, config: RuleConfig) -> None:
        super().__init__(config)
        self.cfg = _Params.model_validate(config.params)
        self._tripped = False

    def check(
        self, tool_name: str, arguments: dict[str, Any], state: dict[str, Any]
    ) -> RuleOutcome:
        pnl = state.get(self.cfg.session_pnl_state_key)
        if isinstance(pnl, (int, float)) and not isinstance(pnl, bool):
            if pnl < self.cfg.session_pnl_threshold_usd:
                self._tripped = True

        if not self._tripped:
            return RuleOutcome(False)

        # The latch trips on any call that reports PnL, but only blocks
        # calls that would place an order.
        if not matches_any(tool_name, self.cfg.tool_match):
            return RuleOutcome(False)

        return RuleOutcome(
            True,
            "drawdown killswitch is tripped (session PnL breached "
            f"${self.cfg.session_pnl_threshold_usd:,.2f}); order placement is "
            "blocked until reset",
        )

    def reset(self) -> None:
        self._tripped = False
