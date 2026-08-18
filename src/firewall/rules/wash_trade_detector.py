"""wash_trade_detector — flag offsetting buy/sell pairs on the same symbol
within a window that net to approximately zero position change.

Matches the order about to be placed against recent, already-filled orders
on the opposite side of the same symbol: if a filled leg exists whose qty
is within `qty_tolerance` of this order's qty, this order would close out
that position with no real economic change of ownership.

Regulation: FINRA Rule 5210.02 (wash trading signature).
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel

from firewall.order_history import OrderHistory
from firewall.rules._util import matches_any
from firewall.rules.base import Rule, RuleConfig, RuleOutcome

_OPPOSITE_SIDE = {"buy": "sell", "sell": "buy"}


class _Params(BaseModel):
    window_seconds: float
    qty_tolerance: float
    place_tool_match: list[str] = [
        "place_stock_order",
        "place_option_order",
        "place_crypto_order",
    ]
    symbol_field: str = "symbol"
    side_field: str = "side"
    qty_field: str = "qty"
    history_state_key: str = "order_history"


class WashTradeDetectorRule(Rule):
    def __init__(self, config: RuleConfig) -> None:
        super().__init__(config)
        self.cfg = _Params.model_validate(config.params)

    def check(
        self, tool_name: str, arguments: dict[str, Any], state: dict[str, Any]
    ) -> RuleOutcome:
        if not matches_any(tool_name, self.cfg.place_tool_match):
            return RuleOutcome(False)

        symbol = arguments.get(self.cfg.symbol_field)
        side = arguments.get(self.cfg.side_field)
        qty = arguments.get(self.cfg.qty_field)
        if not symbol or side not in _OPPOSITE_SIDE or not isinstance(qty, (int, float)):
            return RuleOutcome(False)

        history: OrderHistory | None = state.get(self.cfg.history_state_key)
        if history is None:
            return RuleOutcome(False)

        opposite_side = _OPPOSITE_SIDE[side]
        now = state.get("now", time.time())
        recent = history.since(now, self.cfg.window_seconds)

        offsetting = [
            e
            for e in recent
            if e.symbol == symbol
            and e.side == opposite_side
            and e.outcome == "filled"
            and abs(e.qty - qty) <= self.cfg.qty_tolerance
        ]

        if offsetting:
            evidence = [e.order_id for e in offsetting]
            return RuleOutcome(
                True,
                f"this {side} {qty} {symbol} order offsets recent filled "
                f"{opposite_side} order(s) of matching size within tolerance "
                f"{self.cfg.qty_tolerance} (evidence order_ids: {evidence}); "
                "possible wash trade",
            )
        return RuleOutcome(False)
