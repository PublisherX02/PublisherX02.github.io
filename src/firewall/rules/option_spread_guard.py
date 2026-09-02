"""option_spread_guard — hard block MARKET option orders on a contract
whose current relative bid/ask spread exceeds a configured maximum, to
protect against high slippage on a thinly-quoted contract.

Regulation: SEC Rule 15c3-5(c)(1)(ii) — pre-trade controls must prevent
the entry of erroneous orders "by rejecting orders that exceed
appropriate price or size parameters." This rule enforces the price-
parameter clause of that same subsection directly: relative spread is a
live, market-derived price-quality parameter, and a market order against
a spread this wide has no price protection of its own, so it is rejected
before it can fill at an erroneous effective price. `order_rate_throttle`
already cites this same subsection for its *other* clause ("or that
indicate duplicative orders", over a short period of time) — two rules,
two different clauses of the one subsection, not a duplicate citation.

RELATIVE SPREAD = (ask - bid) / ask, computed from the option's own live
quote — `firewall.market_data.fetch_option_latest_quote`'s
`snapshots[symbol].latestQuote`, verified against the real OpenAPI spec
(see that module's `DEFAULT_OPTION_FEED` comment for the free-tier feed
caveat this inherits). "Exceeds" is strict (`>`, not `>=`): a spread
landing exactly on the configured threshold passes, matching every other
threshold rule in this package (`notional_cap`, `position_cap`, ...).

MARKET ORDERS ONLY, by design, not by omission: a limit order already
states its own price protection via `limit_price` — the trader has
already bounded the erroneous-price risk this rule exists to catch, so
blocking a careful limit order on a wide-spread contract the same way
would be a false positive, not defense-in-depth. `type` defaults to
"market" when omitted (verified against the live `place_option_order`
inputSchema — same default `notional_cap`/`position_cap`'s module
docstrings already document), so an order with no `type` field is
treated as a market order, not skipped as "not specified."

SINGLE-LEG ONLY, a deliberate scope boundary, not an oversight: this rule
reads the parent call's `symbol` and fetches one quote for it. Multi-leg
`place_option_order` calls carry no parent `symbol` (see
`option_expiry_floor.py`/`symbol_allowlist.py`'s module docstrings for
the verified schema evidence) and this rule does not fetch each leg's
quote — unlike `option_expiry_floor`'s any-leg-triggers extension (a pure
symbol-format parse, no network cost per leg), checking N legs' live
spreads here means N market-data round trips per call plus an unresolved
design question this task didn't ask to answer: which leg's (or which
combination's) spread should govern a market order on a multi-leg
net-debit/credit structure. Left as a disclosed gap, matching this
project's established pattern, not silently extended.

MISSING OR BAD MARKET DATA FAILS CLOSED, not open — same convention
`cvar_gate`/`pct_of_adv` already use for a market-data-dependent hard
rule: if the quote can't be fetched, the contract has no recent quote
activity, or the response is malformed, this rule hard-blocks rather than
letting an unassessed market order through. A market order is exactly the
order type where "can't verify the spread" and "spread is unacceptably
wide" carry the same practical risk (an unbounded fill price), so failing
open here would defeat the rule's own purpose.
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel

from firewall.market_data import OptionQuoteResult, fetch_option_latest_quote
from firewall.rules._util import matches_any, parse_occ_underlying
from firewall.rules.base import Rule, RuleConfig, RuleOutcome

QuoteFetcher = Callable[[str], OptionQuoteResult]


class _Params(BaseModel):
    max_relative_spread: float = 0.15
    tool_match: list[str] = ["order"]
    symbol_field: str = "symbol"
    type_field: str = "type"
    market_data_timeout_seconds: float = 5.0
    quote_cache_ttl_seconds: float = 5.0
    feed: str = "indicative"


class OptionSpreadGuardRule(Rule):
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

        order_type = str(arguments.get(self.cfg.type_field, "market")).strip().lower()
        if order_type != "market":
            return RuleOutcome(False)

        symbol = arguments.get(self.cfg.symbol_field)
        if not symbol or parse_occ_underlying(symbol) is None:
            # No parent symbol (multi-leg order) or a symbol that isn't
            # OCC-format (a stock/crypto market order carries a plain
            # ticker in this same field, e.g. place_stock_order's "AAPL")
            # -- either way, not a single-leg option order this rule can
            # assess. Deliberately out of scope; see module docstring.
            return RuleOutcome(False)

        result = self._quote_fetcher(symbol)
        if not result.ok or result.quote is None:
            return RuleOutcome(
                True,
                "insufficient market data to assess risk — failing closed: "
                f"could not obtain a usable quote for {symbol} ({result.reason})",
            )

        bid, ask = result.quote.bid, result.quote.ask
        if ask <= 0 or bid < 0 or bid > ask:
            return RuleOutcome(
                True,
                f"invalid or crossed option quote for {symbol}: "
                f"bid {bid:,.2f} / ask {ask:,.2f}",
            )
        relative_spread = (ask - bid) / ask

        if relative_spread > self.cfg.max_relative_spread:
            return RuleOutcome(
                True,
                f"relative spread {relative_spread:.1%} exceeds maximum "
                f"{self.cfg.max_relative_spread:.1%}, high slippage risk, use a limit "
                f"order or a more liquid strike ({symbol}: bid ${bid:,.2f} / "
                f"ask ${ask:,.2f})",
            )
        return RuleOutcome(False)
