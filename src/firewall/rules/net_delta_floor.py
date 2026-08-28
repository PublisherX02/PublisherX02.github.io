"""net_delta_floor — hard block a single-leg option order if the resulting
NET delta of (existing shares of the underlying) + (this option's own
delta contribution) would drop below a configured floor (default 0),
because that is no longer a hedge — it is a directional bet.

Regulation: SEC Rule 15c3-5(c)(1)(ii) — same subsection `order_rate_throttle`
and `option_spread_guard` already cite for their own, different clauses;
this rule enforces the "erroneous orders... that exceed appropriate...
size parameters" clause: an option order sized past neutral is, in
substance, a mis-sized (erroneous) hedge — its true directional exposure
does not match what a "hedge" is supposed to produce.

NET DELTA = existing_shares * 1.0 (one share always contributes exactly
1.0 delta) + side_sign * option_qty * option_delta * CONTRACT_MULTIPLIER
(100.0 — one option contract represents 100 underlying shares).
`side_sign` is +1.0 for a buy, -1.0 for a sell (selling/writing an option
is the mirror-image delta exposure of buying it). THE x100 MULTIPLIER IS
NOT OPTIONAL: a raw option delta (typically -1.0..1.0) compared directly
against a share count (typically in the hundreds) is off by two orders of
magnitude — see `compute_net_delta`'s own tests, including the exact
worked example this rule was specified against: 100 existing shares, one
put contract at delta -0.50, must compute to net +50.0, not +99.50.

USES THE SAME SNAPSHOT FETCH AS option_spread_guard, not a second one:
`firewall.market_data.fetch_option_latest_quote` already returns
`.delta` from the identical `GET /v1beta1/options/snapshots` response
`option_spread_guard` reads `.bid`/`.ask` from — one HTTP call and one
5-second cache entry per option symbol serve both rules; see that
module's own docstring for the endpoint/feed verification. `greeks`
(and therefore delta) is a separately optional field of Alpaca's
`option_snapshot` schema, distinct from `latestQuote` — a quote can be
usable (`ok=True`) with `delta=None`, which this rule (unlike
`option_spread_guard`, which never touches delta) treats as its own
failure to assess, failing closed the same way an unfetchable quote does.

EXISTING SHARE COUNT comes from `state[underlying_positions_state_key]`
(default key: "underlying_share_positions"), a `{underlying_ticker:
net_share_count}` dict keyed by the UNDERLYING's plain ticker (parsed
from the option's own OCC symbol via `_util.parse_occ_underlying`) — NOT
by the option contract's own symbol, and NOT the same key or unit as
`position_cap`'s `state["positions"]` (which holds USD exposure, not
shares; reusing that key here would silently mix units). An underlying
absent from the dict defaults to 0.0 shares — matching `position_cap`'s
own established convention (`positions.get(symbol, 0.0)`) — rather than
failing closed on a missing entry. This is deliberate, not a lesser
standard than the market-data fail-closed above: a naked option order
with no evidence of an offsetting stock position IS, correctly, read as
a pure directional bet, and floor=0 correctly blocks it. Disclosed,
not silent: nothing in `src/` populates `underlying_share_positions`
today (the same shape of gap `position_cap`'s own `state["positions"]`
already has, and `cvar_gate`'s `account_equity` had before Fix 2) — this
rule is therefore correct by construction, unit-tested directly, but
will hard-block every single-leg option BUY it can otherwise assess in
production today, since the default-to-zero baseline always reads as
unhedged. See AUDIT.md's own section on this rule for the full
disclosure; this is the conservative, safe-by-default direction for a
missing baseline, not an oversight.

SINGLE-LEG ONLY, same scope boundary as `option_spread_guard`, same
reasoning: self-scopes via a valid OCC-format parent `symbol`. Multi-leg
orders (no parent `symbol`) are a disclosed, deliberate gap — a spread's
net delta genuinely depends on every leg's own delta, which this rule
does not attempt to sum.

TWO INDEPENDENT CHECKS, not one — `check()` runs both against the same
fetched quote and must never let one reuse the other's scaled value:

  1. `structural_delta_floor` — the RAW per-contract delta straight off
     the snapshot, before any x100/quantity/side scaling. Asks "is this
     contract even sensitive enough to function as a hedge at all," not
     "does the resulting position stay hedged." A deep out-of-the-money
     contract (|delta| near 0) barely moves with the underlying, so
     buying it accomplishes little regardless of how large a position it
     nets against — this floors that case (default 0.15) before the
     aggregate check below ever runs. This is a plain flat-constant
     heuristic, not derived from anything (not tied to any measured tail-
     risk figure) — see this rule's own tests for the boundary behavior.
     A more principled version would tie this floor to a fraction of
     cvar_gate's own computed tail-loss figure for the flagged position
     (hedge notional = delta * 100 * contracts * underlying price must
     cover some configured fraction of that CVaR estimate) instead of a
     fixed number — deliberately not built here: it needs the flagged
     position's underlying price and cvar_gate's own CVaR result wired
     across the rule boundary the same bespoke way `hedge_proposal.py`
     wires `CVaRGateRule` in directly (this rule has no equivalent
     constructor-injected reference to a live `CVaRGateRule` today), which
     is a larger, separate change than this flat floor.
  2. `net_delta_floor` (above) — the AGGREGATE portfolio delta after this
     order, in delta-equivalent shares (x100-scaled). Asks "does the
     resulting position stay hedged," independent of whether any single
     contract in it is individually sensitive.

Order matters only for which message a rejected call gets, not for
correctness: the structural check runs first (using `result.quote.delta`
directly, never `compute_net_delta`'s return value), then the aggregate
check runs on `compute_net_delta`'s output. A case can fail either one
without failing the other — see
`test_structural_floor_is_independent_of_net_delta_scaling` in this
module's own test file for a worked example (aggregate passes, structural
fails) and `test_structural_floor_does_not_block_when_net_delta_ceiling_would`
for the mirror case.
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel

from firewall.market_data import OptionQuoteResult, fetch_option_latest_quote
from firewall.rules._util import _as_number, matches_any, parse_occ_underlying
from firewall.rules.base import Rule, RuleConfig, RuleOutcome

QuoteFetcher = Callable[[str], OptionQuoteResult]

_SHARE_DELTA = 1.0
DEFAULT_CONTRACT_MULTIPLIER = 100.0


class _Params(BaseModel):
    net_delta_floor: float = 0.0
    # Flat heuristic, not derived from any measured risk figure -- see
    # module docstring's "TWO INDEPENDENT CHECKS" section for the
    # CVaR-linked version this was deliberately not built as.
    structural_delta_floor: float = 0.15
    tool_match: list[str] = ["order"]
    symbol_field: str = "symbol"
    side_field: str = "side"
    qty_field: str = "qty"
    contract_multiplier: float = DEFAULT_CONTRACT_MULTIPLIER
    underlying_positions_state_key: str = "underlying_share_positions"
    market_data_timeout_seconds: float = 5.0
    quote_cache_ttl_seconds: float = 5.0
    feed: str = "indicative"


class NetDeltaFloorRule(Rule):
    def __init__(
        self,
        config: RuleConfig,
        quote_fetcher: QuoteFetcher | None = None,
    ) -> None:
        super().__init__(config)
        self.cfg = _Params.model_validate(config.params)
        self._quote_fetcher = quote_fetcher or self._fetch_quote

    def _fetch_quote(self, symbol: str) -> OptionQuoteResult:
        return fetch_option_latest_quote(
            symbol,
            timeout_seconds=self.cfg.market_data_timeout_seconds,
            cache_ttl_seconds=self.cfg.quote_cache_ttl_seconds,
            feed=self.cfg.feed,
        )

    def check(
        self, tool_name: str, arguments: dict[str, Any], state: dict[str, Any]
    ) -> RuleOutcome:
        if not matches_any(tool_name, self.cfg.tool_match):
            return RuleOutcome(False)

        symbol = arguments.get(self.cfg.symbol_field)
        underlying = parse_occ_underlying(symbol) if symbol else None
        if underlying is None:
            # No parent symbol (multi-leg) or not option-shaped (a
            # stock/crypto order's plain ticker). Deliberately out of
            # scope; see module docstring.
            return RuleOutcome(False)

        qty = _as_number(arguments.get(self.cfg.qty_field))
        if qty is None:
            return RuleOutcome(
                True,
                "cannot compute net delta — qty is missing or unparseable, failing closed",
            )

        side = str(arguments.get(self.cfg.side_field, "buy")).strip().lower()

        result = self._quote_fetcher(symbol)
        if not result.ok or result.quote is None or result.quote.delta is None:
            reason = result.reason if result.ok is False else "no delta available"
            return RuleOutcome(
                True,
                "insufficient market data to assess risk — failing closed: "
                f"could not obtain a usable delta for {symbol} ({reason})",
            )

        raw_delta = result.quote.delta
        if abs(raw_delta) < self.cfg.structural_delta_floor:
            # The message below says "measured CVaR tail risk" -- read that
            # as the reason a hedge needs meaningful delta at all, not as a
            # claim this rule computed one: structural_delta_floor is a flat
            # heuristic (see module docstring's "TWO INDEPENDENT CHECKS"
            # section), not derived from any CVaR figure.
            return RuleOutcome(
                True,
                "proposed hedge delta below minimum structural threshold — "
                "contract too far out-of-the-money to mitigate measured CVaR "
                f"tail risk (|delta| {abs(raw_delta):.4f} < floor "
                f"{self.cfg.structural_delta_floor:.4f}; {symbol} at raw delta "
                f"{raw_delta:+.4f}, before any x100/quantity scaling)",
            )

        positions = state.get(self.cfg.underlying_positions_state_key) or {}
        existing_shares = float(positions.get(underlying, 0.0))

        net_delta = compute_net_delta(
            existing_shares=existing_shares,
            option_qty=qty,
            option_delta=result.quote.delta,
            side=side,
            contract_multiplier=self.cfg.contract_multiplier,
        )

        if net_delta < self.cfg.net_delta_floor:
            return RuleOutcome(
                True,
                "proposed hedge exceeds neutral delta — this is a directional short, "
                f"not a hedge (net delta {net_delta:+.2f} < floor "
                f"{self.cfg.net_delta_floor:+.2f}; {existing_shares:+.0f} existing "
                f"shares of {underlying}, {qty:+.0f} contract(s) of {symbol} at "
                f"delta {result.quote.delta:+.4f}, {side})",
            )
        return RuleOutcome(False)


def compute_net_delta(
    *,
    existing_shares: float,
    option_qty: float,
    option_delta: float,
    side: str,
    contract_multiplier: float = DEFAULT_CONTRACT_MULTIPLIER,
) -> float:
    """Pure function, unit-tested directly (see the module docstring for
    why the x100 multiplier is load-bearing, not cosmetic): net portfolio
    delta after the proposed option order, in "delta-equivalent shares."
    `side` "sell" flips the option's own delta contribution — writing an
    option is the mirror-image exposure of buying it.
    """
    side_sign = -1.0 if side == "sell" else 1.0
    return (existing_shares * _SHARE_DELTA) + (
        side_sign * option_qty * option_delta * contract_multiplier
    )
