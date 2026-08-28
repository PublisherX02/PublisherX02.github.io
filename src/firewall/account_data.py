"""account_data -- shared fetcher for Alpaca's own account-level P&L,
sourcing `session_pnl_usd` for drawdown_killswitch (see AUDIT.md findings
E3/E4 and the follow-up decision on how to compute it).

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

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

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


@dataclass
class AccountPnLResult:
    ok: bool
    session_pnl_usd: float | None = None
    reason: str | None = None


@dataclass
class _CacheEntry:
    result: AccountPnLResult
    fetched_at: float


_cache: _CacheEntry | None = None


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
            ok=False, reason=f"HTTP {exc.code} fetching account: {exc.reason}"
        )
    except urllib.error.URLError as exc:
        return AccountPnLResult(
            ok=False, reason=f"network error fetching account: {exc.reason}"
        )
    except (json.JSONDecodeError, TypeError) as exc:
        return AccountPnLResult(ok=False, reason=f"malformed account response: {exc}")
    except Exception as exc:  # never let a fetch failure escape as an exception
        return AccountPnLResult(ok=False, reason=f"unexpected error fetching account: {exc}")

    try:
        equity = float(payload["equity"])
        last_equity = float(payload["last_equity"])
    except (KeyError, TypeError, ValueError) as exc:
        return AccountPnLResult(
            ok=False, reason=f"account response missing/invalid equity fields: {exc}"
        )

    result = AccountPnLResult(ok=True, session_pnl_usd=equity - last_equity)
    _cache = _CacheEntry(result=result, fetched_at=now)
    return result
