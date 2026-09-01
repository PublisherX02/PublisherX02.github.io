"""position_cap — max USD exposure per symbol, using current positions.

Regulation: SEC Rule 15c3-5(c)(1)(i) (pre-trade controls must cap maximum
position size per instrument).

REFERENCE-PRICE FALLBACK FOR PLAIN STOCK MARKET ORDERS: see
notional_cap.py's module docstring -- same fallback, same reasoning, same
live-verified reason a plain market order can never carry a payload price
(HTTP 422 on type="market" + limit_price). Scoped identically:
stock_tool_match only (place_stock_order), not options or crypto.

CROSS-CHUNK EXPOSURE (the "sum across chunks, not just per individual
order" requirement a chunked rebalancer needs): `state[positions_state_key]`
comes from a real GET /v2/positions fetch behind a cache TTL (see
`firewall.account_data.fetch_positions` and
`firewall.proxy.FirewallMiddleware._populate_positions`) -- a burst of
several same-symbol chunk orders fired faster than that TTL (or faster
than Alpaca settles a fill into its own position snapshot) would all see
the SAME stale baseline and each individually pass, even where their sum
breaches the cap. This rule closes that gap itself, using data already in
`state`: it reads `state["order_history"]` (already populated for every
call, see `FirewallMiddleware.on_call_tool`) and adds the notional of any
same-symbol buy recorded *after* `state[positions_fetched_at_key]` -- i.e.
any order this rule itself already approved earlier in the same burst that
the fetched snapshot hasn't caught up to yet -- on top of the fetched
baseline before comparing against the cap. See `_in_flight_exposure`.
DISCLOSED RESIDUAL GAP: an order placed but not yet filled (e.g. queued
outside market hours) is excluded from both the in-flight sum above (only
"open"/"filled" outcomes count, which is correct) AND from the fetched
`market_value` snapshot (a resting, unfilled order has no market value
yet) -- under-counted in that specific window, not corrected here.

DYNAMIC CAP, WITH A STATIC FALLBACK: see notional_cap.py's identical
docstring section -- same reasoning, same "never fail closed on missing
equity" posture (this rule is #2 in policies/default.yaml's evaluation
order, right after notional_cap; a fail-closed posture here would mask
every rule below it the same way), same `max_usd_per_symbol` fallback
whenever `max_pct_of_equity` is unset or equity is unavailable, same
"every preset_*.yaml is unaffected" guarantee.
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel

from firewall.market_data import BarsResult, fetch_daily_bars
from firewall.rules._util import extract_notional, matches_any
from firewall.rules.base import Rule, RuleConfig, RuleOutcome

BarsFetcher = Callable[[str, int], BarsResult]


class _Params(BaseModel):
    max_usd_per_symbol: float
    # See this module's own "DYNAMIC CAP, WITH A STATIC FALLBACK" docstring
    # section. None (the default) means "always use max_usd_per_symbol".
    max_pct_of_equity: float | None = None
    account_equity_state_key: str = "account_equity"
    tool_match: list[str] = ["order"]
    symbol_field: str = "symbol"
    side_field: str = "side"
    notional_field: str = "notional"
    qty_field: str = "qty"
    price_field: str = "limit_price"
    # Expected shape: state[positions_state_key] == {symbol: current_usd_exposure}
    positions_state_key: str = "positions"
    # time.monotonic() the positions fetch above actually ran (not a
    # cache-hit call's own time) -- see this module's own "CROSS-CHUNK
    # EXPOSURE" docstring section and _in_flight_exposure below.
    positions_fetched_at_key: str = "positions_fetched_at"
    order_history_state_key: str = "order_history"
    # See notional_cap.py's identical fields and its module docstring for
    # the full reasoning. NOT place_option_order/place_crypto_order --
    # narrower than "any order with no price," same scope limits as
    # notional_cap's own stock_tool_match.
    stock_tool_match: list[str] = ["place_stock_order"]
    price_lookback_days: int = 5
    market_data_timeout_seconds: float = 5.0
    bars_cache_ttl_seconds: float = 1800.0
    # See notional_cap.py's identical field and its module docstring for the
    # full reasoning (verified against alpaca-mcp-server's real input
    # schemas): NOT place_stock_order/place_option_order/place_crypto_order
    # -- a plain market buy order legitimately carries qty and no price at
    # all, and that's normal, not incomplete data. Only replace_order_by_id
    # fails closed on a None notional: Alpaca lets a qty-only amendment
    # through while silently leaving the existing resting price untouched,
    # a price this firewall has no visibility into.
    sizing_tool_match: list[str] = ["replace_order_by_id"]


class PositionCapRule(Rule):
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

    def _reference_notional(
        self, symbol: str, arguments: dict[str, Any]
    ) -> RuleOutcome | float | None:
        """See notional_cap.py's identical method for the full reasoning."""
        qty_raw = (
            arguments.get(self.cfg.qty_field)
            if arguments.get(self.cfg.qty_field) is not None
            else arguments.get("quantity")
        )
        if qty_raw is None:
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

    def _in_flight_exposure(self, symbol: str, state: dict[str, Any]) -> float:
        """Notional of same-symbol buys recorded in `order_history` since
        the positions snapshot was fetched -- see this module's own
        "CROSS-CHUNK EXPOSURE" docstring section. Returns 0.0 (not
        fail-closed) on anything unpriceable: this is an *addition* on top
        of an already-fail-open baseline (missing `positions` already
        defaults to 0.0 current exposure, see `check` below), so an
        in-flight entry this can't price is dropped from the sum rather
        than blocking the whole call -- consistent with position_cap's
        existing missing-positions posture, not a new fail-closed surface.
        """
        order_history = state.get(self.cfg.order_history_state_key)
        if not order_history:
            return 0.0
        fetched_at = state.get(self.cfg.positions_fetched_at_key)
        if fetched_at is None:
            # No successful positions fetch to reconcile against this call
            # (missing or failed) -- nothing is "since" an unknown baseline.
            return 0.0

        total = 0.0
        for event in order_history:
            if event.symbol != symbol:
                continue
            if str(event.side).lower() != "buy":
                continue
            if event.outcome not in ("open", "filled"):
                continue
            if event.timestamp <= fetched_at:
                continue
            if event.price is not None:
                total += event.qty * event.price
                continue
            fallback = self._reference_notional(symbol, {self.cfg.qty_field: event.qty})
            if isinstance(fallback, RuleOutcome) or fallback is None:
                continue
            total += fallback
        return total

    def _effective_cap(self, state: dict[str, Any]) -> tuple[float, bool]:
        """See notional_cap.py's identical method for the full reasoning."""
        if self.cfg.max_pct_of_equity is not None:
            equity = state.get(self.cfg.account_equity_state_key)
            if isinstance(equity, (int, float)) and not isinstance(equity, bool):
                return equity * self.cfg.max_pct_of_equity, True
        return self.cfg.max_usd_per_symbol, False

    def check(
        self, tool_name: str, arguments: dict[str, Any], state: dict[str, Any]
    ) -> RuleOutcome:
        if not matches_any(tool_name, self.cfg.tool_match):
            return RuleOutcome(False)

        symbol = arguments.get(self.cfg.symbol_field)
        if not symbol:
            return RuleOutcome(False)

        # A sell reduces long exposure; this cap only guards against
        # accumulating too large a position in one instrument.
        side = str(arguments.get(self.cfg.side_field, "buy")).lower()
        if side == "sell":
            return RuleOutcome(False)

        notional = extract_notional(
            arguments, self.cfg.notional_field, self.cfg.qty_field, self.cfg.price_field
        )
        if notional is None and matches_any(tool_name, self.cfg.stock_tool_match):
            fallback = self._reference_notional(symbol, arguments)
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

        positions: dict[str, Any] = state.get(self.cfg.positions_state_key) or {}
        current = float(positions.get(symbol, 0.0)) + self._in_flight_exposure(symbol, state)
        prospective = current + notional

        cap, is_dynamic = self._effective_cap(state)
        if prospective > cap:
            cap_desc = (
                f"${cap:,.2f} ({self.cfg.max_pct_of_equity:.1%} of equity)"
                if is_dynamic
                else f"${cap:,.2f}"
            )
            return RuleOutcome(
                True,
                f"prospective exposure ${prospective:,.2f} for {symbol} exceeds cap "
                f"{cap_desc} (current ${current:,.2f} + order ${notional:,.2f})",
            )
        return RuleOutcome(False)
