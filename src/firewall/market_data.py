"""market_data — shared market-data fetchers for rules that need it
(cvar_gate/pct_of_adv need historical daily bars; option_spread_guard
needs a live option quote), so multiple rules checking the same
symbol don't each make a redundant network round trip.

Both `fetch_daily_bars` and `fetch_option_latest_quote` never raise. Any
failure -- timeout, auth, malformed response, empty/absent result -- comes
back as a `*Result(ok=False, reason=...)` dataclass rather than
propagating, because it's a hard requirement of the rules that consume
this module that they fail closed on bad market data rather than crash or
silently skip their check.

In-memory caches, kept separate (`_cache` for bars, `_option_quote_cache`
for quotes) with deliberately different TTLs -- see
`DEFAULT_OPTION_QUOTE_CACHE_TTL_SECONDS`'s own comment for why a live
bid/ask spread cannot reuse the 30-minute bars TTL. The bars cache is
keyed by `(symbol, lookback_days)`, not by symbol alone: two rules can
legitimately request different lookback windows for the same symbol
(cvar_gate's `cvar_lookback_days` vs. pct_of_adv's `adv_lookback_days`);
keying by symbol alone would let one rule's cached window silently leak
into the other's calculation. This still eliminates the redundant-fetch
problem whenever two rules do share a lookback window, which is the
common case. Only successful fetches are cached in either cache -- a
failure is not remembered, so a transient market-data outage doesn't keep
every rule failing closed for the full TTL after the API recovers.
"""

from __future__ import annotations

import json
import math
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
_ALPACA_OPTION_SNAPSHOT_URL = "https://data.alpaca.markets/v1beta1/options/snapshots"

DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_CACHE_TTL_SECONDS = 1800.0  # 30 minutes
# Much shorter than bars' 30-minute TTL, deliberately: a bid/ask spread is
# second-to-second-volatile in a way a daily close/volume figure is not.
# This only guards against redundant back-to-back calls for the same
# contract within the same short burst (e.g. a rapid retry), not a
# meaningful staleness window for the spread itself.
DEFAULT_OPTION_QUOTE_CACHE_TTL_SECONDS = 5.0
# Verified against the real OpenAPI spec (alpaca-mcp-server's bundled
# specs/market-data-api.json, `option_feed` schema, checked 2026-08-25):
# the API's own default is "opra" (the paid official OPRA feed). This
# project uses Alpaca's free/Basic market data plan (same constraint
# documented in pct_of_adv.py's module docstring for stocks/IEX), which
# does not carry an OPRA subscription -- "indicative" is the free-tier
# feed ("a free indicative feed where trades are delayed and quotes are
# modified", per the spec's own field description). Requesting "opra"
# without a subscription would fail outright; "indicative" is what this
# project can actually call. The delayed/modified caveat means the
# spread this rule computes is an approximation of the true live spread,
# not a real-time OPRA read -- same "bias toward stricter, never looser"
# framing pct_of_adv already uses for its own free-tier caveat.
DEFAULT_OPTION_FEED = "indicative"
# Empirically verified (2026-08-28, real account, real 403 body): calling
# GET /v2/stocks/{symbol}/bars with no `feed` param at all defaults to
# "sip" (the paid consolidated feed) and this free/Basic-plan account gets
# `403 {"message":"subscription does not permit querying recent SIP
# data"}` for any window that includes recent days -- contradicting this
# module's own prior claim (still visible in pct_of_adv.py's docstring)
# that "historical bars are unrestricted past a 15-minute delay." That
# claim was never checked against a real 403 body; this constant and its
# use in `_fetch_from_alpaca` closes the gap the same way
# `DEFAULT_OPTION_FEED` already closes it for option snapshots -- "iex" is
# the free-tier feed this plan is actually entitled to query.
DEFAULT_STOCK_FEED = "iex"


class DailyBar(NamedTuple):
    close: float
    volume: float


class OptionQuote(NamedTuple):
    bid: float
    ask: float
    # Black-Scholes delta from the same snapshot response's `greeks` field
    # (verified against the real OpenAPI spec's `option_snapshot` schema).
    # `greeks` is NOT a required field of `option_snapshot` -- unlike
    # `bid`/`ask` (which fail the whole quote if missing), a snapshot with
    # no greeks at all is common enough (see fetch_option_latest_quote's
    # docstring) that delta is Optional here rather than failing the
    # entire quote fetch for rules (option_spread_guard) that never touch
    # it. Defaults to None so existing `OptionQuote(bid=.., ask=..)`
    # call sites remain valid.
    delta: float | None = None
    # Implied volatility (Black-Scholes) from the same snapshot response's
    # top-level `impliedVolatility` field -- a sibling of `greeks`/
    # `latestQuote`, NOT nested inside `greeks` (verified against the real
    # OpenAPI spec's `option_snapshot` schema). Same optional-field
    # leniency as delta: a snapshot with no `impliedVolatility` at all, or
    # one that doesn't parse, still returns `ok=True` with `quote.iv=None`
    # rather than failing the whole quote for rules that never touch it.
    iv: float | None = None


@dataclass
class OptionQuoteResult:
    ok: bool
    quote: OptionQuote | None = None
    reason: str | None = None


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


