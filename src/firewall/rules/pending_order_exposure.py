"""Fail-closed cross-cycle pending-order exposure enforcement."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from firewall.rules.base import Rule, RuleConfig, RuleOutcome


class _Params(BaseModel):
    place_tool_match: list[str] = ["place_stock_order"]
    snapshot_state_key: str = "exposure_snapshot"
    metadata_field: str = "_firewall_reconciliation"


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
        expected_fingerprint = str(metadata.get("snapshot_fingerprint") or "")
        if not expected_fingerprint or expected_fingerprint != snapshot.get("fingerprint"):
            return RuleOutcome(True, "position/open-order snapshot changed before submission")
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
