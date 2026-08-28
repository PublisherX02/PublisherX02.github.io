"""hedge_proposal -- a single new triggered action, not a new pillar:
when cvar_gate's computed tail-loss estimate or drawdown_killswitch's
session-PnL proximity crosses a configured early-warning threshold, this
computes ONE defined protective options structure -- a protective put on
the single position contributing most to the flagged risk figure -- via
a disclosed, mechanical formula. It never invents a second, parallel risk
calculation: the cvar_gate trigger reuses `compute_cvar`/`_log_returns`
and the live `CVaRGateRule` instance's own `.cfg`/`._bars_fetcher`
directly (same pattern `sizing_resolver.py` already established for
reusing cvar_gate's math without duplicating it); the drawdown trigger
reads `state["session_pnl_usd"]` against the live `DrawdownKillswitchRule`
instance's own `.cfg.session_pnl_threshold_usd` -- the exact same numbers
those two rules already compute, not a re-derivation.

Not a market view, not a forecast -- a defensive response to an
already-measured risk number: strike/expiry/quantity are all produced by
`_mechanical_strike`/`_mechanical_expiry`/`_mechanical_contracts`, three
pure functions of (current price, flagged notional, and this rule's own
configured percentages), never of any forecast, momentum signal, or
market opinion. Every `HedgeProposal.reason` states this explicitly, and
every place this feature is documented (this docstring, README.md,
AUDIT.md) must describe it the same way.

DETECTION + AUDIT ONLY -- nothing is ever submitted. This firewall has no
approval-token preview -> token -> submit flow for ANY order today (see
README's "What this does not do" -- verified against the full codebase
and git history before this feature was built: no such flow exists to
plug a hedge proposal into). A proposal computed here must go through
that exact flow, with no exception for being "protective," once it
exists -- and since it doesn't exist yet, this module can only ever
compute and report a proposal, never place one. `compute_proposal()`
returns a `HedgeProposal`; nothing in this module calls any order-
placement tool, and nothing in it should ever be changed to do so without
that flow existing first.

HedgeProposalRule.check() is intentionally always a no-op (returns
`RuleOutcome(False)`, never blocks, never appears as a Warning) -- it
exists as a `Rule` subclass purely so its own configuration
(`policies/default.yaml`'s `hedge-proposal` entry) loads through the
exact same `_Params`/`RuleConfig` machinery every other rule uses, with
no bespoke YAML section and no change to `PolicyConfig`. The real output
comes from `compute_proposal()`, called directly by
`FirewallMiddleware` after `PolicyEngine.evaluate()` returns, which writes
its own audit record directly rather than going through the normal
RuleOutcome -> Warning -> audit pipeline every other soft rule uses. Why:
`PolicyEngine.evaluate()` only ever writes accumulated `Warning`s to the
audit log via `record_call_pending`/`record_call_outcome`, and BOTH
explicitly no-op whenever the SAME call's final verdict is `hard_block`
(see policy.py) -- a warning from an earlier soft rule is silently
dropped the moment ANY later rule on the same call hard-blocks, and rule
ordering cannot fix this (a hard block from a rule anywhere in the list,
before or after, has the same effect). That dropped case is exactly the
one that matters most here: `cvar_gate` hard-blocking on a large
tail-loss estimate is precisely when a hedge proposal has the most value.
A mechanism that goes silent exactly at its own trigger condition would
be worse than not having it, so this writes directly instead (mirroring
`FirewallMiddleware._populate_session_pnl`'s fetch-failure-visibility
precedent), independent of whatever else the same call's verdict is.

Known, disclosed gap -- not a silent one: the `cvar_gate` trigger needs
`state["account_equity"]`, and nothing anywhere in `src/` populates that
key in the live proxy today (the same gap `cvar_gate` itself has,
documented in `FirewallMiddleware`'s own docstring and AUDIT.md). This
trigger is therefore structurally dormant in production right now --
correct by construction (unit-tested directly, see
`tests/rules/test_hedge_proposal.py`) but currently unreachable through
`build_proxy()`. The `drawdown_killswitch` trigger has no such gap:
`session_pnl_usd` is populated on every order-related call (see
`firewall.account_data`), and this trigger is proven end-to-end through
the real proxy in `tests/test_proxy.py`.

"Position contributing most" is computed differently per trigger, because
the two rules compute genuinely different shapes of data and this module
must not invent a third:

  - cvar_gate's own CVaR estimate is already scoped to a single symbol
    (the one in the order being evaluated) -- there's no ambiguity to
    resolve.
  - drawdown_killswitch's `session_pnl_usd` is a single account-wide
    scalar with no per-symbol breakdown at all. `order_history` (Fix 2)
    supplies the "which position" context here:
    `_largest_open_position_from_history` picks the symbol with the
    largest absolute net QUANTITY across recorded, still-live order
    events (buys minus sells, excluding cancelled/blocked/rejected
    outcomes) -- deliberately not net notional, and deliberately not
    gated on `event.price`. A market order (the most common real order
    shape) never carries a `limit_price`, so `OrderEvent.price` is `None`
    for it; requiring price at this step silently excluded exactly that
    shape from ever being flagged (see AUDIT.md's "silently blind to
    plain market orders" finding, fixed here). Quantity alone is enough
    to identify *which* symbol is largest; once picked, the caller fetches
    a CURRENT price for that one symbol via the shared market_data bars
    fetcher (the same one already used to size the mechanical strike) and
    multiplies by net quantity to get the flagged notional -- never a
    historical price captured at order-placement time. This is explicitly
    an approximation -- largest recorded live *order* exposure, not a
    true per-symbol realized-loss attribution, since `order_history`
    records order events (qty/price/outcome), not fills' realized P&L. It
    is not a second risk calculation: it estimates no risk or P&L at all,
    only identifies which symbol to hedge, from data `order_history`
    already provides.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel

from firewall.market_data import BarsResult, fetch_daily_bars
from firewall.order_history import OrderHistory
from firewall.rules._util import extract_notional, matches_any
from firewall.rules.base import Rule, RuleConfig, RuleOutcome
from firewall.rules.cvar_gate import CVaRGateRule, _log_returns, compute_cvar
from firewall.rules.drawdown_killswitch import DrawdownKillswitchRule

# Same broad pattern drawdown_killswitch/cvar_gate's own default tool_match
# uses -- this feature only makes sense to evaluate on order-related calls.
_ORDER_RELATED_TOOLS = ("order",)


class _Params(BaseModel):
    # Propose a hedge once cvar_gate's own CVaR estimate reaches this
    # fraction of cvar_gate's own max_loss_usd -- deliberately configurable
    # separately from cvar_gate's own hard-block point (0.8 means "warn
    # before the hard cap, not only exactly at it").
    cvar_trigger_pct_of_max_loss: float = 0.8
    # Propose a hedge once session_pnl_usd reaches this fraction of
    # drawdown_killswitch's own session_pnl_threshold_usd.
    drawdown_trigger_pct_of_threshold: float = 0.8
    # Mechanical strike selection: strike = current_price * (1 - otm_pct).
    otm_pct: float = 0.05
    # Mechanical expiry selection: target date is the midpoint of this
    # window from now. The nearest real listed expiry to that date must be
    # picked by a human -- no options-chain lookup is performed here.
    expiry_min_days: int = 14
    expiry_max_days: int = 45
    # Mechanical sizing: contracts cover this fraction of the flagged
    # position's notional (one contract = 100 shares).
    coverage_pct: float = 0.5


@dataclass
class HedgeProposal:
    trigger: str  # "cvar_gate" | "drawdown_killswitch"
    symbol: str
    current_price: float
    strike: float
    target_expiry: str  # ISO date, a target -- not a verified listed expiry
    contracts: int
    flagged_notional: float
    reason: str


@dataclass
class ScheduledOverlayProposal:
    symbol: str
    current_price: float
    strike: float
    target_expiry: str  # ISO date
    contracts: int
    flagged_notional: float
    occ_symbol: str
    reason: str


class HedgeProposalRule(Rule):
    """See module docstring: `check()` is intentionally always a no-op.
    The real behavior is `compute_proposal()`, called directly by
    `FirewallMiddleware`, not through this rule's `RuleOutcome`."""

    def __init__(self, config: RuleConfig) -> None:
        super().__init__(config)
        self.cfg = _Params.model_validate(config.params)

    def check(
        self, tool_name: str, arguments: dict[str, Any], state: dict[str, Any]
    ) -> RuleOutcome:
        return RuleOutcome(False)


