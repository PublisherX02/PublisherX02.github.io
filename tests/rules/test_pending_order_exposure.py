import asyncio

from fastmcp import Client, FastMCP

import core_strategy
from firewall import account_data
from firewall.policy import PolicyEngine
from firewall.proxy import build_proxy
from firewall.rules.base import RuleConfig
from firewall.rules.pending_order_exposure import PendingOrderExposureRule
from firewall.market_data import BarsResult, DailyBar


def _rule():
    return PendingOrderExposureRule(RuleConfig.model_validate({
        "id": "pending-order-exposure",
        "type": "pending_order_exposure",
        "enabled": True,
        "severity": "hard",
        "regulation_ref": None,
    }))


def _position_state():
    return account_data.PositionsResult(
        ok=True,
        positions={},
        quantities={"AAPL": 0.0},
        current_prices={"AAPL": 10.0},
        fetched_at=1.0,
    )


def _orders_state():
    order = account_data.OpenOrder(
        "prior-80", "AAPL", "buy", 80.0, 10.0, 800.0, "us_equity"
    )
    return account_data.OpenOrdersResult(
        ok=True, orders=(order,), aggregate_outstanding_notional=800.0
    )


def test_direct_core_strategy_path_sizes_80_committed_to_20_and_rule_blocks_21():
    """Live proxy integration: direct strategy calls cannot bypass the rule."""
    received = []
    upstream = FastMCP("fake-broker")

    @upstream.tool
    def place_stock_order(symbol: str, side: str, qty: str, type: str = "market",
                          time_in_force: str = "day") -> dict:
        received.append((symbol, side, qty))
        return {"id": "accepted-20", "status": "accepted"}

    engine = PolicyEngine([_rule()], version="test")
    proxy = build_proxy(
        upstream,
        engine,
        positions_fetcher=_position_state,
        open_orders_fetcher=lambda prices: _orders_state(),
        account_pnl_fetcher=lambda: account_data.AccountPnLResult(
            ok=True, session_pnl_usd=0.0, equity=1000.0
        ),
    )
    snapshot = account_data.exposure_snapshot(_position_state(), _orders_state())

    def bars(symbol, lookback):
        return BarsResult(ok=True, bars=[
            DailyBar(close=value, volume=1000.0) for value in [9.0, 10.0, 9.5, 10.0]
        ])

    async def run():
        async with Client(proxy) as client:
            attempts = await core_strategy.place_basket_orders(
                client,
                basket=("AAPL",),
                total_budget_usd=1000.0,
                bars_fetcher=bars,
                include_options_overlay=False,
                exposure_snapshot_fetcher=lambda prices: snapshot,
            )
            too_large = await client.call_tool(
                "place_stock_order",
                {
                    "symbol": "AAPL", "side": "buy", "qty": "21",
                    "_firewall_reconciliation": {
                        "target_qty": 100,
                        "snapshot_fingerprint": snapshot["fingerprint"],
                    },
                },
                raise_on_error=False,
            )
            return attempts, too_large

    attempts, too_large = asyncio.run(run())
    assert received == [("AAPL", "buy", "20")]
    assert attempts[0].qty == 20
    assert attempts[0].forwarded is True
    assert too_large.is_error is True
    assert "pending-order-exposure" in too_large.content[0].text
    assert "remaining capacity 20" in too_large.content[0].text


def test_snapshot_protocol_fails_when_positions_change_during_reconciliation():
    states = iter([
        account_data.PositionsResult(ok=True, positions={}, quantities={"AAPL": 0.0}),
        account_data.PositionsResult(ok=True, positions={}, quantities={"AAPL": 1.0}),
    ])
    snapshot = account_data.fetch_consistent_exposure_snapshot(
        {"AAPL": 10.0},
        positions_fetcher=lambda: next(states),
        open_orders_fetcher=lambda prices: _orders_state(),
    )
    assert snapshot["ok"] is False
    assert snapshot["reason"] == "positions changed during open-order reconciliation"
