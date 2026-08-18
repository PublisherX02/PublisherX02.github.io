"""layering_detector — flag multiple unfilled orders at distinct price
levels on one side of a symbol, followed by an order on the opposite side.

This is the classic layering/spoofing shape: build a resting wall on one
side to move the price, then trade on the other side. Orders that actually
filled don't count as wall evidence -- a real, executed multi-price
scale-in isn't layering.

Regulation: FINRA Rule 5210; 15 U.S.C. 78i(a)(2); 17 CFR 240.10b-5; FINRA
Rule 2020 (spoofing / layering signature).
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
    min_distinct_price_levels: int
    place_tool_match: list[str] = [
        "place_stock_order",
        "place_option_order",
        "place_crypto_order",
    ]
    symbol_field: str = "symbol"
    side_field: str = "side"
    price_field: str = "limit_price"
    history_state_key: str = "order_history"


class LayeringDetectorRule(Rule):
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
        if not symbol or side not in _OPPOSITE_SIDE:
            return RuleOutcome(False)

        history: OrderHistory | None = state.get(self.cfg.history_state_key)
        if history is None:
            return RuleOutcome(False)

        wall_side = _OPPOSITE_SIDE[side]
        now = state.get("now", time.time())
        recent = history.since(now, self.cfg.window_seconds)

        wall_orders = [
            e
            for e in recent
            if e.symbol == symbol and e.side == wall_side and e.outcome != "filled"
        ]
        distinct_prices = {e.price for e in wall_orders if e.price is not None}

        if len(distinct_prices) >= self.cfg.min_distinct_price_levels:
            evidence = [e.order_id for e in wall_orders if e.price is not None]
            return RuleOutcome(
                True,
                f"{len(distinct_prices)} unfilled {wall_side} orders for {symbol} at "
                f"distinct price levels precede this {side} order "
                f"(evidence order_ids: {evidence}); possible layering",
            )
        return RuleOutcome(False)
