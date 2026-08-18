"""position_cap — max USD exposure per symbol, using current positions.

Regulation: SEC Rule 15c3-5(c)(1)(i) (pre-trade controls must cap maximum
position size per instrument).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from firewall.rules._util import extract_notional, matches_any
from firewall.rules.base import Rule, RuleConfig, RuleOutcome


class _Params(BaseModel):
    max_usd_per_symbol: float
    tool_match: list[str] = ["order"]
    symbol_field: str = "symbol"
    side_field: str = "side"
    notional_field: str = "notional"
    qty_field: str = "qty"
    price_field: str = "limit_price"
    # Expected shape: state[positions_state_key] == {symbol: current_usd_exposure}
    positions_state_key: str = "positions"


class PositionCapRule(Rule):
    def __init__(self, config: RuleConfig) -> None:
        super().__init__(config)
        self.cfg = _Params.model_validate(config.params)

    def check(
        self, tool_name: str, arguments: dict[str, Any], state: dict[str, Any]
    ) -> RuleOutcome:
        if not matches_any(tool_name, self.cfg.tool_match):
            return RuleOutcome(False)

        symbol = arguments.get(self.cfg.symbol_field)
        if not symbol:
            return RuleOutcome(False)

        # A sell reduces long exposure; this cap only guards against
        # accumulating too large a position in one instrument.
        side = str(arguments.get(self.cfg.side_field, "buy")).lower()
        if side == "sell":
            return RuleOutcome(False)

        notional = extract_notional(
            arguments, self.cfg.notional_field, self.cfg.qty_field, self.cfg.price_field
        )
        if notional is None:
            return RuleOutcome(False)

        positions: dict[str, Any] = state.get(self.cfg.positions_state_key) or {}
        current = float(positions.get(symbol, 0.0))
        prospective = current + notional

        if prospective > self.cfg.max_usd_per_symbol:
            return RuleOutcome(
                True,
                f"prospective exposure ${prospective:,.2f} for {symbol} exceeds cap "
                f"${self.cfg.max_usd_per_symbol:,.2f} "
                f"(current ${current:,.2f} + order ${notional:,.2f})",
            )
        return RuleOutcome(False)
