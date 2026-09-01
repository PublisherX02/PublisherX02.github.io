"""core_strategy — an inverse-volatility weighted basket rebalancer and
scheduled options overlay that generates real trading activity for the
firewall's governance layer to manage.

THIS IS NOT PART OF THE FIREWALL AND MAKES NO RISK DECISIONS. It proposes
orders; `firewall.proxy.build_proxy()`'s `FirewallMiddleware` evaluates and
either forwards or hard-blocks them, exactly like every other caller of
this MCP proxy. There is no separate execution route, and no exception
carved out from `notional_cap`/`position_cap`/`symbol_allowlist`/any other
rule -- if a change to `policies/default.yaml` ever tightens what this
basket is allowed to do, this module is governed by that change with zero
code changes of its own.

Entry logic and position sizing, stated plainly:
position sizes are set by inverse-volatility weighting using trailing realized
volatility -- this allocates risk, not conviction, and makes no claim
about expected returns or direction.

THIS ENTRY LOGIC MAKES NO CLAIM OF PREDICTIVE EDGE: this entry logic makes
no claim of predictive edge; it exists to generate real positions for the
risk-governance layer (caps, CVaR, the hedge-trigger) to manage and
demonstrate against. This is deliberately the least sophisticated defensible
option, consistent with this project's stated bias against overclaiming (see
README's "What this does not do").

BASKET is exactly `symbol-allowlist`'s own `allowed_symbols` in the shipped
`policies/default.yaml`: the 4 core assets ("AAPL", "MSFT", "SPY", "QQQ")
plus 7 second-order federal IT and defense contractors with verified,
high-dollar subaward linkages to Microsoft Corporation on USASpending.gov
("GD", "CACI", "ACN", "LDOS", "NOC", "BAH", "J"). ASGN was dropped after
failing Alpaca free-tier 90-day bars availability. Note: the MSFT->federal-
contractor link was validated by hand for this specific pair, not by a
general-purpose resolution engine.

Sizing decision: all 11 names sit alongside each other in a unified 11-asset
inverse-volatility risk-parity basket deploying 90% NAV with a 10% cash
buffer, governed under the same 25% position cap and $5k chunking rules.
Chosen over a separate sub-allocation sleeve or a replacement of the
original four: a unified basket needs zero new risk parameters -- the same
position_cap/chunking/rebalancing logic already verified across AAPL/MSFT/
SPY/QQQ governs all 11 names unmodified -- and the diversification effect
is a real, observed one, not an asserted one: SPY's raw inverse-vol weight
dropped from ~44% in the 4-name basket to ~20.08% in the live 11-name run
purely from the formula now averaging across seven more, generally
lower-volatility names.

VOLATILITY PIPELINE: For each name, trailing realized volatility is computed
by REUSING `cvar_gate`'s existing daily bars fetch (`fetch_daily_bars`) and log
returns calculation (`_log_returns`) -- no second volatility pipeline is
built. Realized volatility is computed as the sample standard deviation of
daily log returns over the lookback window. Inverse-volatility weights are
computed as:
    w_i = (1 / vol_i) / sum(1 / vol_j for all j in basket)
so each position contributes roughly equal risk to the basket rather than
equal dollars.

DRIFT-BASED REBALANCING: Rebalances on a fixed schedule (default 24h cadence).
Only places rebalancing orders if a position's current weight has drifted
beyond a configured threshold (default 5%) from target -- avoiding unnecessary
turnover and unnecessary firewall load.

BASKET BUDGET, DYNAMIC AND STATED: the total basket budget is
`account_equity * DEFAULT_BASKET_PCT_OF_NAV` (90%), recomputed live each
cycle from the SAME `account_equity` state source cvar_gate/pct_of_adv/
hedge_cost_cap/notional_cap/position_cap all read (see
`firewall.account_data.fetch_session_pnl`) -- not a flat dollar figure
picked once (see README's "What this does not do" on exactly that
anti-pattern, and `DEFAULT_TOTAL_BUDGET_USD`'s own comment for the figure
this replaced). A deliberate, stated 90%, not 100%: a real cash buffer, not
an accidental leftover -- see `DEFAULT_BASKET_PCT_OF_NAV`'s own comment.

POSITION-CAP-AWARE ALLOCATION: after computing raw inverse-volatility
target weights, any name whose raw TARGET (not just this cycle's order)
would exceed position_cap's real, configured per-symbol ceiling
(`account_equity * position_cap`'s own `max_pct_of_equity`, read directly
off the loaded policy engine's rule instance by `_read_dynamic_policy_
config` -- never a duplicated constant that could drift from what the
firewall actually enforces) is clipped to that ceiling BEFORE any order is
sized. The clipped-away weight is left as uninvested cash, not
redistributed to other names -- see `clip_weights_to_position_cap`. This
is written to the audit log as a dedicated, non-blocking
`basket_rebalance:weight_clipped` record (visible on the dashboard, not a
footnote) whenever it happens.

NOTIONAL-CAP-AWARE CHUNKING: any resulting order whose notional would
exceed notional_cap's real, configured per-order ceiling (same
real-engine-read pattern as position_cap's ceiling above) is split into
multiple smaller orders, each at or under that ceiling, submitted
sequentially through the same firewall path -- see
`split_order_into_chunks`. Written to the audit log as a dedicated
`basket_rebalance:order_chunked` record disclosing the real chunk count
and sizes.

THROTTLE-AWARE PACING: chunking means a single rebalance cycle can need
many more orders than one-per-basket-name -- at this basket's real ~$100k
scale, a dominant-weight name capped at position_cap's ceiling and chunked
at notional_cap's ceiling alone needs several orders (SPY: a $25,000
capped target at a $5,000 per-chunk ceiling is 5 chunks), and every other
name chunks too. `ThrottlePacer` keeps submissions comfortably under
order_rate_throttle's real configured limit, with an explicit safety
margin -- not "the chunk count happens to fit." This matters concretely:
order_rate_throttle counts hard-blocked attempts too, and once tripped it
stays paused until a human explicitly resets it (see
`policies/default.yaml`'s own comment on that rule) -- a margin of zero
would let one retry or overlapping cycle latch the entire firewall shut.

SCHEDULED OPTIONS OVERLAY: On a fixed schedule, proposes a small protective
put on the basket's largest current position -- sized through the SAME
premium-cap and delta-corridor rules already built for the reactive hedge,
with no new risk logic. This is standing portfolio insurance, not a
market-timing decision: a disclosed, scheduled options overlay applied
regardless of market conditions, distinct from the reactive CVaR-triggered
hedge. Both hedge sources write clearly distinguishable audit records, but
via two different mechanisms because only one of them ever places a real
order: the reactive trigger writes ONE direct record
(`rule_id: hedge-proposal`, `verdict: soft_block`) since it never submits
anything (see `hedge_proposal.py`'s own docstring). The scheduled overlay
DOES submit a real `place_option_order` call, so it produces TWO records --
a provenance record this module writes directly before submitting
(`tool_name: "scheduled_overlay:proposed"`, `rule_id:
"scheduled-options-overlay"`, `verdict: "info"` -- a non-blocking marker
that identifies *where the order came from*, never a policy decision) and
a separate, ordinary record from the normal `PolicyEngine.evaluate()` path
carrying whatever `rule_id` actually fired on that order (or `None` if
nothing did). The provenance record is written unconditionally, before the
order is submitted, so it survives even when the real evaluation
hard-blocks the order -- the two records are never collapsed into one, and
neither is dropped depending on the other's outcome.

WHY EVERY ORDER HERE IS A PLAIN MARKET ORDER WITH `qty` ONLY, NEVER
`limit_price` OR `notional` -- LIVE-VERIFIED, NOT A FIREWALL-VISIBILITY
WORKAROUND: verified directly against the real Alpaca paper API
(2026-08-29) that `type: "market"` combined with a `limit_price` is
rejected outright (HTTP 422, code 40010001, "market orders require no stop
or limit price") -- a plain market order structurally CANNOT carry a price
at all, for any reason, firewall visibility included. The inverse-vol
dollar allocation is converted to an integer share count locally (via
`compute_target_quantities`, using the same `firewall.market_data.
fetch_daily_bars` helper cvar_gate/pct_of_adv already use for a recent
close) because that share count is what a qty-only order needs, not to
dodge any rule's notional check.

`state["account_equity"]` IS now populated (see `firewall.proxy.
FirewallMiddleware._populate_account_state`, and `firewall.account_data`'s
own module docstring) -- the pre-existing gap this section used to
describe is closed. `cvar_gate`/`pct_of_adv` still simply skip a
qty-only order (`firewall.rules._util.extract_notional` returns None for
one, and neither rule has a fallback for that): unaffected by any of this,
since their own inputs (a notional TO assess, not the reference price
itself) are unrelated to what changed. `notional_cap`/`position_cap` are
different: rather than skip, each independently fetches its OWN reference
price for a qty-only STOCK order (same shared `fetch_daily_bars` helper,
`price_lookback_days`-worth of history, most recent close) and evaluates
the order for real -- see `notional_cap.py`'s own module docstring for
why, and for why this is scoped to stock orders only (not options, not
crypto). This is what makes the chunking/clipping described above possible
at all: a qty-only order the firewall could only skip, never actually
size, would give notional_cap/position_cap nothing to enforce regardless
of how carefully this module chunks or clips locally.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from dataclasses import dataclass
from typing import Any, Callable

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from fastmcp import Client, FastMCP

from firewall import account_data
from firewall.audit import AuditLogWriter
from firewall.market_data import BarsResult, fetch_daily_bars
from firewall.rules.cvar_gate import _log_returns
from firewall.rules.hedge_proposal import (
    ScheduledOverlayProposal,
    compute_scheduled_overlay,
    format_occ_symbol,
)

BarsFetcher = Callable[[str, int], BarsResult]

# Disclosed, hardcoded, not selected by any predictive process -- see
# module docstring for why this is exactly symbol-allowlist's own
# allowed_symbols list in policies/default.yaml.
BASKET: tuple[str, ...] = (
    "AAPL",
    "MSFT",
    "SPY",
    "QQQ",
    "GD",
    "CACI",
    "ACN",
    "LDOS",
    "NOC",
    "BAH",
    "J",
)

# Legacy, no-longer-computed-by-default flat budget -- kept only as the
# fallback `compute_qty`/`build_market_buy_payload` callers used before this
# module read account_equity. `place_basket_orders`/`run_once` no longer use
# this by default: see DEFAULT_BASKET_PCT_OF_NAV below and this module's
# docstring for why. A flat $800 was originally "sized comfortably under"
# the firewall's flat-dollar caps -- exactly the "number picked to make
# today's orders pass" this project's own audit discipline warns against,
# not a deliberate deployment-scale decision.
DEFAULT_TOTAL_BUDGET_USD = 800.0
PER_NAME_BUDGET_USD = 200.0  # Legacy alias (~$800 / 4 names)

# Total basket budget as a fraction of real account equity -- the PRIMARY
# sizing mechanism now, recomputed live each cycle from
# state["account_equity"]'s own source (account_data.fetch_session_pnl,
# the SAME shared fetcher cvar_gate/pct_of_adv/hedge_cost_cap/notional_cap/
# position_cap already read via the firewall's account_equity wiring -- not
# a second, independently-derived equity figure). 90%, not 100%: this is a
# strategy meant to deploy MOST of the account (see this module's own
# docstring), not all of it -- a stated cash buffer for fees, slippage, and
# margin headroom, not an accidental leftover.
DEFAULT_BASKET_PCT_OF_NAV = 0.90

# order_rate_throttle's real configured limit (policies/default.yaml) --
# ThrottlePacer's own conservative defaults when `run_once` can't read the
# real policy engine's rule config directly (e.g. an explicit `proxy` was
# passed in, bypassing the engine this module would otherwise build -- see
# `run_once`). Matches the shipped default.yaml values; drifts only if that
# file's order-rate-throttle rule is edited without updating this constant
# too -- `run_once`'s own real-engine read is the drift-proof path.
DEFAULT_ORDER_RATE_MAX_ORDERS = 20
DEFAULT_ORDER_RATE_WINDOW_SECONDS = 60.0

# Drift threshold (5%): only submit rebalance orders if a position's
# actual weight drifts from target weight by more than this amount.
DEFAULT_DRIFT_THRESHOLD = 0.05

# Daily cadence by default -- fixed interval rebalancing schedule.
DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60

# Historical lookback window for trailing realized volatility (matches cvar_gate).
VOLATILITY_LOOKBACK_DAYS = 90
_PRICE_LOOKBACK_DAYS = 5


def compute_realized_volatility(closes: list[float]) -> float:
    """Trailing realized volatility (sample standard deviation of daily log returns).

    Reuses cvar_gate._log_returns on historical daily closes. Requires at least
    2 closes (1 return).
    """
    if len(closes) < 2:
        raise ValueError(f"cannot compute volatility from fewer than 2 closes (got {len(closes)})")
    returns = _log_returns(closes)
    if not returns:
        raise ValueError("empty return series")
    n = len(returns)
    if n == 1:
        return 0.0
    mean_ret = sum(returns) / n
    var = sum((r - mean_ret) ** 2 for r in returns) / (n - 1)
    if var < 0.0 or not math.isfinite(var):
        raise ValueError(f"invalid variance {var}")
    return math.sqrt(var)


def compute_inverse_vol_weights(volatilities: dict[str, float]) -> dict[str, float]:
    """Inverse-volatility weights: w_i = (1 / vol_i) / sum(1 / vol_j for j in basket).

    Position sizes are set by inverse-volatility weighting using trailing realized
    volatility -- this allocates risk, not conviction, and makes no claim about
    expected returns or direction.
    """
    if not volatilities:
        return {}

    valid_vols = {s: v for s, v in volatilities.items() if math.isfinite(v) and v > 0}
    if not valid_vols:
        eq = 1.0 / len(volatilities)
        return {s: eq for s in volatilities}

    inv_vols: dict[str, float] = {}
    min_vol = min(valid_vols.values())
    for sym, vol in volatilities.items():
        if not math.isfinite(vol) or vol <= 0:
            inv_vols[sym] = 1.0 / min_vol
        else:
            inv_vols[sym] = 1.0 / vol

    total_inv = sum(inv_vols.values())
    if total_inv <= 0:
        eq = 1.0 / len(volatilities)
        return {s: eq for s in volatilities}
    return {sym: iv / total_inv for sym, iv in inv_vols.items()}


def compute_target_quantities(
    weights: dict[str, float],
    prices: dict[str, float],
    total_budget_usd: float,
) -> dict[str, int]:
    """Calculate target share quantities for each asset from target weights and prices.

    Minimum 1 share is enforced for each asset in the basket to guarantee non-flat
    demonstration positions.
    """
    targets = {}
    for sym, weight in weights.items():
        price = prices.get(sym, 0.0)
        if not math.isfinite(price) or price <= 0:
            raise ValueError(f"cannot size an order against non-positive price {price!r} for {sym}")
        budget_for_asset = total_budget_usd * weight
        qty = max(1, math.floor(budget_for_asset / price))
        targets[sym] = qty
    return targets


def compute_weights_and_drift(
    current_values_usd: dict[str, float],
    target_weights: dict[str, float],
    reference_value_usd: float | None = None,
) -> dict[str, dict[str, float]]:
    """Per-symbol current weight, target weight, and |current - target| drift.

    Takes each symbol's current USD market value directly (not qty x price)
    so callers that already have a dollar value -- Alpaca's own position
    feed, e.g. -- don't need to round-trip through share counts. A caller
    with qty x price should pass that product in.

    This is the single source of truth for "current basket state" shared
    by compute_rebalance_orders (the trading decision) and
    narration.market_brief.build_narration_context (the AI commentary
    panel) -- both read the same numbers computed the same way, never two
    independently-drifting versions of "current state."
    """
    total_current_value = sum(current_values_usd.get(s, 0.0) for s in target_weights)
    denominator = (
        float(reference_value_usd)
        if reference_value_usd is not None and reference_value_usd > 0
        else total_current_value
    )
    result: dict[str, dict[str, float]] = {}
    for sym, target_w in target_weights.items():
        current_val = current_values_usd.get(sym, 0.0)
        current_w = current_val / denominator if denominator > 0 else 0.0
        result[sym] = {
            "current_w": current_w,
            "target_w": target_w,
            "drift": abs(current_w - target_w),
        }
    return result


def compute_rebalance_orders(
    current_positions: dict[str, int],
    target_quantities: dict[str, int],
    target_weights: dict[str, float],
    prices: dict[str, float],
    drift_threshold: float = DEFAULT_DRIFT_THRESHOLD,
    reference_value_usd: float | None = None,
) -> dict[str, int]:
    """Calculate required order deltas for basket positions.

    Orders are generated only for positions whose current portfolio weight
    drifts from target_weight by more than drift_threshold (or if current
    position is 0 and target > 0).

    Returns a dict mapping symbol -> signed quantity delta (+ for buy, - for sell, 0 for none).
    """
    total_current_value = sum(
        current_positions.get(s, 0) * prices.get(s, 0.0) for s in target_weights
    )
    current_values_usd = {
        s: current_positions.get(s, 0) * prices.get(s, 0.0) for s in target_weights
    }
    weights_and_drift = compute_weights_and_drift(
        current_values_usd, target_weights, reference_value_usd
    )
    order_deltas: dict[str, int] = {}

    for sym, target_w in target_weights.items():
        curr_qty = current_positions.get(sym, 0)
        target_qty = target_quantities.get(sym, 0)

        if total_current_value <= 0 or curr_qty == 0:
            # Initial accumulation or zero position -> place order to reach target
            order_deltas[sym] = target_qty - curr_qty
            continue

        drift = weights_and_drift[sym]["drift"]

        if drift > drift_threshold:
            order_deltas[sym] = target_qty - curr_qty
        else:
            order_deltas[sym] = 0

    return order_deltas


def compute_total_budget_usd(
    account_equity: float, basket_pct_of_equity: float = DEFAULT_BASKET_PCT_OF_NAV
) -> float:
    """Total basket budget as a live fraction of real account equity -- see
    DEFAULT_BASKET_PCT_OF_NAV's own comment for why 90%, not a flat dollar
    figure or 100%."""
    if not math.isfinite(account_equity) or account_equity <= 0:
        raise ValueError(f"cannot size a budget against non-positive equity {account_equity!r}")
    return account_equity * basket_pct_of_equity


@dataclass(frozen=True)
class WeightClip:
    """One basket name's target weight was reduced to stay within
    position_cap's real, configured per-symbol ceiling. See
    `clip_weights_to_position_cap`."""

    symbol: str
    raw_weight: float
    capped_weight: float
    ceiling_usd: float


def clip_weights_to_position_cap(
    target_weights: dict[str, float],
    total_budget_usd: float,
    account_equity: float,
    position_cap_max_pct_of_equity: float,
) -> tuple[dict[str, float], dict[str, WeightClip]]:
    """Clip each name's raw inverse-vol target weight so its TOTAL target
    notional (weight * total_budget_usd -- the intended END-STATE holding,
    not just this cycle's incremental order) never exceeds
    position_cap's own configured ceiling (`account_equity *
    position_cap_max_pct_of_equity` -- the SAME figure the firewall's
    position_cap rule itself enforces, read from the real loaded policy via
    `run_once`, not a duplicated constant that could drift from it).

    Clipping the TARGET (not the order delta) is what makes this correct
    against an account that already holds real prior positions: the
    resulting order delta (`target_qty_capped - current_qty`, computed
    downstream by `compute_rebalance_orders`/`compute_target_quantities`)
    naturally lands the FINAL total holding at or under the ceiling,
    accounting for whatever is already held, not just this order's own
    increment.

    The clipped amount is NOT redistributed to other names -- it is left as
    uninvested cash, deliberately (see this module's own docstring and
    README): a simpler, more conservative rule than a second reallocation
    pass, and one that keeps every OTHER name's own inverse-vol weight
    exactly as risk-parity intended, un-perturbed by a neighbor's cap.
    """
    ceiling_usd = account_equity * position_cap_max_pct_of_equity
    ceiling_weight = ceiling_usd / total_budget_usd if total_budget_usd > 0 else math.inf

    capped_weights: dict[str, float] = {}
    clips: dict[str, WeightClip] = {}
    for sym, weight in target_weights.items():
        if weight > ceiling_weight:
            capped_weights[sym] = ceiling_weight
            clips[sym] = WeightClip(
                symbol=sym, raw_weight=weight, capped_weight=ceiling_weight, ceiling_usd=ceiling_usd
            )
        else:
            capped_weights[sym] = weight
    return capped_weights, clips


def split_order_into_chunks(qty: int, price: float, max_notional_per_chunk: float) -> list[int]:
    """Split a total share quantity into chunks whose individual notional
    (chunk_qty * price) never exceeds `max_notional_per_chunk` -- the
    mechanism requirement 1 asks for: submit multiple orders, each at or
    under notional_cap's real per-order limit, instead of one oversized
    order the firewall would reject outright. Chunk sizes sum exactly to
    `qty`; every chunk but possibly the last is exactly
    `max_shares_per_chunk` shares, so chunk count is minimal for the given
    per-order ceiling. A non-positive/non-finite price or ceiling returns a
    single, unsplit chunk (nothing left for this function to safely divide
    by) -- callers still submit that one order and let the firewall's own
    rules make the real allow/block decision, same as before chunking
    existed.
    """
    if qty <= 0:
        return []
    if (
        not math.isfinite(price)
        or price <= 0
        or not math.isfinite(max_notional_per_chunk)
        or max_notional_per_chunk <= 0
    ):
        return [qty]

    max_shares_per_chunk = max(1, math.floor(max_notional_per_chunk / price))
    chunks: list[int] = []
    remaining = qty
    while remaining > 0:
        chunk = min(remaining, max_shares_per_chunk)
        chunks.append(chunk)
        remaining -= chunk
    return chunks


class ThrottlePacer:
    """Keeps this cycle's order submissions comfortably under
    order_rate_throttle's real configured limit (policies/default.yaml:
    max_orders/window_seconds), with an explicit safety margin -- not "the
    computed chunk count happens to be less than the limit." That margin
    matters concretely here: order_rate_throttle counts hard-blocked
    attempts too (see firewall.proxy's `_track_order_lifecycle` docstring)
    and, once tripped, "stays paused... until a human explicitly resets it"
    (policies/default.yaml's own comment on that rule) -- a single retry or
    a second cycle landing inside the same window would otherwise latch the
    entire firewall shut, not just this run.

    `safety_margin` (0.75 default): this pacer never lets more than
    `floor(max_orders * safety_margin)` submissions land inside any
    trailing `window_seconds` window, sleeping before a submission that
    would exceed it. `sleep_fn`/`time_fn` are injectable so tests can
    verify pacing decisions without a real wait.
    """

    def __init__(
        self,
        max_orders: int,
        window_seconds: float,
        *,
        safety_margin: float = 0.75,
        sleep_fn: Callable[[float], Any] = asyncio.sleep,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = max(1, int(max_orders * safety_margin))
        self._window_seconds = window_seconds
        self._sleep_fn = sleep_fn
        self._time_fn = time_fn
        self._timestamps: list[float] = []

    def _prune(self, now: float) -> None:
        self._timestamps = [t for t in self._timestamps if now - t < self._window_seconds]

    async def before_submit(self) -> None:
        """Call immediately before each order submission -- awaits (sleeps)
        if needed to stay under the margin, then records this submission."""
        now = self._time_fn()
        self._prune(now)
        if len(self._timestamps) >= self._limit:
            oldest = self._timestamps[0]
            wait_seconds = self._window_seconds - (now - oldest) + 0.05
            if wait_seconds > 0:
                await self._sleep_fn(wait_seconds)
            now = self._time_fn()
            self._prune(now)
        self._timestamps.append(now)


def compute_qty(price: float, budget_usd: float) -> int:
    """Legacy helper: whole shares affordable under `budget_usd` at `price`, minimum 1."""
    if not math.isfinite(price) or price <= 0:
        raise ValueError(f"cannot size an order against non-positive price {price!r}")
    return max(1, math.floor(budget_usd / price))


def build_market_order_payload(
    symbol: str, qty: int, side: str = "buy", limit_price: float | None = None,
    client_order_id: str | None = None,
) -> dict[str, Any]:
    """A place_stock_order payload for an order.
    
    Includes limit_price when provided to allow pre-trade notional and
    position cap evaluation.
    """
    payload: dict[str, Any] = {
        "symbol": symbol,
        "side": side,
        "type": "market",
        "time_in_force": "day",
        "qty": str(abs(qty)),
    }
    if limit_price is not None:
        payload["limit_price"] = str(limit_price)
    if client_order_id is not None:
        payload["client_order_id"] = client_order_id
    return payload


def build_market_buy_payload(symbol: str, qty: int, limit_price: float | None = None) -> dict[str, Any]:
    """Backwards-compatible helper for plain buys."""
    return build_market_order_payload(symbol, qty, side="buy", limit_price=limit_price)


def build_option_order_payload(
    symbol: str, qty: int, side: str = "buy", limit_price: float | None = None,
    client_order_id: str | None = None,
) -> dict[str, Any]:
    """A place_option_order payload for an option order."""
    payload: dict[str, Any] = {
        "symbol": symbol,
        "side": side,
        "type": "market",
        "time_in_force": "day",
        "qty": str(abs(qty)),
    }
    if limit_price is not None:
        payload["limit_price"] = str(limit_price)
    if client_order_id is not None:
        payload["client_order_id"] = client_order_id
    return payload


@dataclass(frozen=True)
class OrderAttempt:
    """Outcome of proposing one basket name's order for one cycle."""

    symbol: str
    qty: int | None
    price: float | None
    forwarded: bool
    detail: str


def symbol_tool_name() -> str:
    """The stock order-placement tool."""
    return "place_stock_order"


def option_tool_name() -> str:
    """The option order-placement tool."""
    return "place_option_order"


async def fetch_current_positions(
    client: Client, basket: tuple[str, ...] = BASKET
) -> dict[str, int]:
    """Attempt to retrieve existing share counts for basket symbols from upstream.

    Falls back to 0 shares for any symbol if unqueried or unparseable.
    """
    positions = {s: 0 for s in basket}
    try:
        result = await client.call_tool("get_all_positions", {}, raise_on_error=False)
    except Exception:
        return positions

    if getattr(result, "is_error", False):
        return positions

    content = getattr(result, "content", None) or []
    if not content:
        return positions
    text = getattr(content[0], "text", None)
    if not isinstance(text, str):
        return positions

    try:
        data = json.loads(text)
        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            inner = data.get("data")
            if isinstance(inner, list):
                items = inner
            elif isinstance(inner, dict):
                res = inner.get("result") or inner.get("positions")
                if isinstance(res, list):
                    items = res
            elif "result" in data and isinstance(data["result"], list):
                items = data["result"]
        for item in items:
            if isinstance(item, dict):
                sym = item.get("symbol")
                if sym in positions:
                    qty_str = item.get("qty", "0")
                    try:
                        positions[sym] = int(float(qty_str))
                    except (ValueError, TypeError):
                        pass
    except Exception:
        pass
    return positions


async def place_basket_orders(
    client: Client,
    *,
    basket: tuple[str, ...] = BASKET,
    total_budget_usd: float | None = None,
    budget_usd: float | None = None,
    account_equity: float | None = None,
    basket_pct_of_equity: float = DEFAULT_BASKET_PCT_OF_NAV,
    position_cap_max_pct_of_equity: float | None = None,
    notional_cap_max_usd: float | None = None,
    order_rate_max_orders: int = DEFAULT_ORDER_RATE_MAX_ORDERS,
    order_rate_window_seconds: float = DEFAULT_ORDER_RATE_WINDOW_SECONDS,
    throttle_pacer: "ThrottlePacer | None" = None,
    drift_threshold: float = DEFAULT_DRIFT_THRESHOLD,
    lookback_days: int = VOLATILITY_LOOKBACK_DAYS,
    bars_fetcher: BarsFetcher = fetch_daily_bars,
    current_positions: dict[str, int] | None = None,
    include_options_overlay: bool = True,
    audit_writer: AuditLogWriter | None = None,
    equity_fetcher: Callable[[], Any] | None = None,
    exposure_snapshot_fetcher: Callable[[dict[str, float]], dict[str, Any]] | None = None,
) -> list[OrderAttempt]:
    """Propose inverse-volatility weighted rebalancing orders for the basket,
    followed by the scheduled options overlay.

    Sequential, not concurrent, AND explicitly throttle-paced (see
    `ThrottlePacer`, `order_rate_max_orders`/`order_rate_window_seconds`):
    with chunking (below), basket size alone no longer trivially guarantees
    staying inside order-rate-throttle's window the way one-order-per-name
    used to -- see this module's own docstring for the real math at this
    account's real scale.

    `total_budget_usd`/`budget_usd` explicit overrides skip the dynamic
    equity-based sizing entirely (same value every call, e.g. for
    deterministic tests). Left at their default (`None`), the total basket
    budget is `account_equity * basket_pct_of_equity` (see
    `compute_total_budget_usd`) -- `account_equity` itself defaults to a
    real fetch (`equity_fetcher`, itself defaulting to
    `firewall.account_data.fetch_session_pnl` -- the SAME shared fetcher
    the firewall's own account_equity wiring uses, not a second one) when
    not given explicitly. A failed equity fetch with no explicit budget
    aborts this cycle cleanly (one "skipped" OrderAttempt per basket name,
    no orders placed) rather than falling back to a stale flat-dollar
    guess.

    `position_cap_max_pct_of_equity`, when given (see `run_once` for how
    it's read from the REAL loaded policy, not a duplicated constant),
    clips each name's raw inverse-vol target weight to position_cap's own
    real per-symbol ceiling before any order is sized -- see
    `clip_weights_to_position_cap`. `notional_cap_max_usd`, when given (the
    real, already-resolved per-order ceiling -- see `run_once`), splits any
    resulting order whose notional would exceed it into multiple smaller
    orders -- see `split_order_into_chunks`. Both write a dedicated,
    non-blocking `verdict="info"` audit record (when `audit_writer` is
    given) disclosing exactly what happened and why, mirroring the
    scheduled overlay's own provenance-record pattern below.

    `audit_writer`, when given, is the SAME `AuditLogWriter` instance the
    real proxy's `PolicyEngine` writes to (see `run_once`) -- used to write
    one direct provenance record for the scheduled overlay's order before
    it is submitted (`rule_id="scheduled-options-overlay"`, `verdict="info"`),
    mirroring the pattern `firewall.proxy.FirewallMiddleware` already uses
    for the reactive hedge trigger. This is a second, separate record from
    the real policy verdict `PolicyEngine.evaluate()` writes for the same
    `place_option_order` call -- see module docstring's "SCHEDULED OPTIONS
    OVERLAY" section for why two records, not one. `audit_writer=None`
    (the default) simply skips these provenance records -- callers/tests
    that don't need them (e.g. exercising sizing logic only) are unaffected.
    """
    pacer = throttle_pacer or ThrottlePacer(order_rate_max_orders, order_rate_window_seconds)

    needs_equity = (total_budget_usd is None and budget_usd is None) or (
        position_cap_max_pct_of_equity is not None
    )
    equity_fetch_reason: str | None = None
    if needs_equity and account_equity is None:
        fetcher = equity_fetcher or account_data.fetch_session_pnl
        result = fetcher()
        if result.ok and result.equity is not None:
            account_equity = result.equity
        else:
            equity_fetch_reason = getattr(result, "reason", None) or "unknown reason"

    if budget_usd is not None:
        total_budget_usd = budget_usd * len(basket)
    elif total_budget_usd is None:
        if account_equity is None:
            return [
                OrderAttempt(
                    symbol=symbol,
                    qty=None,
                    price=None,
                    forwarded=False,
                    detail=(
                        "skipped -- could not fetch account equity to size the basket "
                        f"budget: {equity_fetch_reason}"
                    ),
                )
                for symbol in basket
            ]
        total_budget_usd = compute_total_budget_usd(account_equity, basket_pct_of_equity)

    prices: dict[str, float] = {}
    volatilities: dict[str, float] = {}
    attempts: list[OrderAttempt] = []
    failed_symbols: set[str] = set()

    for symbol in basket:
        bars = bars_fetcher(symbol, lookback_days)
        if not bars.ok or not bars.closes:
            reason = bars.reason if not bars.ok else "no closes returned"
            attempts.append(
                OrderAttempt(
                    symbol=symbol,
                    qty=None,
                    price=None,
                    forwarded=False,
                    detail=f"skipped -- could not price/vol {symbol}: {reason}",
                )
            )
            failed_symbols.add(symbol)
            continue

        prices[symbol] = bars.closes[-1]
        try:
            volatilities[symbol] = compute_realized_volatility(bars.closes)
        except Exception as exc:
            attempts.append(
                OrderAttempt(
                    symbol=symbol,
                    qty=None,
                    price=prices[symbol],
                    forwarded=False,
                    detail=f"skipped -- volatility calculation failed for {symbol}: {exc}",
                )
            )
            failed_symbols.add(symbol)

    priced_basket = tuple(s for s in basket if s not in failed_symbols)
    if not priced_basket:
        return attempts

    target_weights = compute_inverse_vol_weights(volatilities)

    weight_clips: dict[str, WeightClip] = {}
    if position_cap_max_pct_of_equity is not None and account_equity is not None:
        target_weights, weight_clips = clip_weights_to_position_cap(
            target_weights, total_budget_usd, account_equity, position_cap_max_pct_of_equity
        )
        for clip in weight_clips.values():
            if audit_writer is None:
                continue
            audit_writer.append(
                tool_name="basket_rebalance:weight_clipped",
                arguments={
                    "symbol": clip.symbol,
                    "raw_target_weight": clip.raw_weight,
                    "capped_target_weight": clip.capped_weight,
                    "ceiling_usd": clip.ceiling_usd,
                    "total_budget_usd": total_budget_usd,
                },
                verdict="info",
                reason=(
                    f"{clip.symbol}'s raw inverse-vol target weight {clip.raw_weight:.1%} "
                    f"would target ${clip.raw_weight * total_budget_usd:,.2f}, over "
                    f"position_cap's ${clip.ceiling_usd:,.2f} per-symbol ceiling "
                    f"({position_cap_max_pct_of_equity:.1%} of equity) -- clipped to "
                    f"{clip.capped_weight:.1%} (${clip.ceiling_usd:,.2f}). The difference "
                    "is left as uninvested cash, not redistributed to other names."
                ),
                forwarded=None,
                upstream_status="not_forwarded",
                rule_id="basket-rebalance-position-cap-clip",
                regulation_ref=None,
            )

    target_quantities = compute_target_quantities(target_weights, prices, total_budget_usd)

    snapshot: dict[str, Any]
    if exposure_snapshot_fetcher is not None:
        snapshot = exposure_snapshot_fetcher(prices)
    elif current_positions is None:
        snapshot = account_data.fetch_consistent_exposure_snapshot(prices)
    else:
        positions_result = account_data.PositionsResult(
            ok=True,
            positions={symbol: current_positions.get(symbol, 0) * prices.get(symbol, 0.0) for symbol in basket},
            quantities={symbol: float(current_positions.get(symbol, 0)) for symbol in basket},
            current_prices=prices,
            fetched_at=time.time(),
        )
        snapshot = account_data.exposure_snapshot(
            positions_result,
            account_data.OpenOrdersResult(ok=True, orders=(), aggregate_outstanding_notional=0.0),
        )
    if not snapshot.get("ok"):
        reason = snapshot.get("reason") or "exposure reconciliation unavailable"
        return attempts + [
            OrderAttempt(symbol=symbol, qty=None, price=prices.get(symbol), forwarded=False,
                         detail=f"skipped -- exposure reconciliation failed closed: {reason}")
            for symbol in priced_basket
        ]
    current_positions = {
        symbol: float(snapshot["positions"].get(symbol, 0.0)) for symbol in basket
    }
    committed_positions = {
        symbol: current_positions.get(symbol, 0.0)
        + float(snapshot["pending_signed_qty"].get(symbol, 0.0))
        for symbol in basket
    }

    order_deltas = compute_rebalance_orders(
        current_positions=committed_positions,
        target_quantities=target_quantities,
        target_weights=target_weights,
        prices=prices,
        drift_threshold=drift_threshold,
    )
    for symbol in basket:
        if abs(float(snapshot["pending_signed_qty"].get(symbol, 0.0))) > 1e-9:
            order_deltas[symbol] = target_quantities.get(symbol, 0) - committed_positions.get(symbol, 0.0)
    order_deltas = {symbol: int(delta) for symbol, delta in order_deltas.items()}

    # 1. Place stock rebalancing orders, chunked to stay within
    # notional_cap's real per-order ceiling (see split_order_into_chunks),
    # and paced to stay under order_rate_throttle's real limit (see
    # ThrottlePacer).
    notional_cap_ceiling = notional_cap_max_usd if notional_cap_max_usd is not None else math.inf

    for symbol in priced_basket:
        delta = order_deltas.get(symbol, 0)
        price = prices[symbol]
        curr_qty = current_positions.get(symbol, 0)
        target_qty = target_quantities.get(symbol, 0)
        weight = target_weights.get(symbol, 0.0)
        clip = weight_clips.get(symbol)
        clip_suffix = f", clipped from raw {clip.raw_weight:.1%}" if clip is not None else ""

        if delta == 0:
            attempts.append(
                OrderAttempt(
                    symbol=symbol,
                    qty=0,
                    price=price,
                    forwarded=True,
                    detail=(
                        f"no rebalance needed for {symbol}: drift within {drift_threshold:.1%} "
                        f"threshold (holding {curr_qty} share(s), target {target_qty}, "
                        f"weight {weight:.1%}{clip_suffix})"
                    ),
                )
            )
            continue

        side = "buy" if delta > 0 else "sell"
        order_qty = abs(delta)
        chunk_sizes = split_order_into_chunks(order_qty, price, notional_cap_ceiling)

        if audit_writer is not None and len(chunk_sizes) > 1:
            audit_writer.append(
                tool_name="basket_rebalance:order_chunked",
                arguments={
                    "symbol": symbol,
                    "side": side,
                    "total_qty": order_qty,
                    "total_notional": order_qty * price,
                    "chunk_count": len(chunk_sizes),
                    "chunk_sizes": chunk_sizes,
                    "chunk_notionals": [c * price for c in chunk_sizes],
                    "per_chunk_ceiling_usd": notional_cap_ceiling,
                },
                verdict="info",
                reason=(
                    f"{symbol}: {order_qty} share(s) (~${order_qty * price:,.2f}) exceeds "
                    f"notional_cap's ${notional_cap_ceiling:,.2f} per-order ceiling -- split "
                    f"into {len(chunk_sizes)} chunk(s) of at most {max(chunk_sizes)} share(s) "
                    f"each (~${max(chunk_sizes) * price:,.2f}), submitted sequentially through "
                    "the same firewall path."
                ),
                forwarded=None,
                upstream_status="not_forwarded",
                rule_id="basket-rebalance-chunking",
                regulation_ref=None,
            )

        for chunk_index, chunk_qty in enumerate(chunk_sizes, start=1):
            chunk_label = f"chunk {chunk_index}/{len(chunk_sizes)} of " if len(chunk_sizes) > 1 else ""
            # qty-only, no limit_price: verified live against the real
            # Alpaca paper API (2026-08-29) that a type="market" order
            # carrying limit_price is rejected outright (HTTP 422, code
            # 40010001, "market orders require no stop or limit price") --
            # this is not a firewall-side design choice, it's an invalid
            # order shape. See the module docstring's "WHY EVERY ORDER HERE
            # IS A PLAIN MARKET ORDER" section.
            payload = build_market_order_payload(symbol, chunk_qty, side=side)
            payload["_firewall_reconciliation"] = {
                "target_qty": target_qty,
                "snapshot_fingerprint": snapshot["fingerprint"],
            }

            await pacer.before_submit()
            try:
                result = await client.call_tool(symbol_tool_name(), payload, raise_on_error=False)
            except Exception as exc:
                attempts.append(
                    OrderAttempt(
                        symbol=symbol,
                        qty=chunk_qty,
                        price=price,
                        forwarded=False,
                        detail=f"transport error placing {chunk_label}order: {exc}",
                    )
                )
                continue

            if getattr(result, "is_error", False):
                attempts.append(
                    OrderAttempt(
                        symbol=symbol,
                        qty=chunk_qty,
                        price=price,
                        forwarded=False,
                        detail=f"blocked or rejected ({chunk_label}{symbol}): {result}",
                    )
                )
                continue

            attempts.append(
                OrderAttempt(
                    symbol=symbol,
                    qty=chunk_qty,
                    price=price,
                    forwarded=True,
                    detail=(
                        f"placed {side} order for {chunk_qty} share(s) of {symbol} "
                        f"({chunk_label}~${price:,.2f}/share, target weight "
                        f"{weight:.1%}{clip_suffix})"
                    ),
                )
            )

    # 2. Propose scheduled options overlay on largest position
    if include_options_overlay:
        overlay_positions = dict(current_positions)
        for sym, delta in order_deltas.items():
            overlay_positions[sym] = overlay_positions.get(sym, 0) + delta

        overlay = compute_scheduled_overlay(overlay_positions, prices)
        if overlay is not None:
            # qty-only, no limit_price -- the stock leg above's
            # type="market" + limit_price rejection (HTTP 422, code
            # 40010001) was verified live against the real Alpaca paper
            # API; this option leg carries the identical shape (type=
            # "market" with limit_price attached) and is reverted on the
            # inference that Alpaca's order-type validation isn't
            # equity-specific -- NOT independently live-verified for
            # place_option_order specifically.
            option_payload = build_option_order_payload(
                overlay.occ_symbol,
                overlay.contracts,
                side="buy",
            )
            if audit_writer is not None:
                # Written BEFORE submission, unconditionally: this is a
                # provenance record ("this order came from the scheduled
                # overlay"), not a policy decision -- it must survive
                # regardless of what the real PolicyEngine.evaluate() call
                # below decides (allow, soft_block, or hard_block). See
                # this function's own docstring and module docstring's
                # "SCHEDULED OPTIONS OVERLAY" section for why this is a
                # second, separate record rather than a rewrite of the
                # real verdict's rule_id.
                audit_writer.append(
                    tool_name="scheduled_overlay:proposed",
                    arguments={
                        "symbol": overlay.symbol,
                        "occ_symbol": overlay.occ_symbol,
                        "contracts": overlay.contracts,
                        "strike": overlay.strike,
                        "target_expiry": overlay.target_expiry,
                    },
                    verdict="info",
                    reason=overlay.reason,
                    forwarded=None,
                    upstream_status="not_forwarded",
                    rule_id="scheduled-options-overlay",
                    regulation_ref=None,
                )
            try:
                await pacer.before_submit()
                result = await client.call_tool(
                    option_tool_name(), option_payload, raise_on_error=False
                )
                forwarded = not getattr(result, "is_error", False)
                detail = (
                    f"scheduled options overlay: placed BUY {overlay.contracts} PUT contract(s) on "
                    f"{overlay.symbol} ({overlay.occ_symbol}) strike ${overlay.strike:,.2f} "
                    f"expiry {overlay.target_expiry}"
                    if forwarded
                    else f"scheduled options overlay proposed ({overlay.occ_symbol}): {result}"
                )
                attempts.append(
                    OrderAttempt(
                        symbol=overlay.occ_symbol,
                        qty=overlay.contracts,
                        price=overlay.strike,
                        forwarded=forwarded,
                        detail=detail,
                    )
                )
            except Exception as exc:
                attempts.append(
                    OrderAttempt(
                        symbol=overlay.occ_symbol,
                        qty=overlay.contracts,
                        price=overlay.strike,
                        forwarded=False,
                        detail=f"scheduled options overlay transport error: {exc}",
                    )
                )

    return attempts


def _read_dynamic_policy_config(engine: Any) -> dict[str, Any]:
    """Read the REAL, loaded policy's notional_cap/position_cap/
    order_rate_throttle configuration directly from `engine.rules` --
    a single source of truth so core_strategy's own clip/chunk/pacing math
    can never silently drift from what the firewall actually enforces (the
    same reasoning `firewall.proxy.FirewallMiddleware._find_rule` already
    established for reading `hedge-proposal`/`cvar-gate`/
    `drawdown-killswitch` rule instances directly, not duplicated
    constants).

    One account_equity fetch here (via `account_data.fetch_session_pnl`,
    cached ~5s -- see `account_data.DEFAULT_CACHE_TTL_SECONDS`) resolves
    notional_cap's real EFFECTIVE per-order ceiling (mirroring
    `NotionalCapRule._effective_cap`'s own equity-vs-static-fallback
    logic exactly, not re-deriving it independently) -- `place_basket_
    orders`'s own later equity fetch for budget/clip sizing hits this same
    cache, not a second real network round trip.
    """
    from firewall.rules.notional_cap import NotionalCapRule
    from firewall.rules.order_rate_throttle import OrderRateThrottleRule
    from firewall.rules.position_cap import PositionCapRule

    config: dict[str, Any] = {}

    account_equity: float | None = None
    equity_result = account_data.fetch_session_pnl()
    if equity_result.ok and equity_result.equity is not None:
        account_equity = equity_result.equity
    config["account_equity"] = account_equity

    for rule in engine.rules:
        if isinstance(rule, PositionCapRule):
            config["position_cap_max_pct_of_equity"] = rule.cfg.max_pct_of_equity
        elif isinstance(rule, NotionalCapRule):
            if rule.cfg.max_pct_of_equity is not None and account_equity is not None:
                config["notional_cap_max_usd"] = account_equity * rule.cfg.max_pct_of_equity
            else:
                config["notional_cap_max_usd"] = rule.cfg.max_usd
        elif isinstance(rule, OrderRateThrottleRule):
            config["order_rate_max_orders"] = rule.cfg.max_orders
            config["order_rate_window_seconds"] = rule.cfg.window_seconds

    return config


async def run_once(
    *,
    proxy: FastMCP | None = None,
    basket: tuple[str, ...] = BASKET,
    total_budget_usd: float | None = None,
    budget_usd: float | None = None,
    account_equity: float | None = None,
    basket_pct_of_equity: float = DEFAULT_BASKET_PCT_OF_NAV,
    position_cap_max_pct_of_equity: float | None = None,
    notional_cap_max_usd: float | None = None,
    order_rate_max_orders: int = DEFAULT_ORDER_RATE_MAX_ORDERS,
    order_rate_window_seconds: float = DEFAULT_ORDER_RATE_WINDOW_SECONDS,
    drift_threshold: float = DEFAULT_DRIFT_THRESHOLD,
    lookback_days: int = VOLATILITY_LOOKBACK_DAYS,
    bars_fetcher: BarsFetcher = fetch_daily_bars,
    current_positions: dict[str, int] | None = None,
    include_options_overlay: bool = True,
    audit_writer: AuditLogWriter | None = None,
) -> list[OrderAttempt]:
    """One rebalancing cycle: connect to the real firewall proxy and propose the
    basket's orders and options overlay through it.

    `proxy` defaults to `firewall.proxy.build_proxy()` -- the exact same
    proxy construction every other caller of this repo uses, wired to the
    real default policy and the real Alpaca paper account (subject to
    `build_proxy`'s own `ALPACA_PAPER_TRADE` guard). Tests pass an explicit
    proxy (built against a fake upstream) instead, the same pattern
    tests/test_proxy.py already establishes.

    When `proxy` is left at its default, this builds the policy engine
    itself (rather than letting `build_proxy()` build one internally and
    discard the reference) purely so it can (a) hand `place_basket_orders`
    the SAME `AuditLogWriter` instance the real proxy's `PolicyEngine`
    writes to -- needed for the scheduled overlay's provenance record (see
    `place_basket_orders`'s docstring) -- and (b) read the real
    notional_cap/position_cap/order_rate_throttle configuration directly
    off that engine's own rule instances (see `_read_dynamic_policy_
    config`) for the same account_equity-based sizing, clipping, and
    pacing the firewall itself will independently enforce. Any of
    `account_equity`/`position_cap_max_pct_of_equity`/
    `notional_cap_max_usd`/`order_rate_max_orders`/
    `order_rate_window_seconds` passed explicitly here overrides that
    real-engine read. Passing an explicit `proxy` bypasses the real-engine
    read entirely -- pass those parameters (and `audit_writer`) explicitly
    alongside it if this behavior is wanted against a fake upstream too
    (the same pattern `tests/test_core_strategy.py` uses).
    """
    if proxy is None:
        from firewall.proxy import _default_policy_engine, build_proxy

        engine = _default_policy_engine()
        proxy = build_proxy(policy_engine=engine)
        audit_writer = engine.audit_writer

        dynamic = _read_dynamic_policy_config(engine)
        if account_equity is None:
            account_equity = dynamic.get("account_equity")
        if position_cap_max_pct_of_equity is None:
            position_cap_max_pct_of_equity = dynamic.get("position_cap_max_pct_of_equity")
        if notional_cap_max_usd is None:
            notional_cap_max_usd = dynamic.get("notional_cap_max_usd")
        if "order_rate_max_orders" in dynamic:
            order_rate_max_orders = dynamic["order_rate_max_orders"]
        if "order_rate_window_seconds" in dynamic:
            order_rate_window_seconds = dynamic["order_rate_window_seconds"]

    async with Client(proxy) as client:
        return await place_basket_orders(
            client,
            basket=basket,
            total_budget_usd=total_budget_usd,
            budget_usd=budget_usd,
            account_equity=account_equity,
            basket_pct_of_equity=basket_pct_of_equity,
            position_cap_max_pct_of_equity=position_cap_max_pct_of_equity,
            notional_cap_max_usd=notional_cap_max_usd,
            order_rate_max_orders=order_rate_max_orders,
            order_rate_window_seconds=order_rate_window_seconds,
            drift_threshold=drift_threshold,
            lookback_days=lookback_days,
            bars_fetcher=bars_fetcher,
            current_positions=current_positions,
            include_options_overlay=include_options_overlay,
            audit_writer=audit_writer,
        )


async def run_forever(
    *,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    cycles: int | None = None,
    **run_once_kwargs: Any,
) -> None:
    """Repeat `run_once` every `interval_seconds`, forever or for `cycles`
    cycles. Each cycle builds its own proxy/client so a single upstream
    hiccup in one cycle can't wedge every subsequent one."""
    completed = 0
    while cycles is None or completed < cycles:
        attempts = await run_once(**run_once_kwargs)
        for attempt in attempts:
            print(f"[core_strategy] {attempt.detail}", flush=True)
        completed += 1
        if cycles is not None and completed >= cycles:
            break
        await asyncio.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--loop",
        action="store_true",
        help="repeat on a fixed interval instead of running once",
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"cadence between cycles when --loop is set (default: {DEFAULT_INTERVAL_SECONDS}s)",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=None,
        help="stop after this many cycles when --loop is set (default: run forever)",
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=None,
        help=(
            "total basket budget in USD (default: unset -- computed dynamically "
            f"as {DEFAULT_BASKET_PCT_OF_NAV:.0%} of the real, live account equity "
            "each cycle, the same way cvar_gate/hedge_cost_cap size their own "
            "thresholds; see DEFAULT_BASKET_PCT_OF_NAV's own comment)"
        ),
    )
    parser.add_argument(
        "--drift-threshold",
        type=float,
        default=DEFAULT_DRIFT_THRESHOLD,
        help=f"weight drift threshold to trigger rebalancing (default: {DEFAULT_DRIFT_THRESHOLD})",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=VOLATILITY_LOOKBACK_DAYS,
        help=f"historical lookback days for realized volatility (default: {VOLATILITY_LOOKBACK_DAYS})",
    )
    parser.add_argument(
        "--no-overlay",
        action="store_true",
        help="disable the scheduled options overlay",
    )
    args = parser.parse_args()

    if args.loop:
        asyncio.run(
            run_forever(
                interval_seconds=args.interval_seconds,
                cycles=args.cycles,
                total_budget_usd=args.budget,
                drift_threshold=args.drift_threshold,
                lookback_days=args.lookback_days,
                include_options_overlay=not args.no_overlay,
            )
        )
    else:
        attempts = asyncio.run(
            run_once(
                total_budget_usd=args.budget,
                drift_threshold=args.drift_threshold,
                lookback_days=args.lookback_days,
                include_options_overlay=not args.no_overlay,
            )
        )
        for attempt in attempts:
            print(f"[core_strategy] {attempt.detail}")


if __name__ == "__main__":
    main()
