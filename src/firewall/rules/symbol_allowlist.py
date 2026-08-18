"""symbol_allowlist — hard block any order for a symbol not on the list.

Regulation: SEC Rule 15c3-5(c)(2)(ii) (algorithmic trading must be
restricted to a defined, reviewed set of instruments).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from firewall.rules._util import matches_any
from firewall.rules.base import Rule, RuleConfig, RuleOutcome


class _Params(BaseModel):
    allowed_symbols: list[str]
    tool_match: list[str] = ["order"]
    symbol_field: str = "symbol"


class SymbolAllowlistRule(Rule):
    def __init__(self, config: RuleConfig) -> None:
        super().__init__(config)
        self.cfg = _Params.model_validate(config.params)
        self._allowed = {s.upper() for s in self.cfg.allowed_symbols}

    def check(
        self, tool_name: str, arguments: dict[str, Any], state: dict[str, Any]
    ) -> RuleOutcome:
        if not matches_any(tool_name, self.cfg.tool_match):
            return RuleOutcome(False)

        symbol = arguments.get(self.cfg.symbol_field)
        if not symbol:
            return RuleOutcome(False)

        if str(symbol).upper() not in self._allowed:
            return RuleOutcome(True, f"symbol {symbol!r} is not on the allowlist")
        return RuleOutcome(False)