def format_occ_symbol(
    symbol: str, target_expiry_date: str, option_type: str, strike: float
) -> str:
    """Format an OCC-format option symbol from components,
    e.g. ('AAPL', '2026-09-18', 'P', 220.0) -> 'AAPL260918P00220000'.
    """
    dt = datetime.fromisoformat(target_expiry_date).date()
    yymmdd = f"{dt.year % 100:02d}{dt.month:02d}{dt.day:02d}"
    strike_int = int(round(strike * 1000))
    return f"{symbol.upper()}{yymmdd}{option_type.upper()}{strike_int:08d}"


def _mechanical_strike(current_price: float, otm_pct: float) -> float:
    return round(current_price * (1 - otm_pct), 2)


def _mechanical_expiry(now: float, expiry_min_days: int, expiry_max_days: int) -> str:
    target_days = (expiry_min_days + expiry_max_days) / 2
    target = datetime.fromtimestamp(now, tz=timezone.utc) + timedelta(days=target_days)
    return target.date().isoformat()


def _mechanical_contracts(flagged_notional: float, strike: float, coverage_pct: float) -> int:
    if strike <= 0:
        return 0
    shares_to_cover = (coverage_pct * flagged_notional) / strike
    return max(1, math.ceil(shares_to_cover / 100))


