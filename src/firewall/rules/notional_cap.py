"""notional_cap — max USD notional per single order.

Regulation: SEC Rule 15c3-5(c)(1)(i) (pre-trade controls must cap the value
of a single order).

DYNAMIC CAP, WITH A STATIC FALLBACK -- DELIBERATE, NOT A DEFAULT-MISSING
GAP: `max_usd` is a flat dollar figure picked once and never revisited as
an account grows or shrinks -- the wrong shape for a real deployment (see
this project's own README "What this does not do" on stale flat-dollar
caps). `max_pct_of_equity`, when set, computes the effective cap as
`state[account_equity_state_key] * max_pct_of_equity` instead -- reusing
`state["account_equity"]` (the SAME key cvar_gate/pct_of_adv/hedge_cost_cap
already read, populated once per call by
`firewall.proxy.FirewallMiddleware._populate_account_state`), not a second
equity input.

Deliberately NOT fail-closed on missing/unavailable equity, unlike
cvar_gate/hedge_cost_cap: this rule sits FIRST in policies/default.yaml's
evaluation order (`PolicyEngine.evaluate` short-circuits on the first
triggered hard rule), so a fail-closed posture here would mean any
transient account-data outage reports as "BLOCKED by notional-cap-
single-order" for every single order, permanently masking whatever
rule further down the list would otherwise have reported its own, more
specific reason -- exactly the diagnosability this project's audit trail
exists to preserve. Instead: `max_usd` stays a REQUIRED field and doubles
as the fallback ceiling whenever `max_pct_of_equity` is unset OR equity is
unavailable -- never a silent skip, always a real cap, just a static one
in that (expected to be rare, once account_equity wiring is healthy)
degraded case. `policies/preset_*.yaml`'s strictness-gradient sweep never
sets `max_pct_of_equity` at all, so every preset's behavior is completely
unchanged by this feature -- see this rule's own tests for both paths.

REFERENCE-PRICE FALLBACK FOR PLAIN STOCK MARKET ORDERS: a plain market order
(`type: "market"`, `qty` only) legitimately carries no price at all -- and,
verified live against the real Alpaca paper API (2026-08-29), CANNOT carry
one: `type: "market"` + `limit_price` is rejected outright (HTTP 422, code
40010001, "market orders require no stop or limit price"). So this rule
cannot wait for the order to disclose its own notional; for
`stock_tool_match` orders (`place_stock_order` only -- not options, not
crypto, not replace_order_by_id, see each field's own comment below) with
no computable payload notional, it fetches a reference price itself via the
same shared `firewall.market_data.fetch_daily_bars` helper cvar_gate/
pct_of_adv/core_strategy already use (most recent close), rather than
skipping the order unassessed. Missing/bad market data for that fetch fails
CLOSED, matching cvar_gate/pct_of_adv's own convention for a
market-data-dependent hard rule.
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel

from firewall.market_data import BarsResult, fetch_daily_bars
from firewall.rules._util import extract_notional, matches_any
from firewall.rules.base import Rule, RuleConfig, RuleOutcome

BarsFetcher = Callable[[str, int], BarsResult]


class _Params(BaseModel):
    max_usd: float
    # See this module's own "DYNAMIC CAP, WITH A STATIC FALLBACK" docstring
    # section: None (the default) means "always use max_usd" -- every
    # preset_*.yaml is unaffected. When set, this takes precedence over
    # max_usd whenever state[account_equity_state_key] is a real number.
    max_pct_of_equity: float | None = None
    account_equity_state_key: str = "account_equity"
    tool_match: list[str] = ["order"]
    notional_field: str = "notional"
    qty_field: str = "qty"
    price_field: str = "limit_price"
    symbol_field: str = "symbol"
    # place_option_order's qty is a contract count, not a share count, and
    # limit_price is a per-share premium/net-debit (single-leg) or net
    # debit/credit (multi-leg) -- one option contract represents 100
    # shares. Verified against the live inputSchema (uvx --offline
    # alpaca-mcp-server, checked 2026-08-24): "qty" description reads
    # "Number of contracts... For multi-leg, this is the strategy
    # multiplier"; "limit_price" description reads "For multi-leg, this is
    # the net debit/credit". Without this multiplier, qty * limit_price
    # undercounts real premium by 100x. Single-leg option orders are
    # already fully hard-blocked by symbol_allowlist today (their `symbol`
    # is OCC-format, e.g. "AAPL250321C00150000", which never matches a
    # plain-ticker allowlist entry), so this multiplier's only live effect
    # is on multi-leg orders -- the one shape symbol_allowlist's parent-
    # symbol check can't see at all (see symbol_allowlist.py's own fix for
    # that), leaving this rule as the sole live defense against an
    # oversized multi-leg options structure until position_cap/cvar_gate/
    # pct_of_adv gain a per-leg notional model of their own (a disclosed,
    # not-yet-built gap -- see README's "What this does not do").
    option_tool_match: list[str] = ["place_option_order"]
    option_contract_multiplier: float = 100.0
    # Tools eligible for the reference-price fallback above: plain stock
    # market orders only. NOT place_option_order (fetch_daily_bars prices
    # equities/ETFs, not option contracts -- pricing an option needs a live
    # option quote, a different, not-yet-built mechanism, same disclosed
    # gap as option_contract_multiplier's own comment above) and NOT
    # place_crypto_order (untested against this fallback; this project's
    # basket/allowlist never trades crypto -- narrowing the blast radius of
    # an unverified path rather than assuming it works).
    stock_tool_match: list[str] = ["place_stock_order"]
    price_lookback_days: int = 5
    market_data_timeout_seconds: float = 5.0
    bars_cache_ttl_seconds: float = 1800.0
    # Tools where a None notional -- even after the stock-order reference-
    # price fallback above has had its chance -- means "incomplete data for
    # an order that was already sized," not "this order type just doesn't
    # carry a price." Deliberately NOT place_stock_order/place_option_order/
    # place_crypto_order: verified against alpaca-mcp-server's real
    # place_stock_order input schema (uvx alpaca-mcp-server, tool
    # inputSchema, checked 2026-08-18) -- `type` defaults to "market", and
    # limit_price is documented "Required for limit and stop_limit orders"
    # only. A plain market order (the common case -- "buy 10 shares of
    # AAPL") legitimately carries qty and no price at all; extract_notional
    # returning None for one is normal, not incomplete data, and a
    # malformed *placement* call missing a required limit_price for a
    # limit/stop_limit order is rejected by Alpaca itself at submission,
    # not silently executed -- there's no exploitable window there for this
    # rule to close.
    #
    # replace_order_by_id is different in kind, not degree: verified
    # against its real input schema, `limit_price` there is "Required if
    # original order's `type` field was `limit` or `stop_limit`" -- i.e.
    # Alpaca lets a caller omit it and leaves the *existing* resting price
    # untouched. This firewall has no visibility into that existing price
    # (it isn't in the call's arguments, and isn't tracked in
    # OrderHistory), so a qty-only replace is a call Alpaca will genuinely
    # accept and execute at an unknown price -- exactly the case this
    # module's docstring and AUDIT.md's A4 finding describe. Fail closed
    # here until a purpose-built amendment rule with real order-state
    # lookup exists (see policies/default.yaml's comment on this rule and
    # README's "What this does not do").
    sizing_tool_match: list[str] = ["replace_order_by_id"]


class NotionalCapRule(Rule):
    def __init__(self, config: RuleConfig, bars_fetcher: BarsFetcher | None = None) -> None:
        super().__init__(config)
        self.cfg = _Params.model_validate(config.params)
        self._bars_fetcher = bars_fetcher or self._fetch_bars

    def _fetch_bars(self, symbol: str, lookback_days: int) -> BarsResult:
        return fetch_daily_bars(
            symbol,
            lookback_days,
            timeout_seconds=self.cfg.market_data_timeout_seconds,
            cache_ttl_seconds=self.cfg.bars_cache_ttl_seconds,
        )

    def _reference_notional(self, arguments: dict[str, Any]) -> RuleOutcome | float | None:
        """Reference-price fallback for a stock market order with no
        payload-computable notional: fetch the most recent close and
        multiply by qty. Returns a float notional on success, a triggered
        (fail-closed) RuleOutcome on bad/missing market data, or None if
        this order doesn't even carry a usable symbol/qty to look up (falls
        through to the caller's own sizing_tool_match handling)."""
        symbol = arguments.get(self.cfg.symbol_field)
        qty_raw = (
            arguments.get(self.cfg.qty_field)
            if arguments.get(self.cfg.qty_field) is not None
            else arguments.get("quantity")
        )
        if not symbol or qty_raw is None:
            return None
        try:
            qty = abs(float(qty_raw))
        except (TypeError, ValueError):
            return None

        result = self._bars_fetcher(symbol, self.cfg.price_lookback_days)
        if not result.ok or not result.closes:
            reason = result.reason if not result.ok else "no closes returned"
            return RuleOutcome(
                True,
                f"insufficient market data to assess risk — failing closed: "
                f"could not fetch a reference price for {symbol}: {reason}",
            )
        return qty * result.closes[-1]

    def _effective_cap(self, state: dict[str, Any]) -> tuple[float, bool]:
        """The cap to enforce for this call, and whether it's the dynamic
        (equity-relative) one -- see this module's own "DYNAMIC CAP, WITH A
        STATIC FALLBACK" docstring section."""
        if self.cfg.max_pct_of_equity is not None:
            equity = state.get(self.cfg.account_equity_state_key)
            if isinstance(equity, (int, float)) and not isinstance(equity, bool):
                return equity * self.cfg.max_pct_of_equity, True
        return self.cfg.max_usd, False

    def check(
        self, tool_name: str, arguments: dict[str, Any], state: dict[str, Any]
    ) -> RuleOutcome:
        if not matches_any(tool_name, self.cfg.tool_match):
            return RuleOutcome(False)

        multiplier = (
            self.cfg.option_contract_multiplier
            if matches_any(tool_name, self.cfg.option_tool_match)
            else 1.0
        )
        notional = extract_notional(
            arguments,
            self.cfg.notional_field,
            self.cfg.qty_field,
            self.cfg.price_field,
            multiplier,
        )
        if notional is None and matches_any(tool_name, self.cfg.stock_tool_match):
            fallback = self._reference_notional(arguments)
            if isinstance(fallback, RuleOutcome):
                return fallback
            notional = fallback

        if notional is None:
            if matches_any(tool_name, self.cfg.sizing_tool_match):
                return RuleOutcome(
                    True,
                    "cannot compute notional — incomplete order data, failing closed",
                )
            return RuleOutcome(False)

        cap, is_dynamic = self._effective_cap(state)
        if notional > cap:
            cap_desc = (
                f"${cap:,.2f} ({self.cfg.max_pct_of_equity:.1%} of equity)"
                if is_dynamic
                else f"${cap:,.2f}"
            )
            return RuleOutcome(
                True,
                f"order notional ${notional:,.2f} exceeds per-order cap {cap_desc}",
            )
        return RuleOutcome(False)
