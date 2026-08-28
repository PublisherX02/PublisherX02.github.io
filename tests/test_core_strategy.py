"""Tests for core_strategy -- the inverse-volatility weighted basket rebalancer.

Verifies (a) realized volatility calculation (reusing cvar_gate._log_returns),
(b) inverse-volatility weighting and risk allocation, (c) drift-based
rebalancing logic, (d) position parsing, and (e) end-to-end runs through the
real default policy firewall via fake upstreams.
"""

from __future__ import annotations

import asyncio
import json
import math

import pytest
from fastmcp import Client, FastMCP

import core_strategy
from firewall.audit import AuditLogWriter
from firewall.market_data import BarsResult, DailyBar
from firewall.policy import PolicyEngine
from firewall.proxy import build_proxy


def _real_policy_engine(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    writer = AuditLogWriter(log_path, session_id="test-session")
    engine = PolicyEngine.from_yaml("policies/default.yaml", audit_writer=writer)
    return engine, log_path


def _fake_bars_fetcher(prices: dict[str, float] | dict[str, list[float]]):
    def fetch(symbol: str, lookback_days: int) -> BarsResult:
        if symbol not in prices:
            return BarsResult(ok=False, reason=f"no fake price for {symbol}")
        val = prices[symbol]
        if isinstance(val, list):
            bars = [DailyBar(close=c, volume=1_000_000.0) for c in val]
        else:
            # 5 daily closes around the base price
            bars = [DailyBar(close=val * (1.0 + 0.005 * i), volume=1_000_000.0) for i in range(5)]
        return BarsResult(ok=True, bars=bars)

    return fetch


# --- pure helpers & inverse-volatility weighting ------------------------


def test_basket_is_small_fixed_and_disclosed():
    assert 3 <= len(core_strategy.BASKET) <= 5
    assert len(set(core_strategy.BASKET)) == len(core_strategy.BASKET)  # no duplicates
    assert all(isinstance(s, str) and s.isupper() for s in core_strategy.BASKET)


def test_compute_realized_volatility():
    # 3 closes -> 2 log returns
    closes = [100.0, 105.0, 102.0]
    vol = core_strategy.compute_realized_volatility(closes)
    assert vol > 0.0
    assert math.isfinite(vol)

    # Fewer than 2 closes raises ValueError
    with pytest.raises(ValueError):
        core_strategy.compute_realized_volatility([100.0])
    with pytest.raises(ValueError):
        core_strategy.compute_realized_volatility([])


def test_compute_inverse_vol_weights_allocates_equal_risk():
    # AAPL has double the volatility of MSFT
    volatilities = {"AAPL": 0.02, "MSFT": 0.01}
    weights = core_strategy.compute_inverse_vol_weights(volatilities)

    assert pytest.approx(sum(weights.values())) == 1.0
    # Lower vol gets higher weight (MSFT gets 2x weight of AAPL)
    assert pytest.approx(weights["MSFT"] / weights["AAPL"]) == 2.0
    # Risk contribution (weight * vol) is equal across both assets
    assert pytest.approx(weights["AAPL"] * volatilities["AAPL"]) == pytest.approx(
        weights["MSFT"] * volatilities["MSFT"]
    )


def test_compute_inverse_vol_weights_handles_fallback():
    # Empty
    assert core_strategy.compute_inverse_vol_weights({}) == {}
    # Flat / zero volatility fallback to equal weights
    vols = {"AAPL": 0.0, "MSFT": 0.0}
    weights = core_strategy.compute_inverse_vol_weights(vols)
    assert pytest.approx(weights["AAPL"]) == 0.5
    assert pytest.approx(weights["MSFT"]) == 0.5


def test_compute_target_quantities():
    weights = {"AAPL": 0.25, "MSFT": 0.75}
    prices = {"AAPL": 100.0, "MSFT": 200.0}
    # Budget $800: AAPL gets $200 (2 shares), MSFT gets $600 (3 shares)
    targets = core_strategy.compute_target_quantities(weights, prices, total_budget_usd=800.0)
    assert targets["AAPL"] == 2
    assert targets["MSFT"] == 3


def test_compute_target_quantities_enforces_minimum_one_share():
    weights = {"AAPL": 0.1}
    prices = {"AAPL": 5000.0}
    # $800 * 0.1 = $80 budget, but min 1 share enforced
    targets = core_strategy.compute_target_quantities(weights, prices, total_budget_usd=800.0)
    assert targets["AAPL"] == 1


def test_compute_rebalance_orders_drift_thresholding():
    weights = {"AAPL": 0.5, "MSFT": 0.5}
    targets = {"AAPL": 5, "MSFT": 5}
    prices = {"AAPL": 100.0, "MSFT": 100.0}

    # 1. Zero initial positions -> orders needed for all
    curr_0 = {"AAPL": 0, "MSFT": 0}
    deltas_0 = core_strategy.compute_rebalance_orders(curr_0, targets, weights, prices, drift_threshold=0.05)
    assert deltas_0 == {"AAPL": 5, "MSFT": 5}

    # 2. Balanced portfolio (50% / 50%) -> drift = 0.0 <= 0.05 -> no orders (delta = 0)
    curr_balanced = {"AAPL": 5, "MSFT": 5}
    deltas_balanced = core_strategy.compute_rebalance_orders(
        curr_balanced, targets, weights, prices, drift_threshold=0.05
    )
    assert deltas_balanced == {"AAPL": 0, "MSFT": 0}

    # 3. Small drift (e.g. 52% vs 48% -> drift = 0.02 <= 0.05) -> no orders
    prices_slight_drift = {"AAPL": 104.0, "MSFT": 96.0}
    deltas_slight = core_strategy.compute_rebalance_orders(
        curr_balanced, targets, weights, prices_slight_drift, drift_threshold=0.05
    )
    assert deltas_slight == {"AAPL": 0, "MSFT": 0}

    # 4. Large drift (e.g. AAPL value $700, MSFT $300 -> 70% vs 30% -> drift = 0.20 > 0.05)
    curr_drifted = {"AAPL": 7, "MSFT": 3}
    deltas_drifted = core_strategy.compute_rebalance_orders(
        curr_drifted, targets, weights, prices, drift_threshold=0.05
    )
    assert deltas_drifted["AAPL"] == -2  # Sell 2 shares of AAPL
    assert deltas_drifted["MSFT"] == 2   # Buy 2 shares of MSFT


def test_market_order_payload_structure():
    buy_payload = core_strategy.build_market_order_payload("AAPL", 3, side="buy")
    assert buy_payload == {
        "symbol": "AAPL",
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "qty": "3",
    }

    sell_payload = core_strategy.build_market_order_payload("MSFT", -2, side="sell")
    assert sell_payload == {
        "symbol": "MSFT",
        "side": "sell",
        "type": "market",
        "time_in_force": "day",
        "qty": "2",
    }


# --- end-to-end against the real, unmodified default policy -------------


def test_basket_orders_clear_the_real_default_policy(tmp_path):
    """The central claim this module's docstring makes: inverse-volatility
    rebalancing orders for every basket name are allowed (not hard-blocked)
    by the real, unmodified policies/default.yaml, and reach the fake upstream
    as qty-only market orders."""
    received: list[tuple[str, dict]] = []

    upstream = FastMCP("fake-alpaca")

    @upstream.tool
    def place_stock_order(
        symbol: str,
        side: str,
        qty: str | None = None,
        type: str = "market",
        time_in_force: str = "day",
        limit_price: str | None = None,
    ) -> dict:
        received.append((symbol, {"side": side, "qty": qty, "type": type, "limit_price": limit_price}))
        return {"order_id": f"fake-{symbol}", "status": "accepted"}

    @upstream.tool
    def get_all_positions() -> list[dict]:
        return []

    engine, log_path = _real_policy_engine(tmp_path)
    proxy = build_proxy(upstream, policy_engine=engine)

    prices = {s: 100.0 + i * 50 for i, s in enumerate(core_strategy.BASKET)}
    bars_fetcher = _fake_bars_fetcher(prices)

    async def run():
        async with Client(proxy) as client:
            return await core_strategy.place_basket_orders(
                client, bars_fetcher=bars_fetcher, include_options_overlay=False
            )

    attempts = asyncio.run(run())

    assert len(attempts) == len(core_strategy.BASKET)
    for attempt in attempts:
        assert attempt.forwarded, attempt.detail
        assert attempt.qty is not None and attempt.qty >= 1

    assert {sym for sym, _ in received} == set(core_strategy.BASKET)
    for symbol, args in received:
        assert args["side"] == "buy"
        assert args["type"] == "market"
        assert args["limit_price"] is None
        assert int(args["qty"]) >= 1

    records = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    assert len(records) >= len(core_strategy.BASKET)


def test_rebalance_skips_when_drift_within_threshold(tmp_path):
    """When existing positions already match target weights within drift_threshold,
    no orders are placed upstream, avoiding unnecessary turnover and firewall load."""
    received: list[tuple[str, dict]] = []

    upstream = FastMCP("fake-alpaca")

    @upstream.tool
    def place_stock_order(
        symbol: str,
        side: str,
        qty: str | None = None,
        type: str = "market",
        time_in_force: str = "day",
        limit_price: str | None = None,
    ) -> dict:
        received.append((symbol, {"side": side, "qty": qty, "type": type, "limit_price": limit_price}))
        return {"order_id": f"fake-{symbol}", "status": "accepted"}

    engine, _ = _real_policy_engine(tmp_path)
    proxy = build_proxy(upstream, policy_engine=engine)

    # Identical prices and equal volatilities -> equal 25% target weights
    prices = {s: 100.0 for s in core_strategy.BASKET}
    bars_fetcher = _fake_bars_fetcher(prices)

    # Positions already held: 2 shares of each ($200 each, 25% each -> 0% drift)
    existing_positions = {s: 2 for s in core_strategy.BASKET}

    async def run():
        async with Client(proxy) as client:
            return await core_strategy.place_basket_orders(
                client,
                bars_fetcher=bars_fetcher,
                current_positions=existing_positions,
                total_budget_usd=800.0,
                drift_threshold=0.05,
                include_options_overlay=False,
            )

    attempts = asyncio.run(run())

    assert len(attempts) == len(core_strategy.BASKET)
    for attempt in attempts:
        assert attempt.forwarded
        assert attempt.qty == 0
        assert "no rebalance needed" in attempt.detail

    # Zero orders sent upstream
    assert len(received) == 0


def test_unpriceable_symbol_is_skipped_not_fatal(tmp_path):
    """A bars-fetch failure for one name doesn't abort the whole cycle --
    the remaining basket names still get evaluated and proposed."""
    upstream = FastMCP("fake-alpaca")
    received: list[str] = []

    @upstream.tool
    def place_stock_order(
        symbol: str,
        side: str,
        qty: str | None = None,
        type: str = "market",
        time_in_force: str = "day",
        limit_price: str | None = None,
    ) -> dict:
        received.append(symbol)
        return {"order_id": f"fake-{symbol}", "status": "accepted"}

    engine, _ = _real_policy_engine(tmp_path)
    proxy = build_proxy(upstream, policy_engine=engine)

    # Price every basket name except the first.
    priced = {s: 100.0 for s in core_strategy.BASKET[1:]}
    bars_fetcher = _fake_bars_fetcher(priced)

    async def run():
        async with Client(proxy) as client:
            return await core_strategy.place_basket_orders(
                client, bars_fetcher=bars_fetcher, current_positions={s: 0 for s in core_strategy.BASKET}
            )

    attempts = asyncio.run(run())

    unpriced_symbol = core_strategy.BASKET[0]
    skipped = next(a for a in attempts if a.symbol == unpriced_symbol)
    assert not skipped.forwarded
    assert "skipped" in skipped.detail

    assert unpriced_symbol not in received
    assert set(received) == set(core_strategy.BASKET[1:])


def test_place_basket_orders_includes_scheduled_options_overlay(tmp_path):
    """When include_options_overlay is enabled (default), place_basket_orders
    proposes a protective put on the largest position in addition to stock rebalancing."""
    stock_orders: list[dict] = []
    option_orders: list[dict] = []

    upstream = FastMCP("fake-alpaca")

    @upstream.tool
    def place_stock_order(
        symbol: str,
        side: str,
        qty: str | None = None,
        type: str = "market",
        time_in_force: str = "day",
        limit_price: str | None = None,
    ) -> dict:
        stock_orders.append({"symbol": symbol, "side": side, "qty": qty, "type": type})
        return {"order_id": f"fake-stock-{symbol}", "status": "accepted"}

    @upstream.tool
    def place_option_order(
        symbol: str,
        side: str,
        qty: str | None = None,
        type: str = "market",
        time_in_force: str = "day",
        limit_price: str | None = None,
    ) -> dict:
        option_orders.append({"symbol": symbol, "side": side, "qty": qty, "type": type})
        return {"order_id": f"fake-opt-{symbol}", "status": "accepted"}

    engine, log_path = _real_policy_engine(tmp_path)
    proxy = build_proxy(upstream, policy_engine=engine)

    # Give AAPL higher value so it is the largest position
    prices = {"AAPL": 200.0, "MSFT": 100.0, "SPY": 100.0, "QQQ": 100.0}
    bars_fetcher = _fake_bars_fetcher(prices)

    async def run():
        async with Client(proxy) as client:
            return await core_strategy.place_basket_orders(
                client,
                bars_fetcher=bars_fetcher,
                current_positions={"AAPL": 0, "MSFT": 0, "SPY": 0, "QQQ": 0},
                total_budget_usd=800.0,
                include_options_overlay=True,
            )

    attempts = asyncio.run(run())

    # 4 stock attempts + 1 option attempt = 5 attempts
    assert len(attempts) == 5

    # 4 stock orders
    stock_attempts = [a for a in attempts if a.symbol in core_strategy.BASKET]
    assert len(stock_attempts) == 4
    for a in stock_attempts:
        assert a.forwarded

    # 1 option overlay attempt
    option_attempt = next(a for a in attempts if a.symbol not in core_strategy.BASKET)
    assert option_attempt.qty >= 1
    assert "scheduled options overlay" in option_attempt.detail.lower()


def test_scheduled_options_overlay_description_and_reason_audit():
    """Verify that the scheduled overlay is explicitly framed as standing portfolio
    insurance applied regardless of market conditions, distinct from the reactive hedge."""
    from firewall.rules.hedge_proposal import compute_scheduled_overlay

    positions = {"AAPL": 100, "MSFT": 10}
    prices = {"AAPL": 150.0, "MSFT": 300.0}  # AAPL: $15,000, MSFT: $3,000

    overlay = compute_scheduled_overlay(positions, prices, now=1_700_000_000.0)

    assert overlay is not None
    assert overlay.symbol == "AAPL"
    assert overlay.current_price == 150.0
    assert overlay.strike == 142.50  # 5% OTM
    assert overlay.contracts >= 1
    assert "SCHEDULED OPTIONS OVERLAY" in overlay.reason
    assert "disclosed, scheduled options overlay applied regardless of market conditions" in overlay.reason
    assert "distinct from the reactive CVaR-triggered hedge" in overlay.reason
    assert "Standing portfolio insurance, not a market-timing decision" in overlay.reason