def compute_scheduled_overlay(
    current_positions: dict[str, float | int],
    prices: dict[str, float],
    *,
    otm_pct: float = 0.05,
    expiry_min_days: int = 14,
    expiry_max_days: int = 45,
    coverage_pct: float = 0.5,
    now: float | None = None,
) -> ScheduledOverlayProposal | None:
    """Compute a scheduled protective put on the basket's largest position.

    This is standing portfolio insurance, not a market-timing decision:
    a disclosed, scheduled options overlay applied regardless of market
    conditions, distinct from the reactive CVaR-triggered hedge.

    Sized through the SAME premium-cap and delta-corridor rules already built
    for the reactive hedge, with no new risk logic.
    """
    if not current_positions or not prices:
        return None

    position_values = {
        sym: float(qty) * prices.get(sym, 0.0)
        for sym, qty in current_positions.items()
        if float(qty) > 0 and prices.get(sym, 0.0) > 0
    }
    if not position_values:
        return None

    largest_sym, largest_notional = max(position_values.items(), key=lambda kv: kv[1])
    if largest_notional <= 0:
        return None

    current_price = prices[largest_sym]
    strike = _mechanical_strike(current_price, otm_pct)
    ts = now if now is not None else time.time()
    target_expiry = _mechanical_expiry(ts, expiry_min_days, expiry_max_days)
    contracts = _mechanical_contracts(largest_notional, strike, coverage_pct)
    occ_symbol = format_occ_symbol(largest_sym, target_expiry, "P", strike)

    reason = (
        "SCHEDULED OPTIONS OVERLAY -- a disclosed, scheduled options overlay applied "
        "regardless of market conditions, distinct from the reactive CVaR-triggered hedge. "
        "Standing portfolio insurance, not a market-timing decision. Proposed structure: "
        f"BUY {contracts} PUT contract(s) on {largest_sym} ({occ_symbol}), strike ${strike:,.2f} "
        f"({otm_pct:.0%} out-of-the-money from current price ${current_price:,.2f}), target "
        f"expiry {target_expiry} ({expiry_min_days}-{expiry_max_days} days out), covering "
        f"{coverage_pct:.0%} of the largest position's ${largest_notional:,.2f} notional. "
        "Sized through the same premium-cap and delta-corridor rules built for the reactive hedge."
    )

    return ScheduledOverlayProposal(
        symbol=largest_sym,
        current_price=current_price,
        strike=strike,
        target_expiry=target_expiry,
        contracts=contracts,
        flagged_notional=largest_notional,
        occ_symbol=occ_symbol,
        reason=reason,
    )


def _net_qty_by_symbol(order_history: OrderHistory) -> dict[str, float]:
    """Net signed quantity (buys minus sells) per symbol across recorded,
    still-live order events (excluding cancelled/blocked/rejected
    outcomes). Deliberately ignores `event.price` -- a market order never
    carries one, and quantity alone is enough to net a position. See
    module docstring's "Position contributing most" section."""
    exposure: dict[str, float] = {}
    for event in order_history:
        if event.outcome in ("cancelled", "blocked", "rejected"):
            continue
        signed = event.qty * (-1.0 if event.side.lower() == "sell" else 1.0)
        exposure[event.symbol] = exposure.get(event.symbol, 0.0) + signed
    return exposure


