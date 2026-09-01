"""Portfolio P&L attribution and benchmark comparison for display/reporting."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def build_performance_summary(
    *,
    session_pnl_usd: float | None,
    last_equity: float | None,
    intraday_pnl: Mapping[str, float] | None,
    benchmark_closes: Sequence[float] | None,
    benchmark_symbol: str = "SPY",
) -> dict[str, Any]:
    """Build an honest, side-effect-free performance snapshot.

    Alpaca's account session P&L is authoritative for the portfolio total.
    Per-position ``unrealized_intraday_pl`` values are attribution estimates;
    their residual versus account P&L captures cash, options, realized P&L,
    fees, and positions for which Alpaca supplied no attribution field.
    """
    portfolio_return = None
    if session_pnl_usd is not None and last_equity not in (None, 0):
        portfolio_return = session_pnl_usd / float(last_equity)

    benchmark_return = None
    if benchmark_closes and len(benchmark_closes) >= 2:
        previous, current = float(benchmark_closes[-2]), float(benchmark_closes[-1])
        if previous:
            benchmark_return = current / previous - 1.0

    attribution = sorted(
        ((str(symbol), float(pnl)) for symbol, pnl in (intraday_pnl or {}).items()),
        key=lambda item: abs(item[1]),
        reverse=True,
    )
    attributed_total = sum(value for _, value in attribution)
    residual = (
        float(session_pnl_usd) - attributed_total
        if session_pnl_usd is not None
        else None
    )
    alpha = (
        portfolio_return - benchmark_return
        if portfolio_return is not None and benchmark_return is not None
        else None
    )
    return {
        "portfolio_pnl_usd": session_pnl_usd,
        "portfolio_return": portfolio_return,
        "benchmark_symbol": benchmark_symbol,
        "benchmark_return": benchmark_return,
        "excess_return": alpha,
        "attribution": attribution,
        "attributed_total_usd": attributed_total,
        "residual_usd": residual,
    }
