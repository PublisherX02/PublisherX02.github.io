"""OrderHistory — rolling record of an agent's order-related tool calls for
the session.

Stateful/windowed rules (order_rate_throttle, place_cancel_ratio,
layering_detector, wash_trade_detector) read from this to detect abusive
patterns across multiple calls, not just the one being evaluated.

Recording is the caller's responsibility -- typically the proxy, once a
call completes or its outcome (fill/cancel) becomes known. Rules only ever
read from an OrderHistory; they never write to it.
"""

from __future__ import annotations

import dataclasses
from collections import deque
from typing import Iterator


@dataclasses.dataclass(frozen=True)
class OrderEvent:
    """One recorded order-related call: its timestamp, what it was, and
    (once known) its outcome."""

    timestamp: float
    tool: str
    symbol: str
    side: str
    qty: float
    price: float | None
    order_id: str
    outcome: str  # "open" | "filled" | "cancelled"


class OrderHistory:
    """A rolling, fixed-capacity deque of OrderEvents for one session."""

    def __init__(self, maxlen: int = 10_000) -> None:
        self._events: deque[OrderEvent] = deque(maxlen=maxlen)

    def record(
        self,
        *,
        timestamp: float,
        tool: str,
        symbol: str,
        side: str,
        qty: float,
        price: float | None,
        order_id: str,
        outcome: str,
    ) -> OrderEvent:
        event = OrderEvent(
            timestamp=timestamp,
            tool=tool,
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            order_id=order_id,
            outcome=outcome,
        )
        self._events.append(event)
        return event

    def update_outcome(self, order_id: str, outcome: str) -> None:
        """Update a previously recorded order's outcome in place (e.g. once
        a fill or cancel confirmation comes back from upstream). No-op if
        the order_id isn't found (e.g. it has aged out of the deque)."""
        for index, event in enumerate(self._events):
            if event.order_id == order_id:
                self._events[index] = dataclasses.replace(event, outcome=outcome)
                return

    def since(self, now: float, window_seconds: float) -> list[OrderEvent]:
        """Events with timestamp within `window_seconds` of `now`."""
        cutoff = now - window_seconds
        return [e for e in self._events if e.timestamp >= cutoff]

    def __iter__(self) -> Iterator[OrderEvent]:
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)
