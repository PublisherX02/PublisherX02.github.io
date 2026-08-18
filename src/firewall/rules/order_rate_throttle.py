"""order_rate_throttle — max N order-placement calls per rolling T seconds.

On breach: hard block and latch a `paused` flag that requires an explicit
human reset before any further orders are allowed, even after the window
that caused the breach has aged out.

Regulation: SEC Rule 15c3-5(c)(1)(ii) (throttle on repeated automated order
submission).
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel

from firewall.order_history import OrderHistory
from firewall.rules._util import matches_any
from firewall.rules.base import Rule, RuleConfig, RuleOutcome


class _Params(BaseModel):
    max_orders: int
    window_seconds: float
    place_tool_match: list[str] = [
        "place_stock_order",
        "place_option_order",
        "place_crypto_order",
    ]
    history_state_key: str = "order_history"


class OrderRateThrottleRule(Rule):
    def __init__(self, config: RuleConfig) -> None:
        super().__init__(config)
        self.cfg = _Params.model_validate(config.params)
        self._paused = False

    def check(
        self, tool_name: str, arguments: dict[str, Any], state: dict[str, Any]
    ) -> RuleOutcome:
        if not matches_any(tool_name, self.cfg.place_tool_match):
            return RuleOutcome(False)

        if self._paused:
            return RuleOutcome(
                True,
                "order rate throttle is paused after a prior breach; "
                "requires explicit human reset before further orders",
            )

        history: OrderHistory | None = state.get(self.cfg.history_state_key)
        now = state.get("now", time.time())
        recent = history.since(now, self.cfg.window_seconds) if history else []
        recent_place_ids = [
            e.order_id for e in recent if matches_any(e.tool, self.cfg.place_tool_match)
        ]

        # +1 accounts for the call being evaluated right now, which hasn't
        # been recorded to history yet.
        if len(recent_place_ids) + 1 > self.cfg.max_orders:
            self._paused = True
            return RuleOutcome(
                True,
                f"order rate exceeded {self.cfg.max_orders} orders per "
                f"{self.cfg.window_seconds:.0f}s "
                f"(evidence order_ids: {recent_place_ids}); "
                "throttle paused until explicit human reset",
            )
        return RuleOutcome(False)

    def reset(self) -> None:
        self._paused = False
