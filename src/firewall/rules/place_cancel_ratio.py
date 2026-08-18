"""place_cancel_ratio — hard block further orders on a symbol once its
cancel-to-place ratio, over a rolling window, indicates spoofing/layering.

cancelled_orders / placed_orders is computed only over orders that were
placed and later cancelled while still unfilled (outcome == "cancelled").
A minimum sample size (min_orders_in_window) guards against flagging a
trader's very first cancel as abuse.

Regulation: UNMAPPED. No verified US federal/FINRA citation has been
confirmed for this specific place/cancel-ratio heuristic -- regulation_ref
is left null in policy config rather than approximated. (Previously cited
an EU market-abuse provision that does not apply to this US-only account;
removed, not replaced.)
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel

from firewall.order_history import OrderHistory
from firewall.rules._util import matches_any
from firewall.rules.base import Rule, RuleConfig, RuleOutcome


class _Params(BaseModel):
    ratio_threshold: float
    window_seconds: float
    min_orders_in_window: int
    place_tool_match: list[str] = [
        "place_stock_order",
        "place_option_order",
        "place_crypto_order",
    ]
    symbol_field: str = "symbol"
    history_state_key: str = "order_history"


class PlaceCancelRatioRule(Rule):
    def __init__(self, config: RuleConfig) -> None:
        super().__init__(config)
        self.cfg = _Params.model_validate(config.params)

    def check(
        self, tool_name: str, arguments: dict[str, Any], state: dict[str, Any]
    ) -> RuleOutcome:
        if not matches_any(tool_name, self.cfg.place_tool_match):
            return RuleOutcome(False)

        symbol = arguments.get(self.cfg.symbol_field)
        if not symbol:
            return RuleOutcome(False)

        history: OrderHistory | None = state.get(self.cfg.history_state_key)
        if history is None:
            return RuleOutcome(False)

        now = state.get("now", time.time())
        recent = [
            e for e in history.since(now, self.cfg.window_seconds) if e.symbol == symbol
        ]
        placed = [e for e in recent if matches_any(e.tool, self.cfg.place_tool_match)]

        if len(placed) < self.cfg.min_orders_in_window:
            return RuleOutcome(False)

        cancelled = [e for e in placed if e.outcome == "cancelled"]
        ratio = len(cancelled) / len(placed) if placed else 0.0

        if ratio > self.cfg.ratio_threshold:
            evidence = [e.order_id for e in cancelled]
            return RuleOutcome(
                True,
                f"place/cancel ratio {ratio:.2f} for {symbol} exceeds threshold "
                f"{self.cfg.ratio_threshold:.2f} over {len(placed)} orders in "
                f"{self.cfg.window_seconds:.0f}s "
                f"(evidence order_ids: {evidence})",
            )
        return RuleOutcome(False)
