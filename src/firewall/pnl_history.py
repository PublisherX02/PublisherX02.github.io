"""PnLHistory — rolling record of realized P&L events for the session.

Stateful/windowed rules (cooldown_after_loss) read from this to detect a
loss concentrated within a rolling window, as opposed to a single
point-in-time cumulative session PnL snapshot (see
firewall.rules.drawdown_killswitch, which uses the latter).

Recording is the caller's responsibility -- typically the proxy, once a
fill's realized P&L becomes known. Rules only ever read from a PnLHistory;
they never write to it.
"""

from __future__ import annotations

import dataclasses
from collections import deque
from typing import Iterator


@dataclasses.dataclass(frozen=True)
class RealizedPnLEvent:
    """One realized P&L event: a fill (or other close-out) resolving to a
    dollar gain (positive) or loss (negative) at a point in time."""

    timestamp: float
    pnl_usd: float


class PnLHistory:
    """A rolling, fixed-capacity deque of RealizedPnLEvents for one session."""

    def __init__(self, maxlen: int = 10_000) -> None:
        self._events: deque[RealizedPnLEvent] = deque(maxlen=maxlen)

    def record(self, *, timestamp: float, pnl_usd: float) -> RealizedPnLEvent:
        event = RealizedPnLEvent(timestamp=timestamp, pnl_usd=pnl_usd)
        self._events.append(event)
        return event

    def since(self, now: float, window_seconds: float) -> list[RealizedPnLEvent]:
        """Events with timestamp within `window_seconds` of `now`."""
        cutoff = now - window_seconds
        return [e for e in self._events if e.timestamp >= cutoff]

    def __iter__(self) -> Iterator[RealizedPnLEvent]:
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)
