"""iv_hv_ratio — hard block a single-leg option order if the option's own
implied volatility (IV) is elevated relative to the underlying's recent
historical volatility (HV), past a configured ratio: this is priced-in
richness the order's own premium is paying for, independent of whether
the order is otherwise correctly sized/hedged/expiry-safe (those are
`net_delta_floor`/`option_spread_guard`/`option_expiry_floor`'s own,
separate concerns).

UNMAPPED, same as `cvar_gate`/`pct_of_adv` (see their own module/policy
docstrings): no verified US federal/FINRA citation confirmed specifically
for an IV/HV volatility-richness gate; `regulation_ref` stays null until
one is verified. This rule is a statistical risk gate in the
`cvar_gate`/`pct_of_adv` family, not the size-cap family `notional_cap`/
`order_rate_throttle` cite SEC Rule 15c3-5(c)(1)(ii) for — do not
approximate a citation here.

REUSES cvar_gate's OWN bars fetch, cache, and lookback window — not a
second market-data pipeline for the same symbol. `cvar_gate` itself has
never computed a historical-volatility figure before this rule (verified:
`cvar_gate.py` computes CVaR — a tail average of a simulated P&L series —
and nowhere computes a standard deviation; grepped for
"volatility", "stdev", and "statistics." across `src/` and found no prior HV
computation anywhere in this codebase). What this rule reuses is
`cvar_gate`'s bars *fetcher and cache* and its *chosen lookback window*,
plus its `_log_returns` pure function — not a pre-existing HV value,
because none exists. It reads a live `CVaRGateRule` instance from
`state[cvar_gate_rule_state_key]` (default key: "cvar_gate_rule") and
calls that instance's own `._bars_fetcher` with that instance's own
`.cfg.cvar_lookback_days` — the exact same bars, from the exact same
`firewall.market_data.fetch_daily_bars` cache entry, `cvar_gate`'s own
`check()` would use for this symbol on the same call — then computes HV
itself, here, for the first time. `_log_returns` is imported directly
from `firewall.rules.cvar_gate` for the same reason (the same pure
function, not a re-derivation) — `hedge_proposal.py` already established
this exact reuse precedent for the same two names. Nothing in `src/`
populates `state["cvar_gate_rule"]` today — a policy-loader/proxy wiring
gap of the same disclosed shape as `hedge_proposal`'s own
`cvar_gate_rule`/`drawdown_killswitch_rule` constructor references, and
`net_delta_floor` not yet being registered in `RULE_TYPES`. A missing or
wrong-typed value at that state key fails CLOSED (this rule cannot
compute HV without a real `CVaRGateRule` to reuse, and must not silently
skip the check).

USES THE SAME OPTION SNAPSHOT FETCH AS option_spread_guard/net_delta_floor,
not a second one: `firewall.market_data.fetch_option_latest_quote`
already returns `.iv` from the identical `GET /v1beta1/options/snapshots`
response those rules read `.bid`/`.ask`/`.delta` from — one HTTP call and
one 5-second cache entry per option symbol serves all three rules within
the same `evaluate()` pass ("one fetch, two checks": if `net_delta_floor`
already fetched this exact symbol moments earlier in the same call, this
rule's own fetch is a cache hit, not a second round trip). `impliedVolatility`
is a separately optional field of Alpaca's `option_snapshot` schema, like
`greeks` — a quote can be usable (`ok=True`) with `iv=None`, which this
rule treats as its own failure to assess, failing closed the same way an
unfetchable quote does.

ANNUALIZED, not raw daily stdev — comparing units correctly matters here.
Alpaca's OpenAPI spec (`market-data-api.json`'s `option_snapshot` schema,
checked 2026-08-26) describes `impliedVolatility` only as "Implied
volatility calculated using the Black-Scholes model" — no units are
stated in the spec text itself, so this is NOT a spec-verified fact the
way the x100 contract multiplier was. It is convention-assumed: IV is
near-universally quoted as an annualized figure across options markets
(the standard Black-Scholes convention this same field description
invokes), and `cvar_gate`'s own `_log_returns` are per-DAY log returns,
so treating them as directly comparable without converting would be
wrong under the near-certain convention even though the spec doesn't
spell it out. `compute_annualized_hv` converts the daily sample standard
deviation of those returns to that assumed annualized convention
(`daily_stdev * sqrt(annualization_trading_days)`, default 252 — the
conventional US trading-day count) before this rule ever divides IV by
it. If that convention assumption is ever wrong for Alpaca's specific
feed, this ratio is off by roughly sqrt(252) ≈ 15.87x — treat this line
as the one to re-verify first if `iv_hv_ratio`'s trigger rate looks
implausible in practice. Comparing an annualized IV directly against a
raw daily stdev would make the resulting ratio
meaningless — the same shape of unit-mismatch bug `net_delta_floor`'s own
x100 contract multiplier exists to prevent for delta.

FLAT RATIO THRESHOLD (`max_iv_hv_ratio`, default 1.5), not derived from
anything — same disclosed-heuristic framing as `net_delta_floor`'s
`structural_delta_floor`: a configurable judgment call, not a figure
backed by any measured tail-risk or options-pricing-theory derivation.

REJECT AND EXPLAIN, never auto-remediate — same principle
`gtc_restriction` already applies to a rejected GTC order: this rule only
ever rejects a call it disagrees with and states why in plain language.
It never rewrites the order into a spread/collar or any other structure
on the agent's behalf, and never submits anything itself — the agent
must resubmit a new call if it wants to proceed differently.

SINGLE-LEG ONLY, same scope boundary as `option_spread_guard`/
`net_delta_floor`, same reasoning: self-scopes via a valid OCC-format
parent `symbol`. Multi-leg orders (no parent `symbol`) are a disclosed,
deliberate gap — a spread's own priced-in volatility is a function of
every leg's own IV net of the others, which this rule does not attempt
to combine.
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Callable

from pydantic import BaseModel

from firewall.market_data import OptionQuoteResult, fetch_option_latest_quote
from firewall.rules._util import matches_any, parse_occ_underlying
from firewall.rules.base import Rule, RuleConfig, RuleOutcome
from firewall.rules.cvar_gate import CVaRGateRule, _log_returns

QuoteFetcher = Callable[[str], OptionQuoteResult]

DEFAULT_ANNUALIZATION_TRADING_DAYS = 252.0


class _Params(BaseModel):
    max_iv_hv_ratio: float = 1.5
    annualization_trading_days: float = DEFAULT_ANNUALIZATION_TRADING_DAYS
    tool_match: list[str] = ["order"]
    symbol_field: str = "symbol"
    cvar_gate_rule_state_key: str = "cvar_gate_rule"
    market_data_timeout_seconds: float = 5.0
    quote_cache_ttl_seconds: float = 5.0
    feed: str = "indicative"


class IVHVRatioRule(Rule):
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

        result = self._quote_fetcher(symbol)
        if not result.ok or result.quote is None or result.quote.iv is None:
            reason = result.reason if result.ok is False else "no implied volatility available"
            return RuleOutcome(
                True,
                "insufficient market data to assess risk — failing closed: "
                f"could not obtain a usable implied volatility for {symbol} ({reason})",
            )
        iv = result.quote.iv

        cvar_gate_rule = state.get(self.cfg.cvar_gate_rule_state_key)
        if not isinstance(cvar_gate_rule, CVaRGateRule):
            return RuleOutcome(
                True,
                "insufficient market data to assess risk — failing closed: "
                f"state[{self.cfg.cvar_gate_rule_state_key!r}] does not hold a "
                "live cvar_gate rule to reuse for historical volatility",
            )

        bars_result = cvar_gate_rule._bars_fetcher(
            underlying, cvar_gate_rule.cfg.cvar_lookback_days
        )
        if not bars_result.ok or len(bars_result.closes) < 3:
            reason = (
                bars_result.reason
                if not bars_result.ok
                else "fewer than 3 daily bars returned (need at least 2 daily "
                "returns for a volatility estimate)"
            )
            return RuleOutcome(
                True,
                f"insufficient market data to assess risk — failing closed: {reason}",
            )

        returns = _log_returns(bars_result.closes)
        hv = compute_annualized_hv(returns, self.cfg.annualization_trading_days)
        if hv is None or hv <= 0:
            # Division by zero/undefined is a market-data problem here, not
            # "infinitely elevated" -- see module docstring.
            return RuleOutcome(
                True,
                "insufficient market data to assess risk — failing closed: "
                f"computed historical volatility for {underlying} over "
                f"{cvar_gate_rule.cfg.cvar_lookback_days}-day lookback is zero "
                "or undefined",
            )

        ratio = iv / hv
        if ratio > self.cfg.max_iv_hv_ratio:
            return RuleOutcome(
                True,
                "elevated IV relative to historical volatility — this option's "
                f"implied volatility ({iv:.2%}) is {ratio:.2f}x {underlying}'s "
                f"annualized historical volatility ({hv:.2%}, {cvar_gate_rule.cfg.cvar_lookback_days}-day "
                f"lookback), above the configured {self.cfg.max_iv_hv_ratio:.2f}x "
                "ceiling. Resubmit once IV normalizes or choose a different "
                "contract — this firewall does not construct an alternative "
                "structure (spread/collar) on your behalf.",
            )
        return RuleOutcome(False)


def compute_annualized_hv(
    daily_log_returns: list[float],
    annualization_trading_days: float = DEFAULT_ANNUALIZATION_TRADING_DAYS,
) -> float | None:
    """Annualized historical volatility from daily log returns: the sample
    standard deviation of `daily_log_returns` (Bessel-corrected, matching
    `statistics.stdev`'s n-1 convention), scaled to the same annualized
    convention Alpaca's own `impliedVolatility` uses
    (`* sqrt(annualization_trading_days)`).

    Returns None if there are fewer than 2 returns (sample variance is
    undefined for n<2, not zero) -- callers must distinguish that from a
    genuinely flat price series, which correctly returns 0.0.
    """
    if len(daily_log_returns) < 2:
        return None
    return statistics.stdev(daily_log_returns) * math.sqrt(annualization_trading_days)