def _net_qty_for_symbol(
    order_history: OrderHistory, symbol: str
) -> tuple[float, bool]:
    """Net signed quantity for one symbol, and whether any live (non-
    cancelled/blocked/rejected) event for it was found at all -- a caller
    needs to distinguish "found, net zero" (position closed) from "never
    recorded" (nothing to say either way)."""
    net = 0.0
    found = False
    for event in order_history:
        if event.outcome in ("cancelled", "blocked", "rejected"):
            continue
        if event.symbol != symbol:
            continue
        found = True
        net += event.qty * (-1.0 if event.side.lower() == "sell" else 1.0)
    return net, found


def _largest_open_position_from_history(
    order_history: OrderHistory,
) -> tuple[str, float] | None:
    """See module docstring's "Position contributing most" section for the
    full reasoning. Approximation, not a realized-P&L attribution.

    Returns (symbol, net_qty) for the symbol with the largest absolute net
    quantity -- NOT a notional figure. Callers that need a dollar amount
    must fetch a current price for that symbol themselves and multiply;
    this function never requires (or uses) a historical order-time price."""
    exposure = _net_qty_by_symbol(order_history)

    if not exposure:
        return None
    symbol, net = max(exposure.items(), key=lambda kv: abs(kv[1]))
    if net == 0:
        return None
    return symbol, abs(net)


def _build_proposal(
    *,
    trigger: str,
    symbol: str,
    current_price: float,
    flagged_notional: float,
    hedge_cfg: _Params,
    now: float,
    detail: str,
) -> HedgeProposal:
    strike = _mechanical_strike(current_price, hedge_cfg.otm_pct)
    target_expiry = _mechanical_expiry(now, hedge_cfg.expiry_min_days, hedge_cfg.expiry_max_days)
    contracts = _mechanical_contracts(flagged_notional, strike, hedge_cfg.coverage_pct)
    reason = (
        "DEFENSIVE HEDGE PROPOSAL -- not a market view, not a forecast: a mechanical "
        f"response to an already-measured risk number. Trigger: {trigger} ({detail}). "
        f"Proposed structure: BUY {contracts} PUT contract(s) on {symbol}, strike "
        f"${strike:,.2f} ({hedge_cfg.otm_pct:.0%} out-of-the-money from current price "
        f"${current_price:,.2f}), target expiry {target_expiry} "
        f"({hedge_cfg.expiry_min_days}-{hedge_cfg.expiry_max_days} days out -- the "
        "nearest real listed expiry to this date must be selected by a human; no "
        f"options-chain lookup is performed here), covering {hedge_cfg.coverage_pct:.0%} "
        f"of the flagged ${flagged_notional:,.2f} notional. PROPOSAL ONLY: nothing has "
        "been submitted or placed. This firewall has no approval-token preview -> "
        "token -> submit flow to route this through yet, and will not submit any "
        "order outside that flow once it exists -- see README."
    )
    return HedgeProposal(
        trigger=trigger,
        symbol=symbol,
        current_price=current_price,
        strike=strike,
        target_expiry=target_expiry,
        contracts=contracts,
        flagged_notional=flagged_notional,
        reason=reason,
    )


def _check_cvar_trigger(
    arguments: dict[str, Any],
    state: dict[str, Any],
    hedge_cfg: _Params,
    cvar_gate_rule: CVaRGateRule,
) -> HedgeProposal | None:
    cfg = cvar_gate_rule.cfg
    symbol = arguments.get(cfg.symbol_field)
    if not symbol:
        return None

    notional = extract_notional(
        arguments, cfg.notional_field, cfg.qty_field, cfg.price_field
    )
    if notional is None:
        return None

    equity = state.get(cfg.account_equity_state_key)
    if not isinstance(equity, (int, float)) or isinstance(equity, bool):
        return None

    result = cvar_gate_rule._bars_fetcher(symbol, cfg.cvar_lookback_days)
    if not result.ok or len(result.closes) < 2:
        return None

    returns = _log_returns(result.closes)
    pnl_series = [notional * r for r in returns]
    cvar = compute_cvar(pnl_series, cfg.cvar_alpha)
    if cvar is None:
        return None

    max_loss_usd = equity * cfg.cvar_max_loss_pct_of_equity
    if max_loss_usd <= 0:
        return None

    if abs(cvar) < hedge_cfg.cvar_trigger_pct_of_max_loss * max_loss_usd:
        return None

    current_price = result.closes[-1]
    detail = (
        f"cvar_gate's CVaR estimate ${abs(cvar):,.2f} reached "
        f"{abs(cvar) / max_loss_usd:.0%} of its own max loss ${max_loss_usd:,.2f} "
        f"({cfg.cvar_max_loss_pct_of_equity:.1%} of ${equity:,.2f} equity) for "
        f"{symbol} (hedge trigger: {hedge_cfg.cvar_trigger_pct_of_max_loss:.0%})"
    )
    return _build_proposal(
        trigger="cvar_gate",
        symbol=symbol,
        current_price=current_price,
        flagged_notional=notional,
        hedge_cfg=hedge_cfg,
        now=state.get("now", time.time()),
        detail=detail,
    )


