"""Parse Alpaca MCP order responses without confusing submission with fills."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_KNOWN_STATUSES = {
    "accepted", "accepted_for_bidding", "calculated", "canceled", "done_for_day",
    "expired", "filled", "held", "new", "partially_filled", "pending_cancel",
    "pending_new", "pending_replace", "rejected", "replaced", "stopped", "suspended",
    "dry_run",
}
TERMINAL_STATUSES = {"canceled", "done_for_day", "expired", "filled", "rejected", "stopped"}
DEFAULT_LIFECYCLE_FILE = Path(__file__).resolve().parent.parent / "data" / "order_lifecycle.json"


@dataclass(frozen=True)
class BrokerOrderReceipt:
    order_id: str | None
    client_order_id: str | None
    status: str
    submitted: bool
    filled: bool
    filled_qty: float | None = None
    filled_avg_price: float | None = None
    raw_parseable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LifecycleJournal:
    """Atomic latest-state journal keyed by idempotent client order ID."""

    def __init__(self, path: Path | str = DEFAULT_LIFECYCLE_FILE) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def record(self, receipt: BrokerOrderReceipt, **context: Any) -> None:
        key = receipt.client_order_id
        if not key:
            return
        payload = self.load()
        previous = payload.get(key, {})
        history = list(previous.get("history", []))
        transition = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": receipt.status,
            "filled_qty": receipt.filled_qty,
            "filled_avg_price": receipt.filled_avg_price,
        }
        if not history or history[-1].get("status") != receipt.status:
            history.append(transition)
        payload[key] = {
            **previous,
            **context,
            "client_order_id": key,
            "order_id": receipt.order_id or previous.get("order_id"),
            "status": receipt.status,
            "terminal": receipt.status in TERMINAL_STATUSES or receipt.status == "dry_run",
            "submitted": receipt.submitted,
            "updated_at": transition["timestamp"],
            "history": history,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def unresolved(self, client_order_id: str) -> bool:
        item = self.load().get(client_order_id)
        return bool(item and item.get("submitted") and not item.get("terminal"))


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _find_order(payload: Any) -> dict[str, Any] | None:
    """Find an order object through common MCP/security wrapper shapes."""
    if isinstance(payload, str):
        try:
            return _find_order(json.loads(payload))
        except (json.JSONDecodeError, TypeError):
            return None
    if isinstance(payload, list):
        for item in payload:
            found = _find_order(item)
            if found:
                return found
        return None
    if not isinstance(payload, dict):
        return None
    raw_id = payload.get("id") or payload.get("order_id")
    status = payload.get("status")
    if raw_id is not None or (isinstance(status, str) and status.lower() in _KNOWN_STATUSES):
        return payload
    for key in ("data", "result", "order", "response"):
        if key in payload:
            found = _find_order(payload[key])
            if found:
                return found
    return None


def parse_broker_order_result(result: Any) -> BrokerOrderReceipt:
    """Parse a ToolResult. Unknown means submitted, never falsely filled."""
    if getattr(result, "is_error", False):
        return BrokerOrderReceipt(None, None, "upstream_error", False, False)
    content = getattr(result, "content", None) or []
    text = getattr(content[0], "text", None) if content else None
    order = _find_order(text)
    if order is None:
        return BrokerOrderReceipt(None, None, "submitted_unconfirmed", True, False)
    order_id = order.get("id") or order.get("order_id")
    client_order_id = order.get("client_order_id")
    status = str(order.get("status") or "submitted_unconfirmed").strip().lower()
    return BrokerOrderReceipt(
        str(order_id) if order_id else None,
        str(client_order_id) if client_order_id else None,
        status,
        status != "dry_run",
        status == "filled",
        _number(order.get("filled_qty")),
        _number(order.get("filled_avg_price")),
        True,
    )


async def reconcile_broker_order(client: Any, receipt: BrokerOrderReceipt) -> BrokerOrderReceipt:
    """Fetch the broker's latest state once; preserve submission evidence on failure."""
    if receipt.order_id:
        tool_name = "get_order_by_id"
        arguments = {"order_id": receipt.order_id}
    elif receipt.client_order_id:
        tool_name = "get_order_by_client_id"
        arguments = {"client_order_id": receipt.client_order_id}
    else:
        return receipt
    try:
        result = await client.call_tool(
            tool_name, arguments, raise_on_error=False
        )
        refreshed = parse_broker_order_result(result)
        return refreshed if refreshed.raw_parseable else receipt
    except Exception:
        return receipt


async def poll_broker_order_terminal(
    client: Any,
    receipt: BrokerOrderReceipt,
    *,
    max_attempts: int = 3,
    poll_interval_seconds: float = 1.0,
    journal: LifecycleJournal | None = None,
    context: dict[str, Any] | None = None,
) -> BrokerOrderReceipt:
    """Bounded lifecycle polling; a timeout remains honestly non-terminal."""
    current = receipt
    if journal:
        journal.record(current, **(context or {}))
    if current.status in TERMINAL_STATUSES or not current.submitted:
        return current
    for attempt in range(max(0, max_attempts)):
        if attempt and poll_interval_seconds > 0:
            await asyncio.sleep(poll_interval_seconds)
        refreshed = await reconcile_broker_order(client, current)
        current = refreshed
        if journal:
            journal.record(current, **(context or {}))
        if current.status in TERMINAL_STATUSES:
            break
    return current


async def recover_pending_order_events(
    client: Any,
    events: list[Any],
    *,
    journal: LifecycleJournal,
    max_attempts: int = 3,
    poll_interval_seconds: float = 1.0,
) -> list[BrokerOrderReceipt]:
    """Reconcile crash-orphaned order calls without placing replacements."""
    recovered: list[BrokerOrderReceipt] = []
    for event in events:
        arguments = getattr(event, "arguments", {}) or {}
        client_id = arguments.get("client_order_id")
        tool_name = str(getattr(event, "tool_name", ""))
        if not client_id or not tool_name.startswith("place_"):
            continue
        initial = BrokerOrderReceipt(
            order_id=None,
            client_order_id=str(client_id),
            status="submitted_unconfirmed",
            submitted=True,
            filled=False,
        )
        final = await poll_broker_order_terminal(
            client,
            initial,
            max_attempts=max_attempts,
            poll_interval_seconds=poll_interval_seconds,
            journal=journal,
            context={
                "recovered_from_call_id": getattr(event, "call_id", None),
                "symbol": arguments.get("symbol") or arguments.get("symbol_or_asset_id"),
                "side": arguments.get("side"),
                "quantity": arguments.get("qty") or arguments.get("quantity"),
            },
        )
        recovered.append(final)
    return recovered
