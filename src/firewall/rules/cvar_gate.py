"""cvar_gate — hard block orders whose Conditional Value at Risk (CVaR) on a
historical-simulation basis exceeds a configured fraction of current
account equity.

Historical simulation, not a parametric assumption: daily close prices are
pulled (via `firewall.market_data.fetch_daily_bars`) over a lookback
window, converted to daily log returns, and resampled as-is to build a
simulated daily P&L series for the proposed position size. No normal/t-
distribution (or any other parametric shape) is fit to the returns -- the
empirical tail is used directly.

CAVEAT -- short lookback windows produce a noisy tail estimate. This is a
known limitation of historical CVaR, not a bug: the whole point of CVaR is
to characterize the average of the worst (1 - alpha) fraction of
outcomes, but tail events are by definition rare. A 90-day window (the
default `cvar_lookback_days`) has only ~90 samples, so at alpha=0.95 the
"worst 5%" tail being averaged is just ~4-5 data points -- a handful of
observations standing in for the whole left tail of the distribution.
Widening the window trades this noise for staleness (older data reflecting
a different volatility regime); there is no lookback length that escapes
the tradeoff. This gate is one signal among several, not a precise risk
bound, and this caveat must also be stated in the README's "what this
does not do" section.

Missing or bad market data fails CLOSED, not open: if fewer than two
closes come back, or the fetch itself failed, this rule hard-blocks rather
than letting an unassessed order through. The same applies to account
equity: the loss cap is a percentage of current equity
(`cvar_max_loss_pct_of_equity`), read from
`state[account_equity_state_key]` (default key: "account_equity"). If
that value is absent or not numeric, this rule cannot compute a dollar
threshold at all and hard-blocks rather than skipping the check.
"""

from __future__ import annotations

import math
from typing import Any, Callable

from pydantic import BaseModel

from firewall.market_data import BarsResult, fetch_daily_bars
from firewall.rules._util import extract_notional, matches_any
from firewall.rules.base import Rule, RuleConfig, RuleOutcome

BarsFetcher = Callable[[str, int], BarsResult]


class _Params(BaseModel):
    cvar_lookback_days: int = 90
    cvar_alpha: float = 0.95
    cvar_max_loss_pct_of_equity: float
    tool_match: list[str] = ["order"]
    symbol_field: str = "symbol"
    notional_field: str = "notional"
    qty_field: str = "qty"
    price_field: str = "limit_price"
    market_data_timeout_seconds: float = 5.0
    bars_cache_ttl_seconds: float = 1800.0
    account_equity_state_key: str = "account_equity"


def _log_returns(closes: list[float]) -> list[float]:
    return [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]


def compute_cvar(pnl_series: list[float], alpha: float) -> float | None:
    """CVaR (expected shortfall) of a P&L series at confidence `alpha`.

    Sorts ascending and averages the worst (1 - alpha) fraction of
    outcomes. The tail size is rounded up (`ceil`), not down, so the
    averaged tail never covers less than the requested probability mass;
    at least one observation is always included. Returns None if the
    series is empty.
    """
    if not pnl_series:
        return None

    ordered = sorted(pnl_series)
    tail_size = max(1, math.ceil(len(ordered) * (1 - alpha)))
    tail = ordered[:tail_size]
    return sum(tail) / len(tail)


def get_cvar_from_distribution(
    quantiles: list[tuple[float, float]],
    alpha: float = 0.95,
) -> float | None:
    """CVaR from an arbitrary (probability, return) distribution, e.g. a
    forecast rather than historical bars -- same sort-and-average-the-tail
    calculation as `compute_cvar`, so another source's forecast
    distribution can feed this later without touching that function's
    core logic. Not wired into `CVaRGateRule` yet.

    Entries here can carry unequal probability mass, so the tail average
    is probability-weighted rather than a plain arithmetic mean: outcomes
    are consumed whole, sorted worst-first, until at least (1 - alpha) of
    the total probability mass is covered -- the bucket that crosses the
    threshold is included in full, not split at the boundary. This
    mirrors `compute_cvar`'s `ceil` rounding (round the tail size up, up
    to and including the crossing sample, never down) rather than a
    continuous integral, which makes the two functions agree exactly on
    uniform-weight input: `get_cvar_from_distribution([(1/n, r) for r in
    returns], alpha)` equals `compute_cvar(returns, alpha)` for any
    `returns` list of length `n` (see the equivalence test).
    """
    if not quantiles:
        return None

    total_probability = sum(probability for probability, _ in quantiles)
    if total_probability <= 0:
        return None

    ordered = sorted(quantiles, key=lambda pair: pair[1])
    tail_mass_target = (1 - alpha) * total_probability

    weighted_sum = 0.0
    covered_mass = 0.0
    for probability, outcome in ordered:
        weighted_sum += outcome * probability
        covered_mass += probability
        if covered_mass >= tail_mass_target:
            break

    if covered_mass <= 0:
        return None

    return weighted_sum / covered_mass


class CVaRGateRule(Rule):
    def __init__(
        self,
        config: RuleConfig,
        bars_fetcher: BarsFetcher | None = None,
    ) -> None:
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

    def check(
        self, tool_name: str, arguments: dict[str, Any], state: dict[str, Any]
    ) -> RuleOutcome:
        if not matches_any(tool_name, self.cfg.tool_match):
            return RuleOutcome(False)

        symbol = arguments.get(self.cfg.symbol_field)
        if not symbol:
            return RuleOutcome(False)

        notional = extract_notional(
            arguments, self.cfg.notional_field, self.cfg.qty_field, self.cfg.price_field
        )
        if notional is None:
            return RuleOutcome(False)

        equity = state.get(self.cfg.account_equity_state_key)
        if not isinstance(equity, (int, float)) or isinstance(equity, bool):
            return RuleOutcome(
                True,
                "insufficient account state to assess risk — failing closed: "
                f"state[{self.cfg.account_equity_state_key!r}] is missing or not numeric",
            )
        max_loss_usd = equity * self.cfg.cvar_max_loss_pct_of_equity

        result = self._bars_fetcher(symbol, self.cfg.cvar_lookback_days)
        if not result.ok or len(result.closes) < 2:
            reason = result.reason if not result.ok else "fewer than 2 daily bars returned"
            return RuleOutcome(
                True,
                f"insufficient market data to assess risk — failing closed: {reason}",
            )

        returns = _log_returns(result.closes)
        pnl_series = [notional * r for r in returns]
        cvar = compute_cvar(pnl_series, self.cfg.cvar_alpha)
        if cvar is None:
            return RuleOutcome(
                True,
                "insufficient market data to assess risk — failing closed: "
                "no return series could be computed",
            )

        if abs(cvar) > max_loss_usd:
            return RuleOutcome(
                True,
                f"CVaR estimate ${abs(cvar):,.2f} at alpha={self.cfg.cvar_alpha} "
                f"over a {self.cfg.cvar_lookback_days}-day historical lookback "
                f"exceeds max loss ${max_loss_usd:,.2f} "
                f"({self.cfg.cvar_max_loss_pct_of_equity:.1%} of ${equity:,.2f} equity) "
                f"for {symbol}",
            )
        return RuleOutcome(False)