def _check_drawdown_trigger(
    state: dict[str, Any],
    hedge_cfg: _Params,
    drawdown_killswitch_rule: DrawdownKillswitchRule,
    bars_fetcher,
) -> HedgeProposal | None:
    cfg = drawdown_killswitch_rule.cfg
    pnl = state.get(cfg.session_pnl_state_key)
    if not isinstance(pnl, (int, float)) or isinstance(pnl, bool):
        return None

    threshold = cfg.session_pnl_threshold_usd
    if threshold >= 0:
        return None  # a non-negative "loss" threshold is not a usable trigger

    trigger_level = threshold * hedge_cfg.drawdown_trigger_pct_of_threshold
    if pnl > trigger_level:
        return None

    order_history = state.get("order_history")
    if order_history is None:
        return None
    picked = _largest_open_position_from_history(order_history)
    if picked is None:
        return None
    symbol, net_qty = picked

    result = bars_fetcher(symbol, 5)
    if not result.ok or not result.closes:
        return None
    current_price = result.closes[-1]
    # Notional is net quantity (largest by qty, price-independent -- see
    # _largest_open_position_from_history) times a CURRENT price fetched
    # here at evaluation time, never a historical order-time price -- this
    # is what makes the trigger visible to market orders (no limit_price).
    flagged_notional = abs(net_qty) * current_price

    detail = (
        f"drawdown_killswitch's session PnL ${pnl:,.2f} reached "
        f"{pnl / threshold:.0%} of its ${threshold:,.2f} threshold (hedge trigger: "
        f"{hedge_cfg.drawdown_trigger_pct_of_threshold:.0%}); {symbol} is this "
        f"session's largest recorded live order exposure (${flagged_notional:,.2f}, "
        "approximated from order_history -- not a per-symbol realized-loss "
        "attribution)"
    )
    return _build_proposal(
        trigger="drawdown_killswitch",
        symbol=symbol,
        current_price=current_price,
        flagged_notional=flagged_notional,
        hedge_cfg=hedge_cfg,
        now=state.get("now", time.time()),
        detail=detail,
    )


def compute_proposal(
    tool_name: str,
    arguments: dict[str, Any],
    state: dict[str, Any],
    *,
    hedge_cfg: _Params,
    cvar_gate_rule: CVaRGateRule | None,
    drawdown_killswitch_rule: DrawdownKillswitchRule | None,
    bars_fetcher=None,
) -> HedgeProposal | None:
    """Detection + audit only -- see module docstring. Never submits
    anything. Checks the cvar_gate trigger first, then drawdown_killswitch,
    returning the first proposal found (never both -- one defined
    structure per call, not a pillar). `bars_fetcher` defaults to
    `firewall.market_data.fetch_daily_bars`, resolved at call time (not
    bound as a default argument) so tests can monkeypatch the module-level
    name; only the drawdown_killswitch path uses it -- the cvar_gate path
    reuses `cvar_gate_rule._bars_fetcher` directly instead (see module
    docstring)."""
    if not matches_any(tool_name, _ORDER_RELATED_TOOLS):
        return None

    if cvar_gate_rule is not None:
        proposal = _check_cvar_trigger(arguments, state, hedge_cfg, cvar_gate_rule)
        if proposal is not None:
            return proposal

    if drawdown_killswitch_rule is not None:
        proposal = _check_drawdown_trigger(
            state, hedge_cfg, drawdown_killswitch_rule, bars_fetcher or fetch_daily_bars
        )
        if proposal is not None:
            return proposal

    return None


