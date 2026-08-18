"""notional_cap — max USD notional per single order.

Regulation: SEC Rule 15c3-5(c)(1)(i) (pre-trade controls must cap the value
of a single order).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from firewall.rules._util import extract_notional, matches_any
from firewall.rules.base import Rule, RuleConfig, RuleOutcome


class _Params(BaseModel):
    max_usd: float
    tool_match: list[str] = ["order"]
    notional_field: str = "notional"
    qty_field: str = "qty"
    price_field: str = "limit_price"


class NotionalCapRule(Rule):
    def __init__(self, config: RuleConfig) -> None:
        super().__init__(config)
        self.cfg = _Params.model_validate(config.params)

    def check(
        self, tool_name: str, arguments: dict[str, Any], state: dict[str, Any]
    ) -> RuleOutcome:
        if not matches_any(tool_name, self.cfg.tool_match):
            return RuleOutcome(False)

        notional = extract_notional(
            arguments, self.cfg.notional_field, self.cfg.qty_field, self.cfg.price_field
        )
        if notional is None:
            return RuleOutcome(False)

        if notional > self.cfg.max_usd:
            return RuleOutcome(
                True,
                f"order notional ${notional:,.2f} exceeds per-order cap "
                f"${self.cfg.max_usd:,.2f}",
            )
        return RuleOutcome(False)
