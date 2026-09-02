"""account_data -- shared fetcher for Alpaca's own account-level state,
sourcing `session_pnl_usd` for drawdown_killswitch (see AUDIT.md findings
E3/E4 and the follow-up decision on how to compute it) and `equity` for
cvar_gate/pct_of_adv/hedge_cost_cap's `account_equity_state_key` (same GET
/v2/account response, not a second fetch -- see `AccountPnLResult.equity`
and `firewall.proxy.FirewallMiddleware._populate_account_state`).

Decision: pull from Alpaca's GET /v2/account rather than compute PnL
locally from recorded fills against live mark prices. Verified against
Alpaca's real API docs (docs.alpaca.markets/reference/getaccount-1,
checked 2026-08-23) before building against it -- the same discipline
AUDIT.md's A4 finding says was missing the first time (rules built
against an assumed tool shape that was never checked against the real
upstream):

  - `equity`: real-time total account equity (cash + long/short market
    value).
  - `last_equity`: "Equity as of previous trading day at 16:00:00 ET" --
    i.e. the prior session's closing equity.

`session_pnl_usd = equity - last_equity` is exactly Alpaca's own "day
P&L" figure (this firewall's notion of "session" is the current trading
day, matching `last_equity`'s definition) -- one field subtraction,
computed by Alpaca from real fills, corporate actions, fees, and
mark-to-market pricing, none of which this firewall has to get right
itself. Reusing it is simpler and less error-prone than re-deriving the
same number from a locally-maintained cost-basis ledger.

Deliberately NOT wired here: `cooldown_after_loss`'s windowed
`pnl_history` (a rolling series of *realized* P&L events). Verified
against Alpaca's docs that no endpoint provides this cleanly:
GET /v2/account/portfolio/history's `profit_loss` is cumulative
mark-to-market P&L from a period `base_value` (includes unrealized
gains/losses, not realized-only, and its finest granularity is 1Min --
both a semantic mismatch and a coarser resolution than
`cooldown_after_loss`'s "realized loss" framing and its default 300s
window), and GET /v2/account/activities's FILL records carry no P&L
field at all (price/qty/side/symbol only -- computing realized P&L from
them would require the same local cost-basis matching this module exists
to avoid). This is a real, undecided design choice -- not an oversight --
left for a follow-up conversation rather than silently picked here.

Like `firewall.market_data.fetch_daily_bars`, this never raises: any
failure comes back as `AccountPnLResult(ok=False, reason=...)` so a
fetch problem is something a rule (or its caller) can see and fail
closed on, not something that crashes the call.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from firewall.market_data import LatestPriceResult, fetch_stock_latest_price

_TRADING_API_PAPER_URL = "https://paper-api.alpaca.markets"

# Must match firewall.proxy._UPSTREAM_PAPER_TRUE_VALUES exactly: both read
# the same ALPACA_PAPER_TRADE env var and must agree on what counts as
# "paper". Duplicated (not imported) to avoid a proxy.py <-> account_data.py
# import cycle (proxy.py wires this module's fetcher into FirewallMiddleware).
_PAPER_TRUE_VALUES = ("true", "1", "yes")

DEFAULT_TIMEOUT_SECONDS = 5.0
# Short on purpose: this gates a real-time trading halt (drawdown_killswitch).
# A 30-minute-stale equity figure (market_data.py's bars TTL) would be worse
# than useless here.
DEFAULT_CACHE_TTL_SECONDS = 5.0


def _redact_error(value: object) -> str:
    text = str(value)
    for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        secret = os.environ.get(name, "")
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


@dataclass
class AccountPnLResult:
    ok: bool
    session_pnl_usd: float | None = None
    # Raw `equity` from the same GET /v2/account response session_pnl_usd is
    # derived from -- NOT a second fetch. Feeds state["account_equity"]
    # (cvar_gate/pct_of_adv/hedge_cost_cap's account_equity_state_key), the
    # same wiring pattern as session_pnl_usd -> drawdown_killswitch, see
    # firewall.proxy.FirewallMiddleware._populate_account_state.
    equity: float | None = None
    last_equity: float | None = None
    account_id: str | None = None
    reason: str | None = None


@dataclass
class PositionsResult:
    ok: bool
    # symbol -> current USD market value (Alpaca's own `market_value`, long
    # positions only for now -- see fetch_positions's own docstring on
    # short positions). Feeds state["positions"] (position_cap's
    # positions_state_key).
    positions: dict[str, float] | None = None
    # Per-symbol intraday mark-to-market P&L reported by Alpaca.  This is
    # display/attribution data only and is never fed into order decisions.
    intraday_pnl: dict[str, float] | None = None
    # Broker-reported signed share/contract quantities from the same
    # response. Cycle sizing requires these and fails closed if absent.
    quantities: dict[str, float] | None = None
    current_prices: dict[str, float] | None = None
    # time.time() (wall clock -- NOT time.monotonic(), deliberately: this
    # must be directly comparable to OrderHistory event timestamps, which
    # are wall-clock too, see firewall.proxy's state["now"]) this was
    # actually fetched from Alpaca -- NOT the time of a cache-hit call that
    # returned it. Lets a caller (position_cap) tell "an order recorded in
    # order_history after this timestamp is not yet reflected in
    # `positions` and must be added on top of it" from "it already is."
    # See position_cap.py's own in-flight-exposure comment.
    fetched_at: float | None = None
    reason: str | None = None


@dataclass
class MarketClockResult:
    ok: bool
    is_open: bool | None = None
    next_open: str | None = None
    next_close: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class OpenOrder:
    order_id: str
    symbol: str
    side: str
    remaining_qty: float
    unit_price: float
    outstanding_notional: float
    asset_class: str
    exposure_price_source: str = "broker_order_price"
    exposure_price_is_estimate: bool = False


@dataclass(frozen=True)
class OpenOrdersResult:
    ok: bool
    orders: tuple[OpenOrder, ...] = ()
    aggregate_outstanding_notional: float | None = None
    reason: str | None = None


def include_pending_equity_orders(
    positions: dict[str, int | float], orders: tuple[OpenOrder, ...], symbols: tuple[str, ...]
) -> tuple[dict[str, float], dict[str, float]]:
    """Return broker-committed positions and signed pending qty by symbol."""
    pending: dict[str, float] = {}
    for order in orders:
        if order.asset_class not in {"us_equity", "equity"}:
            continue
        signed = order.remaining_qty if order.side == "buy" else -order.remaining_qty
        pending[order.symbol] = pending.get(order.symbol, 0.0) + signed
    committed = {
        symbol: float(positions.get(symbol, 0)) + pending.get(symbol, 0.0)
        for symbol in symbols
    }
    return committed, pending


def exposure_snapshot(positions: PositionsResult, open_orders: OpenOrdersResult) -> dict:
    """Canonical snapshot consumed by sizing and the PolicyEngine rule."""
    if not positions.ok or positions.quantities is None or not open_orders.ok:
        return {"ok": False, "reason": positions.reason or open_orders.reason}
    _, pending = include_pending_equity_orders(
        positions.quantities, open_orders.orders, tuple(positions.quantities)
    )
    body = {
        "positions": {k: float(v) for k, v in sorted(positions.quantities.items())},
        "current_prices": {
            k: float(v) for k, v in sorted((positions.current_prices or {}).items())
        },
        "pending_signed_qty": {k: float(v) for k, v in sorted(pending.items())},
        "open_order_ids": sorted(order.order_id for order in open_orders.orders),
    }
    body["fingerprint"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {"ok": True, **body}


def fetch_consistent_exposure_snapshot(
    prices: dict[str, float],
    *,
    positions_fetcher: Callable[[], PositionsResult] | None = None,
    open_orders_fetcher: Callable[[dict[str, float]], OpenOrdersResult] | None = None,
) -> dict:
    """Positions -> open orders -> positions; accept only a stable snapshot."""
    get_positions = positions_fetcher or (lambda: fetch_positions(cache_ttl_seconds=0))
    get_orders = open_orders_fetcher or fetch_open_orders
    before = get_positions()
    if not before.ok or before.quantities is None:
        return {"ok": False, "reason": before.reason or "position quantities unavailable"}
    quote_prices = {**(before.current_prices or {}), **prices}
    orders = get_orders(quote_prices)
    if not orders.ok:
        return {"ok": False, "reason": orders.reason or "open orders unavailable"}
    after = get_positions()
    if not after.ok or after.quantities is None:
        return {"ok": False, "reason": after.reason or "position re-check unavailable"}
    if before.quantities != after.quantities:
        return {"ok": False, "reason": "positions changed during open-order reconciliation"}
    # Preserve independently supplied quote prices in the final snapshot.
    # Positions only contain current_price for symbols already held.
    after.current_prices = {**(after.current_prices or {}), **prices}
    snapshot = exposure_snapshot(after, orders)
    snapshot["aggregate_outstanding_notional"] = orders.aggregate_outstanding_notional
    snapshot["open_orders"] = orders.orders
    return snapshot


@dataclass
class _CacheEntry:
    result: AccountPnLResult
    fetched_at: float


@dataclass
class _PositionsCacheEntry:
    result: PositionsResult
    fetched_at: float


_cache: _CacheEntry | None = None
_positions_cache: _PositionsCacheEntry | None = None


def _trading_api_base_url() -> str | None:
    """The paper-trading API host, or None if ALPACA_PAPER_TRADE doesn't
    explicitly resolve to paper. Same posture as
    firewall.proxy._require_paper_trade_mode: unset or ambiguous is
    refused, never defaulted to the live API -- this fetcher must not
    silently pull a live account's real PnL."""
    raw = os.environ.get("ALPACA_PAPER_TRADE")
    if raw is not None and raw.strip().lower() in _PAPER_TRUE_VALUES:
        return _TRADING_API_PAPER_URL
    return None