def _fetch_from_alpaca(
    symbol: str, lookback_days: int, timeout_seconds: float, feed: str = DEFAULT_STOCK_FEED
) -> list[DailyBar]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    params = {
        "timeframe": "1Day",
        "start": start.date().isoformat(),
        "end": end.date().isoformat(),
        "limit": "10000",
        "adjustment": "all",
        "feed": feed,
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
    feed: str = DEFAULT_STOCK_FEED,
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
        bars = _fetch_from_alpaca(symbol, lookback_days, timeout_seconds, feed)
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


@dataclass
class _OptionQuoteCacheEntry:
    result: OptionQuoteResult
    fetched_at: float


_option_quote_cache: dict[str, _OptionQuoteCacheEntry] = {}


def _fetch_option_quote_from_alpaca(
    symbol: str, timeout_seconds: float, feed: str
) -> dict:
    params = {"symbols": symbol, "feed": feed}
    url = _ALPACA_OPTION_SNAPSHOT_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={
            "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", ""),
            "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read())


def fetch_option_latest_quote(
    symbol: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    cache_ttl_seconds: float = DEFAULT_OPTION_QUOTE_CACHE_TTL_SECONDS,
    feed: str = DEFAULT_OPTION_FEED,
) -> OptionQuoteResult:
    """The latest bid/ask (and, when available, Black-Scholes delta and
    implied volatility) for one OCC-format option contract symbol, from
    `GET /v1beta1/options/snapshots?symbols=<symbol>&feed=<feed>`'s
    `snapshots[symbol]` (`.latestQuote` for bid/ask, `.greeks.delta` for
    delta, `.impliedVolatility` for IV -- a sibling field of `latestQuote`/
    `greeks`, not nested inside either -- verified against the real
    OpenAPI spec, see module docstring's `DEFAULT_OPTION_FEED` comment).
    Never raises -- any failure (timeout, malformed response, symbol
    absent from the response, `latestQuote` absent for that symbol, a
    non-positive ask) comes back as `OptionQuoteResult(ok=False,
    reason=...)`, matching `fetch_daily_bars`'s contract. This is the
    single fetch shared by every rule that needs an option quote/delta/IV
    (`option_spread_guard`, `net_delta_floor`, `iv_hv_ratio`) -- one HTTP
    call and one cache entry per symbol, not a separate round trip per
    rule.

    `latestQuote` is not a required field of Alpaca's own `option_snapshot`
    schema -- a contract with no recent quote activity can have a snapshot
    with no `latestQuote` at all, which this treats as unavailable (`ok=
    False`), not as a malformed response. `greeks` and `impliedVolatility`
    are separately optional and handled more leniently: a snapshot with no
    `greeks`/`impliedVolatility` at all, or values that don't parse, still
    returns `ok=True` with `quote.delta=None`/`quote.iv=None` -- rules that
    only need bid/ask (`option_spread_guard`) must not be blocked by a
    missing delta/IV they never asked for; only a rule that specifically
    needs one (`net_delta_floor` for delta, `iv_hv_ratio` for IV) treats
    its own `None` case as a failure to assess.
    """
    now = time.monotonic()
    cached = _option_quote_cache.get(symbol)
    if cached is not None and (now - cached.fetched_at) < cache_ttl_seconds:
        return cached.result

    try:
        payload = _fetch_option_quote_from_alpaca(symbol, timeout_seconds, feed)
    except (socket.timeout, TimeoutError):
        return OptionQuoteResult(
            ok=False,
            reason=f"timed out after {timeout_seconds}s fetching option quote for {symbol}",
        )
    except urllib.error.HTTPError as exc:
        return OptionQuoteResult(
            ok=False,
            reason=f"HTTP {exc.code} fetching option quote for {symbol}: {exc.reason}",
        )
    except urllib.error.URLError as exc:
        return OptionQuoteResult(
            ok=False,
            reason=f"network error fetching option quote for {symbol}: {exc.reason}",
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return OptionQuoteResult(
            ok=False,
            reason=f"malformed market data response for {symbol}: {exc}",
        )
    except Exception as exc:  # never let a fetch failure escape as an exception
        return OptionQuoteResult(
            ok=False,
            reason=f"unexpected error fetching option quote for {symbol}: {exc}",
        )

    snapshot = (payload.get("snapshots") or {}).get(symbol)
    if snapshot is None:
        return OptionQuoteResult(
            ok=False,
            reason=f"no snapshot returned for {symbol}",
        )

    latest_quote = snapshot.get("latestQuote")
    if latest_quote is None:
        return OptionQuoteResult(
            ok=False,
            reason=f"snapshot for {symbol} has no latestQuote (no recent quote activity)",
        )

    try:
        bid = float(latest_quote["bp"])
        ask = float(latest_quote["ap"])
    except (KeyError, TypeError, ValueError) as exc:
        return OptionQuoteResult(
            ok=False,
            reason=f"malformed latestQuote for {symbol}: {exc}",
        )

    if ask <= 0 or bid < 0 or not math.isfinite(ask) or not math.isfinite(bid):
        return OptionQuoteResult(
            ok=False,
            reason=f"non-usable quote for {symbol} (bid={bid}, ask={ask})",
        )

    delta: float | None = None
    greeks = snapshot.get("greeks")
    if greeks is not None:
        try:
            parsed_delta = float(greeks["delta"])
        except (KeyError, TypeError, ValueError):
            parsed_delta = None
        if parsed_delta is not None and math.isfinite(parsed_delta):
            delta = parsed_delta

    iv: float | None = None
    try:
        parsed_iv = float(snapshot["impliedVolatility"])
    except (KeyError, TypeError, ValueError):
        parsed_iv = None
    if parsed_iv is not None and math.isfinite(parsed_iv):
        iv = parsed_iv

    result = OptionQuoteResult(
        ok=True, quote=OptionQuote(bid=bid, ask=ask, delta=delta, iv=iv)
    )
    _option_quote_cache[symbol] = _OptionQuoteCacheEntry(result=result, fetched_at=now)
    return result
