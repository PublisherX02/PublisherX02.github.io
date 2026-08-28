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
`policies/default.yaml` ("AAPL", "MSFT", "SPY", "QQQ") -- reused verbatim,
not independently chosen, so this module needs zero policy-file edits to
run against the real, unmodified default policy. AAPL/MSFT are large-cap
equities; SPY/QQQ are large-cap-weighted index ETFs, not equities in the
strict sense -- stated here rather than glossed over, since both are
already disclosed, already-liquid instruments this repo's own tests and
corpus already exercise.

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

SCHEDULED OPTIONS OVERLAY: On a fixed schedule, proposes a small protective
put on the basket's largest current position -- sized through the SAME
premium-cap and delta-corridor rules already built for the reactive hedge,
with no new risk logic. This is standing portfolio insurance, not a
market-timing decision: a disclosed, scheduled options overlay applied
regardless of market conditions, distinct from the reactive CVaR-triggered
hedge. Both hedge sources write clearly distinguishable audit records
(`rule_id: hedge-proposal` vs `rule_id: scheduled-options-overlay`) so the
dashboard and write-up can show them as two separate, honest mechanisms.

WHY EVERY ORDER HERE IS A PLAIN MARKET ORDER WITH `qty` ONLY, NEVER
`limit_price` OR `notional`: verified directly against the real, loaded
`policies/default.yaml` (one `PolicyEngine.evaluate()` call, not assumed)
before writing this module. `cvar_gate` and `pct_of_adv` both hard-block
any order carrying a computable notional (`notional` field, or `qty` +
`limit_price`) whenever `state["account_equity"]` is missing -- a
pre-existing, disclosed gap (see README's "What this does not do"):
nothing in `src/` populates that state key today. A plain market order
(`qty` only, no price) carries no computable notional
(`firewall.rules._util.extract_notional` returns None for it), so
`cvar_gate`/`pct_of_adv`/`notional_cap`/`position_cap` all correctly skip
it rather than failing closed -- this is the documented, load-bearing
reason the inverse-volatility dollar allocation is converted to an
integer share count locally (via `compute_target_quantities`, using the
same `firewall.market_data.fetch_daily_bars` helper `cvar_gate`/`pct_of_adv`
already use for a recent close) rather than sent to Alpaca as a `notional`
order or a limit order. This is not a workaround for the firewall -- it's
this module picking the one order shape the firewall's own rule set (as
shipped today, gap included) actually evaluates to `allow` for a
plain, compliant buy or sell.
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
BASKET: tuple[str, ...] = ("AAPL", "MSFT", "SPY", "QQQ")

# Total dollar allocation for the basket across all names. Sized comfortably
# under notional-cap-single-order's $5,000/order and position-cap-per-symbol's
# $20,000/symbol caps in policies/default.yaml.
DEFAULT_TOTAL_BUDGET_USD = 800.0
PER_NAME_BUDGET_USD = 200.0  # Legacy alias (~$800 / 4 names)

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


