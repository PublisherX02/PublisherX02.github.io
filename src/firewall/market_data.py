"""market_data — shared historical daily-bars fetcher for rules that need
OHLCV data (cvar_gate, pct_of_adv), so multiple rules checking the same
symbol don't each make a redundant network round trip.

`fetch_daily_bars` never raises. Any failure -- timeout, auth, malformed
response, empty result -- comes back as `BarsResult(ok=False, reason=...)`
rather than propagating, because it's a hard requirement of the rules that
consume this module (cvar_gate, pct_of_adv) that they fail closed on bad
market data rather than crash or silently skip their check.

In-memory cache: keyed by `(symbol, lookback_days)`, not by symbol alone.
Two rules can legitimately request different lookback windows for the same
symbol (cvar_gate's `cvar_lookback_days` vs. pct_of_adv's
`adv_lookback_days`); keying by symbol alone would let one rule's cached
window silently leak into the other's calculation. This still eliminates
the redundant-fetch problem whenever two rules do share a lookback window,
which is the common case. Only successful fetches are cached -- a failure
is not remembered, so a transient market-data outage doesn't keep every
rule failing closed for the full TTL after the API recovers.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

_ALPACA_DATA_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"

DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_CACHE_TTL_SECONDS = 1800.0  # 30 minutes


class DailyBar(NamedTuple):
    close: float
    volume: float


@dataclass
class BarsResult:
    ok: bool
    bars: list[DailyBar] = field(default_factory=list)
    reason: str | None = None

    @property
    def closes(self) -> list[float]:
        return [bar.close for bar in self.bars]

    @property
    def volumes(self) -> list[float]:
        return [bar.volume for bar in self.bars]


@dataclass
class _CacheEntry:
    result: BarsResult
    fetched_at: float


_cache: dict[tuple[str, int], _CacheEntry] = {}


def _fetch_from_alpaca(symbol: str, lookback_days: int, timeout_seconds: float) -> list[DailyBar]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    params = {
        "timeframe": "1Day",
        "start": start.date().isoformat(),
        "end": end.date().isoformat(),
        "limit": "10000",
        "adjustment": "all",
    }
    url = _ALPACA_DATA_URL.format(symbol=symbol) + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={
            "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", ""),
            "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = json.loads(response.read())

    bars = payload.get("bars") or []
    return [DailyBar(close=float(bar["c"]), volume=float(bar.get("v", 0.0))) for bar in bars]


def fetch_daily_bars(
    symbol: str,
    lookback_days: int,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
) -> BarsResult:
    """Daily OHLCV bars for `symbol` over the last `lookback_days` calendar
    days, oldest first. Never raises -- see module docstring.
    """
    cache_key = (symbol, lookback_days)
    now = time.monotonic()
    cached = _cache.get(cache_key)
    if cached is not None and (now - cached.fetched_at) < cache_ttl_seconds:
        return cached.result

    try:
        bars = _fetch_from_alpaca(symbol, lookback_days, timeout_seconds)
    except (socket.timeout, TimeoutError):
        return BarsResult(
            ok=False,
            reason=f"timed out after {timeout_seconds}s fetching bars for {symbol}",
        )
    except urllib.error.HTTPError as exc:
        return BarsResult(
            ok=False,
            reason=f"HTTP {exc.code} fetching bars for {symbol}: {exc.reason}",
        )
    except urllib.error.URLError as exc:
        return BarsResult(
            ok=False,
            reason=f"network error fetching bars for {symbol}: {exc.reason}",
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return BarsResult(
            ok=False,
            reason=f"malformed market data response for {symbol}: {exc}",
        )
    except Exception as exc:  # never let a fetch failure escape as an exception
        return BarsResult(
            ok=False,
            reason=f"unexpected error fetching bars for {symbol}: {exc}",
        )

    if not bars:
        return BarsResult(
            ok=False,
            reason=f"empty bars response for {symbol} over {lookback_days}-day lookback",
        )

    result = BarsResult(ok=True, bars=bars)
    _cache[cache_key] = _CacheEntry(result=result, fetched_at=now)
    return result
