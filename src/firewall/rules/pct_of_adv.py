"""pct_of_adv — hard block orders whose proposed share size exceeds a
configured fraction of the symbol's average daily volume (ADV), computed
from historical daily bars (via `firewall.market_data.fetch_daily_bars`)
over a lookback window.

An order that is large relative to a symbol's typical daily volume is a
market-impact risk: it can move the price against the trader on the way
in, and is the kind of disorderly order flow SEC Rule 15c3-5 pre-trade
controls are meant to catch before it reaches the market.

The proposed order's dollar notional is converted to a share count using
the most recent close from the same bars fetch as "current price" --
this rule makes no separate live-quote call.

NOTE: this project uses Alpaca's free/Basic market data plan (IEX-only
real-time quotes; historical bars are unrestricted past a 15-minute
delay). IEX-only volume can understate true consolidated (all-exchanges)
ADV, which understates the true denominator here -- that makes
`pct_of_adv` bias toward being stricter than necessary (flagging orders
that a full-tape ADV would have allowed), never looser. For a pre-trade
risk control, over-blocking on incomplete data is the correct failure
direction; under-blocking would not be.

Missing or bad market data fails CLOSED, not open: if fewer than two bars
come back, the fetch itself failed, or the resulting ADV is zero, this
rule hard-blocks rather than letting an unassessed order through.
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel

from firewall.market_data import BarsResult, fetch_daily_bars
from firewall.rules._util import extract_notional, matches_any
from firewall.rules.base import Rule, RuleConfig, RuleOutcome

BarsFetcher = Callable[[str, int], BarsResult]

_INSUFFICIENT_VOLUME_HISTORY = "insufficient volume history to assess liquidity risk"


class _Params(BaseModel):
    max_percent_of_adv: float = 0.01
    adv_lookback_days: int = 30
    tool_match: list[str] = ["order"]
    symbol_field: str = "symbol"
    notional_field: str = "notional"
    qty_field: str = "qty"
    price_field: str = "limit_price"
    market_data_timeout_seconds: float = 5.0
    bars_cache_ttl_seconds: float = 1800.0


class PctOfAdvRule(Rule):
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

        proposed_notional = extract_notional(
            arguments, self.cfg.notional_field, self.cfg.qty_field, self.cfg.price_field
        )
        if proposed_notional is None:
            return RuleOutcome(False)

        result = self._bars_fetcher(symbol, self.cfg.adv_lookback_days)
        if not result.ok or len(result.closes) < 2:
            detail = result.reason if not result.ok else "fewer than 2 daily bars returned"
            return RuleOutcome(
                True,
                "insufficient market data to assess risk — failing closed: "
                f"{_INSUFFICIENT_VOLUME_HISTORY} ({detail})",
            )

        volumes = result.volumes
        avg_daily_volume = sum(volumes) / len(volumes)
        if avg_daily_volume <= 0:
            return RuleOutcome(
                True,
                "insufficient market data to assess risk — failing closed: "
                f"{_INSUFFICIENT_VOLUME_HISTORY} (average daily volume is zero "
                f"for {symbol} over the {self.cfg.adv_lookback_days}-day lookback)",
            )

        current_price = result.closes[-1]
        if current_price <= 0:
            return RuleOutcome(
                True,
                "insufficient market data to assess risk — failing closed: "
                f"{_INSUFFICIENT_VOLUME_HISTORY} (non-positive close price "
                f"${current_price:,.2f} for {symbol})",
            )

        proposed_shares = proposed_notional / current_price
        pct_of_adv = proposed_shares / avg_daily_volume
        if pct_of_adv > self.cfg.max_percent_of_adv:
            return RuleOutcome(
                True,
                f"Order exceeds {pct_of_adv:.1%} of {self.cfg.adv_lookback_days}-day "
                f"ADV ({avg_daily_volume:,.0f} shares) — high slippage/market-impact risk.",
            )
        return RuleOutcome(False)
