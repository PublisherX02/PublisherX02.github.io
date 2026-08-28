"""hedge_cost_cap — hard block a single-leg option BUY order whose total
premium cost, at the contract's live ask, exceeds a configured fraction of
account equity.

TOTAL HEDGE COST = live `ask` price x 100 (one option contract represents
100 underlying shares) x `qty` (contract count) -- read from the SAME
options-snapshot fetch `option_spread_guard`/`net_delta_floor` already use
(`firewall.market_data.fetch_option_latest_quote`), not a second one: one
HTTP call and one 5-second cache entry per symbol serves all three rules
within the same `evaluate()` pass.

WHY `ask`, NOT THE ORDER'S OWN `limit_price` -- an explicit decision, not
an oversight: a MARKET order (no `limit_price` field at all, per the
verified schema `notional_cap.py`/`option_spread_guard.py` already
document) still needs a cost estimate, and a limit order's own
`limit_price` could be set arbitrarily low to dodge this check while a
real fill still clears near the market. The live `ask` is the
conservative, market-derived estimate of what a buy order can actually
cost, for either order type, and it's the same figure
`option_spread_guard` already fetches for this exact symbol on this exact
call.

WHAT THIS PROTECTS, AND HOW IT AGREES WITH `notional_cap` -- an explicit,
documented decision, resolving the question of what `extract_notional()`
computes for options once its string-coercion fix (conformance-audit
finding A4) landed: for `place_option_order`, `extract_notional()` with
`option_contract_multiplier=100.0` computes `qty * limit_price * 100` --
TOTAL PREMIUM (the dollar cost of the option leg), never underlying
notional (`strike * 100 * qty`, which `extract_notional` does not and
cannot compute -- it has no strike input) and never a naive unmultiplied
`qty * price` (that undercounts real premium by 100x -- see
`notional_cap.py`'s own module docstring for the verified live-schema
evidence). `notional_cap` protects that exact quantity -- premium
dollars -- against a flat USD cap. This rule protects the SAME quantity,
premium dollars, against an equity-relative cap instead of a flat one.
The two rules deliberately do not diverge on what they are capping, only
on the price source (`notional_cap` reads the order's own `limit_price`;
this rule reads the live `ask`, for the reasons above) and the threshold
shape (flat USD vs. percent of equity).

REUSES `cvar_gate`'s OWN account-equity state key
(`account_equity_state_key`, default "account_equity"), not a second,
differently-named input -- both rules cap something against the same
underlying number, and `hedge_proposal.py` already established the
precedent of reusing `cvar_gate`'s own inputs directly rather than
re-deriving them. Missing or non-numeric equity FAILS CLOSED, exactly
like `cvar_gate` -- and, as of this writing, nothing in `src/` populates
`state["account_equity"]` from a real upstream response (the same
disclosed gap `cvar_gate` itself has, documented in its own module
docstring, `FirewallMiddleware`'s docstring, and README's "What this does
not do"). This rule is therefore correct by construction, unit-tested
directly, but structurally dormant in production right now -- the same
shape of gap, not a new one.

BUY SIDE ONLY: selling/writing an option RECEIVES premium, it doesn't
spend it, so a "cost cap" is inapplicable to that side by definition (not
merely unenforced) -- `side == "sell"` is a plain no-op here, not a
fail-closed case. The real risk of an uncovered short (unbounded loss on
assignment) is out of scope for a cost cap and is instead hard-blocked
outright by the separate `option_sell_guard` rule -- see that module's
own docstring.

REGARDLESS OF CALL ORIGIN: this is a proper `Rule` registered in
`firewall.rules.RULE_TYPES` and evaluated by `PolicyEngine.evaluate()` for
EVERY `place_option_order` call, not validation logic embedded inside
`hedge_proposal.py`'s own proposal-generation step (which computes a
proposal's strike/expiry/contract count but performs no validation of its
own -- see that module's docstring). Despite the name (this rule exists
to keep a defensive hedge's cost proportionate to the account it is
meant to protect), nothing about its `check()` is conditioned on whether
the call came from a hedge proposal -- an agent buying options for any
other reason is capped identically. Building this as hedge-trigger-only
validation would leave every non-hedge-triggered `place_option_order`
call, and every option-buying call made through a different client
entirely, with no cost-proportionality check at all -- the exact class of
bug conformance-audit finding A4's Fix 1 (string-coercion) was built to
close, reopened in a new corner of the system. Confirmed explicitly
before this rule was written, not assumed.

SINGLE-LEG ONLY, same scope boundary and same reasoning as
`option_spread_guard`/`net_delta_floor`/`iv_hv_ratio`: self-scopes via a
valid OCC-format parent `symbol`. A multi-leg order's true net cost
depends on every leg's own price net of the others (a spread's net
debit/credit, not a simple sum of asks) -- this rule does not attempt to
combine them; disclosed, deliberate gap, not silently extended.
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel

from firewall.market_data import OptionQuoteResult, fetch_option_latest_quote
from firewall.rules._util import _as_number, matches_any, parse_occ_underlying
from firewall.rules.base import Rule, RuleConfig, RuleOutcome

QuoteFetcher = Callable[[str], OptionQuoteResult]

DEFAULT_CONTRACT_MULTIPLIER = 100.0


class _Params(BaseModel):
    max_pct_of_equity: float
    contract_multiplier: float = DEFAULT_CONTRACT_MULTIPLIER
    tool_match: list[str] = ["order"]
    symbol_field: str = "symbol"
    side_field: str = "side"
    qty_field: str = "qty"
    account_equity_state_key: str = "account_equity"
    market_data_timeout_seconds: float = 5.0
    quote_cache_ttl_seconds: float = 5.0
    feed: str = "indicative"


class HedgeCostCapRule(Rule):
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
            # No parent symbol (multi-leg) or not option-shaped. Deliberately
            # out of scope; see module docstring.
            return RuleOutcome(False)

        side = str(arguments.get(self.cfg.side_field, "buy")).strip().lower()
        if side == "sell":
            # Receives premium, doesn't spend it -- inapplicable by
            # definition, not merely unenforced. See module docstring.
            return RuleOutcome(False)

        qty = _as_number(arguments.get(self.cfg.qty_field))
        if qty is None:
            return RuleOutcome(
                True,
                "cannot compute hedge cost — qty is missing or unparseable, "
                "failing closed",
            )

        result = self._quote_fetcher(symbol)
        if not result.ok or result.quote is None:
            return RuleOutcome(
                True,
                "insufficient market data to assess risk — failing closed: "
                f"could not obtain a usable quote for {symbol} ({result.reason})",
            )

        equity = state.get(self.cfg.account_equity_state_key)
        if not isinstance(equity, (int, float)) or isinstance(equity, bool):
            return RuleOutcome(
                True,
                "insufficient account state to assess risk — failing closed: "
                f"state[{self.cfg.account_equity_state_key!r}] is missing or "
                "not numeric",
            )

        hedge_cost = result.quote.ask * self.cfg.contract_multiplier * qty
        max_cost = equity * self.cfg.max_pct_of_equity

        if hedge_cost > max_cost:
            return RuleOutcome(
                True,
                f"proposed hedge costs ${hedge_cost:,.2f}, {hedge_cost / equity:.1%} "
                f"of NAV, exceeds maximum {self.cfg.max_pct_of_equity:.1%} — reduce "
                "quantity or select a cheaper strike "
                f"({symbol}: ask ${result.quote.ask:,.2f} x {self.cfg.contract_multiplier:.0f} "
                f"x {qty:,.0f} contract(s); equity ${equity:,.2f})",
            )
        return RuleOutcome(False)
