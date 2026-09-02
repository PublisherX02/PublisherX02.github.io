"""drawdown_killswitch — hard block ALL order placement once session PnL
drops below a threshold. Once tripped, stays tripped until explicit reset.

Regulation: SEC Rule 15c3-5(c)(1)(i) (pre-trade controls must include an
automated halt on breach of a defined loss limit).
"""

from __future__ import annotations

import math
import re
from typing import Any

from pydantic import BaseModel

from firewall.rules._util import matches_any
from firewall.rules.base import Rule, RuleConfig, RuleOutcome


class _Params(BaseModel):
    session_pnl_threshold_usd: float
    tool_match: list[str] = ["order"]
    session_pnl_state_key: str = "session_pnl_usd"
    exposure_snapshot_state_key: str = "exposure_snapshot"


_OCC_OPTION_SYMBOL = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$", re.IGNORECASE)


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

        # The sole exception is an exact plain-equity sell bounded by a
        # fresh, authoritative broker quantity. Everything else reaches the
        # ordinary killswitch block below.
        symbol = str(arguments.get("symbol") or "").strip().upper()
        side = str(arguments.get("side") or "").strip().lower()
        asset_class = str(arguments.get("asset_class") or "us_equity").strip().lower()
        snapshot = state.get(self.cfg.exposure_snapshot_state_key)
        plain_equity_sell = (
            tool_name == "place_stock_order"
            and side == "sell"
            and asset_class in {"equity", "us_equity"}
            and "legs" not in arguments
            and not _OCC_OPTION_SYMBOL.fullmatch(symbol)
        )
        if plain_equity_sell and isinstance(snapshot, dict):
            try:
                held_qty = float((snapshot.get("positions") or {})[symbol])
                order_qty = float(arguments["qty"])
            except (KeyError, TypeError, ValueError):
                held_qty = order_qty = math.nan
            if (
                snapshot.get("ok") is True
                and snapshot.get("authoritative") is True
                and math.isfinite(held_qty)
                and math.isfinite(order_qty)
                and held_qty > 0
                and order_qty > 0
                and order_qty <= held_qty
            ):
                reason = (
                    "DELEVERAGING_EXCEPTION_ALLOW: drawdown killswitch is tripped; "
                    f"plain-equity sell for {symbol} is provably exposure-reducing "
                    f"(broker_confirmed_held_qty={held_qty:g}, order_qty={order_qty:g})"
                )
                return RuleOutcome(False, reason, state_events=[("info", reason)])

        return RuleOutcome(
            True,
            "drawdown killswitch is tripped (session PnL breached "
            f"${self.cfg.session_pnl_threshold_usd:,.2f}); order placement is "
            "blocked until reset",
        )

    def reset(self) -> None:
        self._tripped = False