def _fetch_account(timeout_seconds: float) -> dict:
    base_url = _trading_api_base_url()
    if base_url is None:
        raise PermissionError(
            "ALPACA_PAPER_TRADE does not explicitly resolve to paper mode "
            "(refusing to fetch account data against a possibly-live account)"
        )
    request = urllib.request.Request(
        f"{base_url}/v2/account",
        headers={
            "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", ""),
            "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read())


def _fetch_positions_raw(timeout_seconds: float) -> list:
    base_url = _trading_api_base_url()
    if base_url is None:
        raise PermissionError(
            "ALPACA_PAPER_TRADE does not explicitly resolve to paper mode "
            "(refusing to fetch position data against a possibly-live account)"
        )
    request = urllib.request.Request(
        f"{base_url}/v2/positions",
        headers={
            "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", ""),
            "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read())


def _fetch_open_orders_raw(timeout_seconds: float, limit: int) -> list:
    base_url = _trading_api_base_url()
    if base_url is None:
        raise PermissionError(
            "ALPACA_PAPER_TRADE does not explicitly resolve to paper mode "
            "(refusing to fetch orders against a possibly-live account)"
        )
    request = urllib.request.Request(
        f"{base_url}/v2/orders?status=open&limit={limit}&nested=true&direction=asc",
        headers={
            "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", ""),
            "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read())


def fetch_open_orders(
    prices: dict[str, float], *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS, limit: int = 500,
    latest_price_fetcher: Callable[[str], LatestPriceResult] = fetch_stock_latest_price,
) -> OpenOrdersResult:
    """Fetch every open Alpaca order and fail closed on incomplete exposure data.

    A full page is treated as incomplete because Alpaca may have additional
    orders beyond the endpoint limit. Market-order exposure uses the same
    current symbol price snapshot used by cycle sizing. Options apply their
    100-share contract multiplier.
    """
    try:
        payload = _fetch_open_orders_raw(timeout_seconds, limit)
        if not isinstance(payload, list):
            raise ValueError("response is not a list")
        if len(payload) >= limit:
            raise ValueError(f"response reached page limit {limit}; completeness unknown")
        parsed: list[OpenOrder] = []
        latest_price_cache: dict[str, LatestPriceResult] = {}
        for index, raw in enumerate(payload):
            if not isinstance(raw, dict):
                raise ValueError(f"order {index} is not an object")
            order_id = str(raw.get("id") or "").strip()
            symbol = str(raw.get("symbol") or "").strip().upper()
            side = str(raw.get("side") or "").strip().lower()
            asset_class = str(raw.get("asset_class") or "us_equity").strip().lower()
            if not order_id or not symbol or side not in {"buy", "sell"}:
                raise ValueError(f"order {index} lacks id/symbol/valid side")
            qty = float(raw["qty"])
            filled_qty = float(raw.get("filled_qty") or 0)
            remaining_qty = qty - filled_qty
            if qty <= 0 or filled_qty < 0 or remaining_qty < 0:
                raise ValueError(f"order {order_id} has invalid qty/filled_qty")
            if remaining_qty == 0:
                continue
            if raw.get("limit_price") is not None:
                raw_price = raw["limit_price"]
                price_source = "limit_price"
                price_is_estimate = False
            elif raw.get("stop_price") is not None:
                raw_price = raw["stop_price"]
                price_source = "stop_price"
                price_is_estimate = False
            elif prices.get(symbol) is not None:
                raw_price = prices[symbol]
                price_source = "supplied_market_price_estimate"
                price_is_estimate = True
            elif asset_class in {"us_equity", "equity"}:
                latest = latest_price_cache.get(symbol)
                if latest is None:
                    latest = latest_price_fetcher(symbol)
                    latest_price_cache[symbol] = latest
                if not latest.ok or latest.price is None:
                    raise ValueError(
                        f"order {order_id} has no usable exposure price for {symbol}; "
                        f"fresh latest-trade estimate failed: {latest.reason or 'unknown error'}"
                    )
                raw_price = latest.price
                price_source = "fresh_latest_trade_estimate"
                price_is_estimate = True
            else:
                raw_price = None
                price_source = "unavailable"
                price_is_estimate = False
            if raw_price is None:
                raise ValueError(f"order {order_id} has no usable exposure price for {symbol}")
            unit_price = float(raw_price)
            if unit_price <= 0:
                raise ValueError(f"order {order_id} has invalid exposure price")
            multiplier = 100.0 if asset_class in {"us_option", "option"} else 1.0
            notional = remaining_qty * unit_price * multiplier
            parsed.append(OpenOrder(
                order_id=order_id,
                symbol=symbol,
                side=side,
                remaining_qty=remaining_qty,
                unit_price=unit_price,
                outstanding_notional=notional,
                asset_class=asset_class,
                exposure_price_source=price_source,
                exposure_price_is_estimate=price_is_estimate,
            ))
        return OpenOrdersResult(
            ok=True,
            orders=tuple(parsed),
            aggregate_outstanding_notional=sum(order.outstanding_notional for order in parsed),
        )
    except PermissionError as exc:
        return OpenOrdersResult(ok=False, reason=str(exc))
    except (socket.timeout, TimeoutError):
        return OpenOrdersResult(ok=False, reason=f"timed out after {timeout_seconds}s fetching open orders")
    except urllib.error.HTTPError as exc:
        return OpenOrdersResult(ok=False, reason=f"HTTP {exc.code} fetching open orders: {_redact_error(exc.reason)}")
    except urllib.error.URLError as exc:
        return OpenOrdersResult(ok=False, reason=f"network error fetching open orders: {_redact_error(exc.reason)}")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return OpenOrdersResult(ok=False, reason=f"incomplete open-order exposure data: {exc}")
    except Exception as exc:
        return OpenOrdersResult(ok=False, reason=f"open-order reconciliation failed: {_redact_error(exc)}")


def fetch_market_clock(*, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> MarketClockResult:
    """Read Alpaca's paper market clock. Never raises and never mutates state."""
    base_url = _trading_api_base_url()
    if base_url is None:
        return MarketClockResult(ok=False, reason="paper mode is not explicitly enabled")
    request = urllib.request.Request(
        f"{base_url}/v2/clock",
        headers={
            "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", ""),
            "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read())
        return MarketClockResult(
            ok=True,
            is_open=bool(payload["is_open"]),
            next_open=payload.get("next_open"),
            next_close=payload.get("next_close"),
        )
    except Exception as exc:
        return MarketClockResult(ok=False, reason=f"market clock unavailable: {_redact_error(exc)}")


def fetch_positions(
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
) -> PositionsResult:
    """Current per-symbol USD market value from Alpaca's real GET
    /v2/positions (verified live against the real paper account,
    2026-08-29 -- `market_value`, a string-typed dollar figure, is the
    real field name). Never raises -- see module docstring.

    Only long positions contribute a positive figure; this project's
    option_sell_guard already hard-blocks every option-sell order (a
    stated scope limitation, see that rule's own module docstring) and
    core_strategy.py never shorts stock, so a short `market_value`
    (negative) is left as-is rather than special-cased -- position_cap's
    own `current + notional` arithmetic (buys only; it already skips
    sells) still behaves sensibly against it, just not independently
    verified against a real short position.
    """
    global _positions_cache

    # Two clocks, deliberately: cache freshness is judged on the monotonic
    # clock (immune to wall-clock adjustments, matching fetch_session_pnl's
    # own convention); the result's own `fetched_at` (below) is wall-clock,
    # because that one has to compare against OrderHistory timestamps.
    cache_now = time.monotonic()
    if (
        _positions_cache is not None
        and (cache_now - _positions_cache.fetched_at) < cache_ttl_seconds
    ):
        return _positions_cache.result

    try:
        payload = _fetch_positions_raw(timeout_seconds)
    except PermissionError as exc:
        return PositionsResult(ok=False, reason=str(exc))
    except (socket.timeout, TimeoutError):
        return PositionsResult(
            ok=False, reason=f"timed out after {timeout_seconds}s fetching positions"
        )
    except urllib.error.HTTPError as exc:
        return PositionsResult(
            ok=False, reason=f"HTTP {exc.code} fetching positions: {_redact_error(exc.reason)}"
        )
    except urllib.error.URLError as exc:
        return PositionsResult(
            ok=False, reason=f"network error fetching positions: {_redact_error(exc.reason)}"
        )
    except (json.JSONDecodeError, TypeError) as exc:
        return PositionsResult(ok=False, reason=f"malformed positions response: {exc}")
    except Exception as exc:  # never let a fetch failure escape as an exception
        return PositionsResult(ok=False, reason=f"unexpected error fetching positions: {_redact_error(exc)}")

    if not isinstance(payload, list):
        return PositionsResult(ok=False, reason=f"unexpected positions response shape: {type(payload)}")

    positions: dict[str, float] = {}
    intraday_pnl: dict[str, float] = {}
    quantities: dict[str, float] = {}
    current_prices: dict[str, float] = {}
    try:
        for item in payload:
            symbol = item["symbol"]
            positions[symbol] = float(item["market_value"])
            if "qty" in item:
                quantities[symbol] = float(item["qty"])
            if item.get("current_price") is not None:
                current_prices[symbol] = float(item["current_price"])
            raw_intraday = item.get("unrealized_intraday_pl")
            if raw_intraday is not None:
                intraday_pnl[symbol] = float(raw_intraday)
    except (KeyError, TypeError, ValueError) as exc:
        return PositionsResult(
            ok=False, reason=f"positions response missing/invalid market_value field: {exc}"
        )

    result = PositionsResult(
        ok=True,
        positions=positions,
        intraday_pnl=intraday_pnl,
        quantities=quantities,
        current_prices=current_prices,
        fetched_at=time.time(),
    )
    _positions_cache = _PositionsCacheEntry(result=result, fetched_at=cache_now)
    return result


def fetch_session_pnl(
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
) -> AccountPnLResult:
    """Today's session P&L (`equity - last_equity`) from Alpaca's real
    account state. Never raises -- see module docstring."""
    global _cache

    now = time.monotonic()
    if _cache is not None and (now - _cache.fetched_at) < cache_ttl_seconds:
        return _cache.result

    try:
        payload = _fetch_account(timeout_seconds)
    except PermissionError as exc:
        return AccountPnLResult(ok=False, reason=str(exc))
    except (socket.timeout, TimeoutError):
        return AccountPnLResult(
            ok=False, reason=f"timed out after {timeout_seconds}s fetching account"
        )
    except urllib.error.HTTPError as exc:
        return AccountPnLResult(
            ok=False, reason=f"HTTP {exc.code} fetching account: {_redact_error(exc.reason)}"
        )
    except urllib.error.URLError as exc:
        return AccountPnLResult(
            ok=False, reason=f"network error fetching account: {_redact_error(exc.reason)}"
        )
    except (json.JSONDecodeError, TypeError) as exc:
        return AccountPnLResult(ok=False, reason=f"malformed account response: {exc}")
    except Exception as exc:  # never let a fetch failure escape as an exception
        return AccountPnLResult(ok=False, reason=f"unexpected error fetching account: {_redact_error(exc)}")

    try:
        equity = float(payload["equity"])
        last_equity = float(payload["last_equity"])
    except (KeyError, TypeError, ValueError) as exc:
        return AccountPnLResult(
            ok=False, reason=f"account response missing/invalid equity fields: {exc}"
        )

    account_id = payload.get("id") or payload.get("account_id")
    result = AccountPnLResult(
        ok=True,
        session_pnl_usd=equity - last_equity,
        equity=equity,
        last_equity=last_equity,
        account_id=str(account_id) if account_id else None,
    )
    _cache = _CacheEntry(result=result, fetched_at=now)
    return result