def format_hedge_release_note(symbol: str) -> str:
    """Format the informational note when a hedge trigger condition normalizes."""
    sym = symbol if symbol.startswith("$") else f"${symbol}"
    return f"hedge on {sym}: trigger condition resolved, review for release"


def is_drawdown_trigger_normalized(
    state: dict[str, Any],
    hedge_cfg: _Params,
    drawdown_killswitch_rule: DrawdownKillswitchRule,
    symbol: str | None = None,
) -> bool:
    """Check if the drawdown_killswitch hedge trigger condition has normalized.

    The trigger condition was: session_pnl_usd <= threshold * drawdown_trigger_pct_of_threshold.
    Normalized when:
      1. session_pnl_usd > threshold * drawdown_trigger_pct_of_threshold, OR
      2. The position for symbol in order_history has been closed (net exposure == 0).
    """
    if symbol is not None:
        order_history = state.get("order_history")
        if order_history is not None:
            net_qty, found = _net_qty_for_symbol(order_history, symbol)
            if found and net_qty == 0.0:
                return True

    cfg = drawdown_killswitch_rule.cfg
    pnl = state.get(cfg.session_pnl_state_key)
    if not isinstance(pnl, (int, float)) or isinstance(pnl, bool):
        return False

    threshold = cfg.session_pnl_threshold_usd
    if threshold >= 0:
        return False

    trigger_level = threshold * hedge_cfg.drawdown_trigger_pct_of_threshold
    return pnl > trigger_level


def is_cvar_trigger_normalized(
    symbol: str,
    state: dict[str, Any],
    hedge_cfg: _Params,
    cvar_gate_rule: CVaRGateRule,
    notional: float | None = None,
) -> bool:
    """Check if the cvar_gate hedge trigger condition has normalized for symbol.

    The trigger condition was: abs(cvar) >= hedge_cfg.cvar_trigger_pct_of_max_loss * max_loss_usd.
    Normalized when:
      1. Position for symbol has no open notional (closed), OR
      2. abs(cvar) < hedge_cfg.cvar_trigger_pct_of_max_loss * max_loss_usd.
    """
    cfg = cvar_gate_rule.cfg
    equity = state.get(cfg.account_equity_state_key)
    if not isinstance(equity, (int, float)) or isinstance(equity, bool):
        return False

    max_loss_usd = equity * cfg.cvar_max_loss_pct_of_equity
    if max_loss_usd <= 0:
        return False

    # Fetched once, up front: needed both to derive a notional fallback
    # (net qty * CURRENT price, never a historical order-time price) and,
    # below, for the CVaR returns calculation itself.
    result = cvar_gate_rule._bars_fetcher(symbol, cfg.cvar_lookback_days)
    if not result.ok or len(result.closes) < 2:
        return False
    current_price = result.closes[-1]

    if notional is None:
        order_history = state.get("order_history")
        if order_history is not None:
            net_qty, found = _net_qty_for_symbol(order_history, symbol)
            if found and net_qty == 0.0:
                return True
            if found and net_qty != 0.0:
                notional = abs(net_qty) * current_price

    if notional is None or notional <= 0:
        return False

    returns = _log_returns(result.closes)
    pnl_series = [notional * r for r in returns]
    cvar = compute_cvar(pnl_series, cfg.cvar_alpha)
    if cvar is None:
        return False

    return abs(cvar) < hedge_cfg.cvar_trigger_pct_of_max_loss * max_loss_usd


def is_trigger_normalized(
    trigger: str,
    symbol: str,
    state: dict[str, Any],
    *,
    hedge_cfg: _Params,
    cvar_gate_rule: CVaRGateRule | None,
    drawdown_killswitch_rule: DrawdownKillswitchRule | None,
    notional: float | None = None,
) -> bool:
    """Check if the given trigger condition for an open hedge on symbol has normalized."""
    if trigger == "drawdown_killswitch":
        if drawdown_killswitch_rule is None:
            return False
        return is_drawdown_trigger_normalized(
            state, hedge_cfg, drawdown_killswitch_rule, symbol=symbol
        )
    elif trigger == "cvar_gate":
        if cvar_gate_rule is None:
            return False
        return is_cvar_trigger_normalized(
            symbol, state, hedge_cfg, cvar_gate_rule, notional=notional
        )
    return False
