"""Fail-closed cross-cycle pending-order exposure enforcement."""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel

from firewall.rules.base import Rule, RuleConfig, RuleOutcome


class _Params(BaseModel):
    place_tool_match: list[str] = ["place_stock_order"]
    snapshot_state_key: str = "exposure_snapshot"
    metadata_field: str = "_firewall_reconciliation"
    max_target_usd: float = 20_000.0
    max_target_pct_of_equity: float = 0.25
    account_equity_state_key: str = "account_equity"


class PendingOrderExposureRule(Rule):
    def __init__(self, config: RuleConfig) -> None:
        super().__init__(config)
        self.cfg = _Params.model_validate(config.params)

    def check(self, tool_name: str, arguments: dict[str, Any], state: dict[str, Any]) -> RuleOutcome:
        if tool_name not in self.cfg.place_tool_match:
            return RuleOutcome(False)
        snapshot = state.get(self.cfg.snapshot_state_key)
        metadata = arguments.get(self.cfg.metadata_field)
        if isinstance(snapshot, dict) and snapshot.get("authoritative") is False:
            return RuleOutcome(False)
        if not isinstance(snapshot, dict) or not snapshot.get("ok"):
            return RuleOutcome(True, "broker position/open-order exposure snapshot unavailable")
        if not isinstance(metadata, dict):
            pending = snapshot.get("pending_signed_qty") or {}
            if any(abs(float(value)) > 1e-9 for value in pending.values()):
                return RuleOutcome(True, "order lacks reconciliation metadata while pending exposure exists")
            return RuleOutcome(False)
        symbol = str(arguments.get("symbol") or "").upper()
        side = str(arguments.get("side") or "").lower()
        try:
            qty = float(arguments["qty"])
            target_qty = float(metadata["target_qty"])
            current_qty = float(snapshot["positions"].get(symbol, 0.0))
            pending_qty = float(snapshot["pending_signed_qty"].get(symbol, 0.0))
        except (KeyError, TypeError, ValueError):
            return RuleOutcome(True, "reconciliation exposure fields are incomplete")
        prices = snapshot.get("current_prices") or {}
        position_cap_rule = state.get("position_cap_rule")
        try:
            equity = float(state[self.cfg.account_equity_state_key])
        except (KeyError, TypeError, ValueError):
            return RuleOutcome(True, "server-side target validation data is incomplete")
        try:
            price = float(prices[symbol])
        except (KeyError, TypeError, ValueError):
            # A brand-new symbol has no broker position/current_price yet.
            # Use the enabled position-cap rule's own trusted bars fetcher,
            # never a price supplied by the order caller.
            if position_cap_rule is None:
                return RuleOutcome(True, "server-side target validation data is incomplete")
            reference = position_cap_rule._reference_notional(symbol, {"qty": 1.0})
            if not isinstance(reference, (int, float)) or isinstance(reference, bool):
                return RuleOutcome(True, "server-side target reference price is unavailable")
            price = float(reference)
        if not all(math.isfinite(value) and value > 0 for value in (price, equity)):
            return RuleOutcome(True, "server-side target validation data is invalid")
        if position_cap_rule is not None:
            server_cap_usd, _ = position_cap_rule._effective_cap(state)
        else:
            server_cap_usd = min(
                self.cfg.max_target_usd,
                equity * self.cfg.max_target_pct_of_equity,
            )
        server_max_target_qty = server_cap_usd / price
        if (
            not math.isfinite(target_qty)
            or target_qty < 0
            or target_qty > server_max_target_qty + 1e-9
        ):
            return RuleOutcome(
                True,
                f"caller target qty {target_qty:g} exceeds independently derived server maximum "
                f"{server_max_target_qty:g} for {symbol}",
            )
        committed = current_qty + pending_qty
        capacity = max(0.0, target_qty - committed) if side == "buy" else max(0.0, committed - target_qty)
        if side not in {"buy", "sell"} or qty <= 0:
            return RuleOutcome(True, "invalid side or quantity for exposure reconciliation")
        if qty > capacity + 1e-9:
            return RuleOutcome(
                True,
                f"{symbol} {side} qty {qty:g} exceeds reconciled remaining capacity {capacity:g} "
                f"(current {current_qty:g}, pending signed {pending_qty:g}, target {target_qty:g})",
            )
        return RuleOutcome(False)