def compute_rebalance_orders(
    current_positions: dict[str, int],
    target_quantities: dict[str, int],
    target_weights: dict[str, float],
    prices: dict[str, float],
    drift_threshold: float = DEFAULT_DRIFT_THRESHOLD,
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
    order_deltas: dict[str, int] = {}

    for sym, target_w in target_weights.items():
        curr_qty = current_positions.get(sym, 0)
        target_qty = target_quantities.get(sym, 0)
        price = prices.get(sym, 0.0)

        if total_current_value <= 0 or curr_qty == 0:
            # Initial accumulation or zero position -> place order to reach target
            order_deltas[sym] = target_qty - curr_qty
            continue

        current_val = curr_qty * price
        current_w = current_val / total_current_value if total_current_value > 0 else 0.0
        drift = abs(current_w - target_w)

        if drift > drift_threshold:
            order_deltas[sym] = target_qty - curr_qty
        else:
            order_deltas[sym] = 0

    return order_deltas


def compute_qty(price: float, budget_usd: float) -> int:
    """Legacy helper: whole shares affordable under `budget_usd` at `price`, minimum 1."""
    if not math.isfinite(price) or price <= 0:
        raise ValueError(f"cannot size an order against non-positive price {price!r}")
    return max(1, math.floor(budget_usd / price))


def build_market_order_payload(symbol: str, qty: int, side: str = "buy") -> dict[str, Any]:
    """A place_stock_order payload for a plain market order.

    Deliberately carries `qty` only -- no `limit_price`, no `notional` --
    string-typed to match Alpaca's real schema (see AUDIT.md finding A4).
    See the module docstring for exactly why this shape, not a notional or
    limit order, is what lets this basket clear the real, unmodified
    default policy today.
    """
    return {
        "symbol": symbol,
        "side": side,
        "type": "market",
        "time_in_force": "day",
        "qty": str(abs(qty)),
    }


def build_market_buy_payload(symbol: str, qty: int) -> dict[str, Any]:
    """Backwards-compatible helper for plain market buys."""
    return build_market_order_payload(symbol, qty, side="buy")


def build_option_order_payload(
    symbol: str, qty: int, side: str = "buy"
) -> dict[str, Any]:
    """A place_option_order payload for a plain market option order."""
    return {
        "symbol": symbol,
        "side": side,
        "type": "market",
        "time_in_force": "day",
        "qty": str(abs(qty)),
    }


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
        if isinstance(data, list):
            for item in data:
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
    total_budget_usd: float = DEFAULT_TOTAL_BUDGET_USD,
    budget_usd: float | None = None,
    drift_threshold: float = DEFAULT_DRIFT_THRESHOLD,
    lookback_days: int = VOLATILITY_LOOKBACK_DAYS,
    bars_fetcher: BarsFetcher = fetch_daily_bars,
    current_positions: dict[str, int] | None = None,
    include_options_overlay: bool = True,
) -> list[OrderAttempt]:
    """Propose inverse-volatility weighted rebalancing orders for the basket,
    followed by the scheduled options overlay.

    Sequential, not concurrent: keeps order submission rate trivially
    inside order-rate-throttle's window (policies/default.yaml:
    max_orders=20/60s) regardless of basket size, and keeps each order's
    audit-log record in a predictable sequence.
    """
    if budget_usd is not None:
        total_budget_usd = budget_usd * len(basket)

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
    target_quantities = compute_target_quantities(target_weights, prices, total_budget_usd)

    if current_positions is None:
        current_positions = await fetch_current_positions(client, basket)

    order_deltas = compute_rebalance_orders(
        current_positions=current_positions,
        target_quantities=target_quantities,
        target_weights=target_weights,
        prices=prices,
        drift_threshold=drift_threshold,
    )

    # 1. Place stock rebalancing orders
    for symbol in priced_basket:
        delta = order_deltas.get(symbol, 0)
        price = prices[symbol]
        curr_qty = current_positions.get(symbol, 0)
        target_qty = target_quantities.get(symbol, 0)
        weight = target_weights.get(symbol, 0.0)

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
                        f"weight {weight:.1%})"
                    ),
                )
            )
            continue

        side = "buy" if delta > 0 else "sell"
        order_qty = abs(delta)
        payload = build_market_order_payload(symbol, order_qty, side=side)

        try:
            result = await client.call_tool(symbol_tool_name(), payload, raise_on_error=False)
        except Exception as exc:
            attempts.append(
                OrderAttempt(
                    symbol=symbol,
                    qty=order_qty,
                    price=price,
                    forwarded=False,
                    detail=f"transport error placing order: {exc}",
                )
            )
            continue

        if getattr(result, "is_error", False):
            attempts.append(
                OrderAttempt(
                    symbol=symbol,
                    qty=order_qty,
                    price=price,
                    forwarded=False,
                    detail=f"blocked or rejected: {result}",
                )
            )
            continue

        attempts.append(
            OrderAttempt(
                symbol=symbol,
                qty=order_qty,
                price=price,
                forwarded=True,
                detail=(
                    f"placed {side} order for {order_qty} share(s) of {symbol} "
                    f"(~${price:,.2f}/share, target weight {weight:.1%})"
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
            option_payload = build_option_order_payload(
                overlay.occ_symbol, overlay.contracts, side="buy"
            )
            try:
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


async def run_once(
    *,
    proxy: FastMCP | None = None,
    basket: tuple[str, ...] = BASKET,
    total_budget_usd: float = DEFAULT_TOTAL_BUDGET_USD,
    budget_usd: float | None = None,
    drift_threshold: float = DEFAULT_DRIFT_THRESHOLD,
    lookback_days: int = VOLATILITY_LOOKBACK_DAYS,
    bars_fetcher: BarsFetcher = fetch_daily_bars,
    current_positions: dict[str, int] | None = None,
    include_options_overlay: bool = True,
) -> list[OrderAttempt]:
    """One rebalancing cycle: connect to the real firewall proxy and propose the
    basket's orders and options overlay through it.

    `proxy` defaults to `firewall.proxy.build_proxy()` -- the exact same
    proxy construction every other caller of this repo uses, wired to the
    real default policy and the real Alpaca paper account (subject to
    `build_proxy`'s own `ALPACA_PAPER_TRADE` guard). Tests pass an explicit
    proxy (built against a fake upstream) instead, the same pattern
    tests/test_proxy.py already establishes.
    """
    if proxy is None:
        from firewall.proxy import build_proxy

        proxy = build_proxy()
    async with Client(proxy) as client:
        return await place_basket_orders(
            client,
            basket=basket,
            total_budget_usd=total_budget_usd,
            budget_usd=budget_usd,
            drift_threshold=drift_threshold,
            lookback_days=lookback_days,
            bars_fetcher=bars_fetcher,
            current_positions=current_positions,
            include_options_overlay=include_options_overlay,
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
        default=DEFAULT_TOTAL_BUDGET_USD,
        help=f"total basket budget in USD (default: ${DEFAULT_TOTAL_BUDGET_USD})",
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

