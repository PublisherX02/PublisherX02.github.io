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
_ALPACA_LATEST_TRADE_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest"
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
class LatestPriceResult:
    ok: bool
    price: float | None = None
    reason: str | None = None


def fetch_stock_latest_price(
    symbol: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    feed: str = DEFAULT_STOCK_FEED,
) -> LatestPriceResult:
    """Fetch a fresh latest-trade estimate for equity exposure sizing.

    This deliberately has no cache: it is the last-resort mark for an accepted,
    unfilled market order that has neither a limit nor fill price. The returned
    value is an exposure estimate, never represented as a confirmed fill.
    """
    url = _ALPACA_LATEST_TRADE_URL.format(symbol=symbol) + "?" + urllib.parse.urlencode(
        {"feed": feed}
    )
    request = urllib.request.Request(
        url,
        headers={
            "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", ""),
            "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read())
        price = float((payload.get("trade") or {})["p"])
        if not math.isfinite(price) or price <= 0:
            raise ValueError(f"invalid latest trade price {price!r}")
        return LatestPriceResult(ok=True, price=price)
    except (socket.timeout, TimeoutError):
        return LatestPriceResult(ok=False, reason=f"timed out fetching latest trade for {symbol}")
    except urllib.error.HTTPError as exc:
        return LatestPriceResult(ok=False, reason=f"HTTP {exc.code} fetching latest trade for {symbol}: {exc.reason}")
    except urllib.error.URLError as exc:
        return LatestPriceResult(ok=False, reason=f"network error fetching latest trade for {symbol}: {exc.reason}")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return LatestPriceResult(ok=False, reason=f"malformed latest trade for {symbol}: {exc}")
    except Exception as exc:
        return LatestPriceResult(ok=False, reason=f"unexpected error fetching latest trade for {symbol}: {exc}")


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


def _parse_option_snapshot(symbol: str, snapshot: dict | None) -> OptionQuoteResult:
    """Parse one symbol's `snapshots[symbol]` entry from a
    `/v1beta1/options/snapshots` response into an `OptionQuoteResult` --
    the shared parsing logic behind both `fetch_option_latest_quote`
    (single-symbol) and `fetch_option_quotes` (batched): a snapshot's
    shape doesn't depend on whether it arrived alongside 0 or 99 other
    symbols in the same response, so there is exactly one parser, not one
    per call shape. See `fetch_option_latest_quote`'s own docstring for
    the `latestQuote`-required/`greeks`+`impliedVolatility`-optional
    leniency rules this implements."""
    if snapshot is None:
        return OptionQuoteResult(ok=False, reason=f"no snapshot returned for {symbol}")

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

    return OptionQuoteResult(ok=True, quote=OptionQuote(bid=bid, ask=ask, delta=delta, iv=iv))


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
    rule. `resolve_listed_contract`'s delta-based strike selection uses
    the batched sibling `fetch_option_quotes` instead, sharing this same
    cache and the same `_parse_option_snapshot` parser.

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

    result = _parse_option_snapshot(symbol, (payload.get("snapshots") or {}).get(symbol))
    if result.ok:
        _option_quote_cache[symbol] = _OptionQuoteCacheEntry(result=result, fetched_at=now)
    return result


def _fetch_option_quotes_from_alpaca(
    symbols: list[str], timeout_seconds: float, feed: str
) -> dict:
    params = {"symbols": ",".join(symbols), "feed": feed}
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


# Verified against the OpenAPI spec's `symbols` parameter description: "A
# comma-separated list of contract symbols with a limit of 100." Chunking
# below is not a defensive guess -- a single expiry's full strike chain
# for a wide, liquid underlying (e.g. SPY, $1-wide strikes across a broad
# price range) can genuinely exceed 100 contracts.
_MAX_SYMBOLS_PER_SNAPSHOT_REQUEST = 100


def fetch_option_quotes(
    symbols: list[str],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    cache_ttl_seconds: float = DEFAULT_OPTION_QUOTE_CACHE_TTL_SECONDS,
    feed: str = DEFAULT_OPTION_FEED,
) -> dict[str, OptionQuoteResult]:
    """Batched sibling of `fetch_option_latest_quote`: ONE HTTP round trip
    (or, above `_MAX_SYMBOLS_PER_SNAPSHOT_REQUEST`, the minimum number of
    chunked round trips) for multiple contract symbols, instead of one
    round trip per contract -- what `resolve_listed_contract`'s delta-based
    strike selection needs to compare several listed strikes' deltas
    without a fetch per strike. Shares the SAME per-symbol cache
    (`_option_quote_cache`) and the SAME `_parse_option_snapshot` parser as
    `fetch_option_latest_quote`: a symbol already cached from a prior
    single-symbol fetch is skipped here, and a symbol fetched here becomes
    a cache hit for a later single-symbol call.

    Never raises. A request-level failure (timeout/HTTP/network/malformed
    JSON for one chunk) returns `ok=False` for every symbol in that chunk
    with the SAME reason (the failure was request-level, not per-symbol);
    an individual symbol simply absent from a successful response's
    `snapshots` is `ok=False` for that symbol alone, via the same
    `_parse_option_snapshot` a single-symbol fetch would apply.
    """
    if not symbols:
        return {}

    results: dict[str, OptionQuoteResult] = {}
    to_fetch: list[str] = []
    now = time.monotonic()
    for symbol in symbols:
        cached = _option_quote_cache.get(symbol)
        if cached is not None and (now - cached.fetched_at) < cache_ttl_seconds:
            results[symbol] = cached.result
        else:
            to_fetch.append(symbol)

    for start in range(0, len(to_fetch), _MAX_SYMBOLS_PER_SNAPSHOT_REQUEST):
        chunk = to_fetch[start : start + _MAX_SYMBOLS_PER_SNAPSHOT_REQUEST]
        try:
            payload = _fetch_option_quotes_from_alpaca(chunk, timeout_seconds, feed)
        except (socket.timeout, TimeoutError):
            reason = (
                f"timed out after {timeout_seconds}s fetching option quotes "
                f"for {len(chunk)} symbol(s)"
            )
            for symbol in chunk:
                results[symbol] = OptionQuoteResult(ok=False, reason=reason)
            continue
        except urllib.error.HTTPError as exc:
            reason = f"HTTP {exc.code} fetching option quotes: {exc.reason}"
            for symbol in chunk:
                results[symbol] = OptionQuoteResult(ok=False, reason=reason)
            continue
        except urllib.error.URLError as exc:
            reason = f"network error fetching option quotes: {exc.reason}"
            for symbol in chunk:
                results[symbol] = OptionQuoteResult(ok=False, reason=reason)
            continue
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            reason = f"malformed market data response fetching option quotes: {exc}"
            for symbol in chunk:
                results[symbol] = OptionQuoteResult(ok=False, reason=reason)
            continue
        except Exception as exc:  # never let a fetch failure escape as an exception
            reason = f"unexpected error fetching option quotes: {exc}"
            for symbol in chunk:
                results[symbol] = OptionQuoteResult(ok=False, reason=reason)
            continue

        snapshots = payload.get("snapshots") or {}
        for symbol in chunk:
            result = _parse_option_snapshot(symbol, snapshots.get(symbol))
            results[symbol] = result
            if result.ok:
                _option_quote_cache[symbol] = _OptionQuoteCacheEntry(
                    result=result, fetched_at=now
                )

    return results


# Trading API, NOT Market Data API -- a DIFFERENT host from
# _ALPACA_OPTION_SNAPSHOT_URL/_ALPACA_DATA_URL (data.alpaca.markets) and a
# DIFFERENT surface (verified against alpaca-mcp-server's bundled
# specs/trading-api.json, `/v2/options/contracts`, checked 2026-08-29):
# this lists which contracts actually exist (symbol/strike/expiry
# metadata), and does NOT carry a `feed` parameter at all -- `feed` is a
# market-data subscription-tier concept (opra vs indicative) that only
# applies to live quote/snapshot/bar data, never to contract listings.
# Hardcoded to the Paper server deliberately, matching this project's own
# paper-only scope (see `firewall.proxy._require_paper_trade`) -- a
# live-trading build would need `https://api.alpaca.markets` instead.
_ALPACA_OPTION_CONTRACTS_URL = "https://paper-api.alpaca.markets/v2/options/contracts"

# Which strikes/expiries Alpaca lists for an underlying changes on the
# order of days (new expiries added, old ones rolling off), not seconds --
# reuses the same 30-minute window as daily bars, not
# DEFAULT_OPTION_QUOTE_CACHE_TTL_SECONDS's 5-second window, which exists
# only for a live bid/ask spread.
DEFAULT_CONTRACT_CACHE_TTL_SECONDS = DEFAULT_CACHE_TTL_SECONDS

# OCC single-letter convention (matching `format_occ_symbol`'s own
# `option_type` parameter) mapped to the Trading API's `type` query enum
# (verified against trading-api.json's `OptionContractType` schema:
# "call"/"put", not single letters).
_OCC_TYPE_TO_ALPACA = {"P": "put", "C": "call"}

# The REAL enforced lower bound: net_delta_floor's own structural_delta_floor
# (policies/default.yaml), the raw per-contract |delta| below which that
# rule hard-blocks regardless of anything resolve_listed_contract does.
DELTA_CORRIDOR_FLOOR = 0.15
# NOT itself enforced by any rule anywhere in policy -- a disclosed upper
# anchor for "reasonable OTM protective put" selection (standard hedging
# practice keeps a protective put's delta roughly in this band; a
# near-ATM/high-delta put is expensive and barely different from owning
# fewer shares, a very-low-delta one is what structural_delta_floor exists
# to reject). Stated as a resolver-side selection preference, never
# claimed as a policy-configured ceiling.
DELTA_CORRIDOR_CEILING = 0.50
DELTA_CORRIDOR_CENTER = (DELTA_CORRIDOR_FLOOR + DELTA_CORRIDOR_CEILING) / 2  # 0.325

# STARTING size of the price-ranked delta search window, not a hard cap --
# resolve_listed_contract widens it (doubling, re-using price_ranked's
# fixed ranking so no already-queried strike is re-fetched) whenever the
# searched window's |delta| values don't yet straddle
# DELTA_CORRIDOR_CENTER on both sides, up to the full listed chain at that
# expiry. This starting value only controls how much gets queried in the
# common case where the center-delta strike is close in price to the
# otm_pct anchor -- it can never cause settling for a worse pick than a
# wider start would, only a possibly-larger (still bounded, still cached)
# first request.
DEFAULT_STRIKE_SEARCH_COUNT = 20


@dataclass
class ResolvedContract:
    """A real, listed option contract Alpaca actually offers, resolved
    from the real chain -- not asserted from arithmetic. `occ_symbol` is
    copied verbatim from Alpaca's own `symbol` field in the contracts
    response, never reconstructed via `format_occ_symbol`: using Alpaca's
    own string removes any risk of this module's OCC-formatting ever
    diverging from what Alpaca actually lists. `delta` is the raw,
    unscaled per-contract delta (matching `OptionQuote.delta`'s own
    convention, e.g. -0.32 for a put) this contract was actually selected
    on -- present whenever selection used delta (the normal case),
    `None` only if delta-based selection could not run at all (see
    `resolve_listed_contract`'s docstring)."""

    occ_symbol: str
    strike: float
    expiry: str  # ISO date -- a REAL listed expiry, not a target
    delta: float | None = None


@dataclass
class ContractResolutionResult:
    ok: bool
    contract: ResolvedContract | None = None
    reason: str | None = None


@dataclass
class _ContractsCacheEntry:
    contracts: list[dict]
    fetched_at: float


_contracts_cache: dict[tuple[str, str, str, str], _ContractsCacheEntry] = {}


def _fetch_option_contracts_from_alpaca(
    underlying_symbol: str,
    alpaca_type: str,
    expiration_date_gte: str,
    expiration_date_lte: str,
    timeout_seconds: float,
) -> list[dict]:
    params = {
        "underlying_symbols": underlying_symbol,
        "status": "active",
        "type": alpaca_type,
        "expiration_date_gte": expiration_date_gte,
        "expiration_date_lte": expiration_date_lte,
        # A single underlying+type+~weeks-wide date window is always well
        # under this cap in practice (a few hundred strikes at most across
        # a handful of expiries) -- pagination via the response's
        # `next_page_token` is deliberately not implemented for this
        # narrow, disclosed use case.
        "limit": "10000",
    }
    url = _ALPACA_OPTION_CONTRACTS_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={
            "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", ""),
            "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = json.loads(response.read())
    return payload.get("option_contracts") or []


def resolve_listed_contract(
    underlying_symbol: str,
    target_strike: float,
    target_expiry: str,
    option_type: str,
    *,
    min_dte: int | None = None,
    search_window_days: int = 30,
    strike_search_count: int = DEFAULT_STRIKE_SEARCH_COUNT,
    now: float | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    cache_ttl_seconds: float = DEFAULT_CONTRACT_CACHE_TTL_SECONDS,
    feed: str = DEFAULT_OPTION_FEED,
) -> ContractResolutionResult:
    """Resolve a mechanically-computed (strike, expiry) TARGET to a REAL
    contract Alpaca actually lists -- the gap `format_occ_symbol` alone
    could never close: formatting a strike/expiry combination into an
    OCC-format string does not make it a contract that exists. Calls
    Alpaca's real options chain (`GET /v2/options/contracts`, Trading API
    -- see module-level `_ALPACA_OPTION_CONTRACTS_URL` comment for why
    this is a different host/surface from the quote/bars endpoints and
    carries no `feed` parameter) and picks, from what is ACTUALLY listed:

      1. The listed expiry nearest `target_expiry`, restricted to
         expiries whose calendar-day distance from `now` is >= `min_dte`
         when given -- the SAME calendar-day definition
         `option_expiry_floor` itself uses (see that rule's module
         docstring), so a resolved contract that rule would hard-block on
         arrival is never proposed in the first place. If the single
         nearest listed expiry violates the floor, this does NOT fall
         back to "next-nearest ignoring the floor" -- it snaps to the
         nearest listed expiry that ALSO satisfies the floor.
      2. Among that expiry's contracts, the strike whose DELTA lands
         closest to `DELTA_CORRIDOR_CENTER` (0.325 -- the midpoint of
         `net_delta_floor`'s real enforced `structural_delta_floor`, 0.15,
         and a disclosed, non-enforced upper anchor, 0.50) -- NOT the
         strike nearest `target_strike` by price. `target_strike` (from
         `otm_pct`, still fully mechanical) is used only as a rough price
         anchor for where to START looking: strikes at the chosen expiry
         are ranked once by price-proximity to it, and `strike_search_count`
         sets the STARTING window into that ranking whose real deltas get
         pulled via one batched `fetch_option_quotes` call (the SAME
         snapshot fetch `option_spread_guard`/`net_delta_floor`/
         `hedge_cost_cap` already use -- no second, competing delta
         source). If the searched window's |delta| values don't yet
         straddle the corridor center on both sides, the window doubles
         (re-querying only the newly-added strikes -- nothing already
         fetched is repeated) and searches again, up to the FULL listed
         chain at that expiry if needed -- a narrow window can otherwise
         silently settle for "closest available in an arbitrarily small
         slice" rather than the actual closest-to-center strike (verified
         against a real chain: a low-delta-per-dollar name can need a
         search window several times wider than a 20-strike default
         before the center-delta strike is even reachable). Selection by
         a measured Greek, not a market view -- still fully mechanical and
         disclosed, consistent with the rest of this corridor. A candidate
         with no usable quote/delta (e.g. no recent quote activity) is
         simply excluded from consideration, not treated as a fetch
         failure. The final pick must still clear `DELTA_CORRIDOR_FLOOR`
         (the one part of this that IS policy-enforced) -- if even the
         closest-to-center strike found after exhausting the full chain
         doesn't, resolution fails closed rather than proposing a contract
         `net_delta_floor` would hard-block on arrival; `DELTA_CORRIDOR_CEILING`
         is never enforced this way, since no rule blocks on |delta| being
         too high.

    Never raises -- any failure (timeout, malformed response, no listed
    expiry satisfying `min_dte` within the search window, no tradable
    contract found at all, no searched strike with a usable delta, or the
    best-available delta still below `DELTA_CORRIDOR_FLOOR`) comes back as
    `ContractResolutionResult(ok=False, reason=...)`, matching this
    module's `BarsResult`/`OptionQuoteResult` contract. `option_type` is
    OCC single-letter ("P"/"C"), matching `format_occ_symbol`'s own
    convention -- translated internally to the Trading API's "put"/"call".

    Only the raw chain fetch is cached (keyed by underlying_symbol,
    option_type, and the resolved date window), not the picked contract --
    two calls with different `target_strike`/`target_expiry` but an
    overlapping date window still share one HTTP round trip. Delta quotes
    fetched here also populate `fetch_option_latest_quote`'s own
    `_option_quote_cache` (see `fetch_option_quotes`), so the corridor
    rules' own quote fetch for the eventually-submitted order is often
    already warm.
    """
    alpaca_type = _OCC_TYPE_TO_ALPACA.get(option_type.upper())
    if alpaca_type is None:
        return ContractResolutionResult(
            ok=False,
            reason=f"unrecognized option_type {option_type!r} (expected 'P' or 'C')",
        )

    ts = now if now is not None else time.time()
    today = datetime.fromtimestamp(ts, tz=timezone.utc).date()
    try:
        target_expiry_date = datetime.fromisoformat(target_expiry).date()
    except ValueError as exc:
        return ContractResolutionResult(
            ok=False, reason=f"malformed target_expiry {target_expiry!r}: {exc}"
        )

    expiration_date_gte = today.isoformat()
    expiration_date_lte = (
        target_expiry_date + timedelta(days=search_window_days)
    ).isoformat()

    cache_key = (underlying_symbol, alpaca_type, expiration_date_gte, expiration_date_lte)
    monotonic_now = time.monotonic()
    cached = _contracts_cache.get(cache_key)
    if cached is not None and (monotonic_now - cached.fetched_at) < cache_ttl_seconds:
        contracts = cached.contracts
    else:
        try:
            contracts = _fetch_option_contracts_from_alpaca(
                underlying_symbol,
                alpaca_type,
                expiration_date_gte,
                expiration_date_lte,
                timeout_seconds,
            )
        except (socket.timeout, TimeoutError):
            return ContractResolutionResult(
                ok=False,
                reason=(
                    f"timed out after {timeout_seconds}s fetching option chain "
                    f"for {underlying_symbol}"
                ),
            )
        except urllib.error.HTTPError as exc:
            return ContractResolutionResult(
                ok=False,
                reason=(
                    f"HTTP {exc.code} fetching option chain for "
                    f"{underlying_symbol}: {exc.reason}"
                ),
            )
        except urllib.error.URLError as exc:
            return ContractResolutionResult(
                ok=False,
                reason=(
                    f"network error fetching option chain for "
                    f"{underlying_symbol}: {exc.reason}"
                ),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            return ContractResolutionResult(
                ok=False,
                reason=f"malformed option chain response for {underlying_symbol}: {exc}",
            )
        except Exception as exc:  # never let a fetch failure escape as an exception
            return ContractResolutionResult(
                ok=False,
                reason=f"unexpected error fetching option chain for {underlying_symbol}: {exc}",
            )
        _contracts_cache[cache_key] = _ContractsCacheEntry(
            contracts=contracts, fetched_at=monotonic_now
        )

    tradable = [c for c in contracts if c.get("tradable", True)]
    if not tradable:
        return ContractResolutionResult(
            ok=False,
            reason=(
                f"no tradable {alpaca_type} contracts listed for {underlying_symbol} "
                f"between {expiration_date_gte} and {expiration_date_lte}"
            ),
        )

    eligible_expiries: dict[str, list[dict]] = {}
    for contract in tradable:
        expiry = contract.get("expiration_date")
        if not expiry:
            continue
        try:
            expiry_date = datetime.fromisoformat(expiry).date()
        except ValueError:
            continue
        if min_dte is not None and (expiry_date - today).days < min_dte:
            continue
        eligible_expiries.setdefault(expiry, []).append(contract)

    if not eligible_expiries:
        reason = (
            f"no listed {alpaca_type} expiry for {underlying_symbol} satisfies the "
            f"{min_dte}-day DTE floor within {expiration_date_gte}..{expiration_date_lte}"
            if min_dte is not None
            else (
                f"no listed {alpaca_type} expiry found for {underlying_symbol} within "
                f"{expiration_date_gte}..{expiration_date_lte}"
            )
        )
        return ContractResolutionResult(ok=False, reason=reason)

    chosen_expiry = min(
        eligible_expiries,
        key=lambda e: abs((datetime.fromisoformat(e).date() - target_expiry_date).days),
    )

    def _strike_of(contract: dict) -> float | None:
        try:
            return float(contract["strike_price"])
        except (KeyError, TypeError, ValueError):
            return None

    candidates = [
        (contract, strike)
        for contract in eligible_expiries[chosen_expiry]
        for strike in [_strike_of(contract)]
        if strike is not None
    ]
    if not candidates:
        return ContractResolutionResult(
            ok=False,
            reason=(
                f"listed contracts for {underlying_symbol} {chosen_expiry} have no "
                "parseable strike_price"
            ),
        )

    # target_strike is a ROUGH PRICE ANCHOR only, for where to START
    # looking -- not the final criterion. Rank the whole expiry's strikes
    # by price-proximity to it once; the window actually queried below
    # widens adaptively over this fixed ranking.
    price_ranked = sorted(
        candidates, key=lambda cs: (abs(cs[1] - target_strike), cs[1])
    )

    # Adaptive widening: |delta| moves monotonically with strike at a
    # fixed expiry (a real, not assumed, property -- verified empirically
    # against a live chain: e.g. successive $1 SPY put strikes stepping
    # delta from -0.093 to -0.152 in strict order). price_ranked expands
    # outward from target_strike as a contiguous strike interval, so once
    # the searched window's |delta| values straddle DELTA_CORRIDOR_CENTER
    # on both sides, the true closest-to-center strike is provably already
    # inside that window -- no further widening can improve the pick.
    # Without this, a fixed narrow window can silently settle for
    # "closest available in an arbitrarily small slice," which is what
    # first surfaced this gap (a low-delta-per-dollar name like SPY,
    # searched only 20-wide, picked a strike near the floor instead of
    # the center simply because the center-delta strike was farther away
    # in price than the window reached).
    queried: dict[str, tuple[dict, float, float | None]] = {}
    search_count = min(strike_search_count, len(price_ranked)) if price_ranked else 0
    while True:
        window = price_ranked[:search_count]
        to_query = [
            contract.get("symbol")
            for contract, _ in window
            if contract.get("symbol") and contract.get("symbol") not in queried
        ]
        if to_query:
            quotes = fetch_option_quotes(to_query, timeout_seconds=timeout_seconds, feed=feed)
            for contract, strike in window:
                occ_symbol = contract.get("symbol")
                if not occ_symbol or occ_symbol not in to_query:
                    continue
                quote_result = quotes.get(occ_symbol)
                delta = None
                if quote_result is not None and quote_result.ok and quote_result.quote is not None:
                    delta = quote_result.quote.delta
                queried[occ_symbol] = (contract, strike, delta)

        usable_abs_deltas = [
            abs(delta) for _, _, delta in queried.values() if delta is not None
        ]
        bracketed = bool(usable_abs_deltas) and (
            min(usable_abs_deltas) <= DELTA_CORRIDOR_CENTER <= max(usable_abs_deltas)
        )
        if bracketed or search_count >= len(price_ranked):
            break
        search_count = min(search_count * 2, len(price_ranked))

    delta_candidates = [
        (contract, strike, delta)
        for contract, strike, delta in queried.values()
        if delta is not None
    ]
    if not delta_candidates:
        return ContractResolutionResult(
            ok=False,
            reason=(
                f"none of the {len(queried)} listed {alpaca_type} strikes searched near "
                f"${target_strike:,.2f} at {underlying_symbol} {chosen_expiry} (out of "
                f"{len(price_ranked)} listed at this expiry) has a usable delta quote"
            ),
        )

    chosen_contract, chosen_strike, chosen_delta = min(
        delta_candidates, key=lambda csd: abs(abs(csd[2]) - DELTA_CORRIDOR_CENTER)
    )

    # Only the REAL enforced bound (net_delta_floor's structural_delta_floor)
    # gates fail-closed here -- DELTA_CORRIDOR_CEILING is a disclosed
    # selection preference, not a policy-enforced ceiling (verified: no
    # rule anywhere blocks on |delta| being too HIGH), so exceeding it is
    # never a reason to refuse a resolution the floor would still accept.
    if abs(chosen_delta) < DELTA_CORRIDOR_FLOOR:
        return ContractResolutionResult(
            ok=False,
            reason=(
                f"closest-to-center listed strike for {underlying_symbol} {chosen_expiry} "
                f"({chosen_contract.get('symbol')}, strike ${chosen_strike:,.2f}, |delta| "
                f"{abs(chosen_delta):.4f}) is still below net_delta_floor's "
                f"{DELTA_CORRIDOR_FLOOR}-delta structural floor after searching "
                f"{len(queried)} of {len(price_ranked)} listed strikes at this expiry -- "
                "no viable protective put exists at this expiry for this name today"
            ),
        )

    occ_symbol = chosen_contract.get("symbol")
    if not occ_symbol:
        return ContractResolutionResult(
            ok=False,
            reason=(
                f"listed contract for {underlying_symbol} {chosen_expiry} strike "
                f"{chosen_strike} has no symbol field"
            ),
        )

    return ContractResolutionResult(
        ok=True,
        contract=ResolvedContract(
            occ_symbol=occ_symbol,
            strike=chosen_strike,
            expiry=chosen_expiry,
            delta=chosen_delta,
        ),
    )
