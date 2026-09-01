import asyncio

from fastmcp import Client, FastMCP

import core_strategy
import run_agent
from firewall import account_data
from firewall.market_data import BarsResult, DailyBar
from firewall.policy import PolicyEngine
from firewall.proxy import build_proxy
from firewall.rules.base import RuleConfig
from firewall.rules.pending_order_exposure import PendingOrderExposureRule


def _rule():
    return PendingOrderExposureRule(RuleConfig.model_validate({
        "id": "pending-order-exposure", "type": "pending_order_exposure",
        "enabled": True, "severity": "hard", "regulation_ref": None,
    }))


def _positions():
    return account_data.PositionsResult(
        ok=True, positions={}, quantities={"AAPL": 0.0},
        current_prices={"AAPL": 10.0}, fetched_at=1.0,
    )


def _orders():
    order = account_data.OpenOrder("prior-80", "AAPL", "buy", 80, 10, 800, "us_equity")
    return account_data.OpenOrdersResult(ok=True, orders=(order,), aggregate_outstanding_notional=800)


def _snapshot():
    snap = account_data.exposure_snapshot(_positions(), _orders())
    snap.update(aggregate_outstanding_notional=800, open_orders=_orders().orders)
    return snap


def _proxy(received):
    upstream = FastMCP("instrumented-upstream")

    @upstream.tool
    def place_stock_order(symbol: str, side: str, qty: str, type: str = "market",
                          time_in_force: str = "day", client_order_id: str | None = None):
        received.append((symbol, side, qty))
        return {"id": "SHOULD-NOT-EXIST", "status": "accepted"}

    return build_proxy(
        upstream, PolicyEngine([_rule()], version="audit"),
        positions_fetcher=_positions,
        open_orders_fetcher=lambda prices: _orders(),
        account_pnl_fetcher=lambda: account_data.AccountPnLResult(
            ok=True, session_pnl_usd=0, equity=1000, account_id="old-account"
        ),
    )


def _bars(*args, **kwargs):
    return BarsResult(ok=True, bars=[DailyBar(close=x, volume=1000) for x in (9, 10, 9.5, 10)])


def test_raw_order_path_blocks_faulty_100_share_stack():
    received = []
    proxy = _proxy(received)
    snap = _snapshot()

    async def run():
        async with Client(proxy) as client:
            return await client.call_tool("place_stock_order", {
                "symbol": "AAPL", "side": "buy", "qty": "100",
                "_firewall_reconciliation": {
                    "target_qty": 100, "snapshot_fingerprint": snap["fingerprint"]
                },
            }, raise_on_error=False)

    result = asyncio.run(run())
    assert result.is_error
    assert "pending-order-exposure" in result.content[0].text
    assert "remaining capacity 20" in result.content[0].text
    assert received == []


def test_direct_core_path_reduces_faulty_100_to_reconciled_20(monkeypatch):
    received = []
    proxy = _proxy(received)
    monkeypatch.setattr(core_strategy, "compute_rebalance_orders", lambda **kwargs: {"AAPL": 100})

    async def run():
        async with Client(proxy) as client:
            return await core_strategy.place_basket_orders(
                client, basket=("AAPL",), total_budget_usd=1000,
                bars_fetcher=_bars, include_options_overlay=False,
                exposure_snapshot_fetcher=lambda prices: _snapshot(),
            )

    attempts = asyncio.run(run())
    assert attempts[0].forwarded is True
    assert attempts[0].qty == 20
    assert received == [("AAPL", "buy", "20")]


def test_run_agent_path_blocks_if_strategy_faultily_proposes_100(monkeypatch, tmp_path):
    received = []
    proxy = _proxy(received)
    monkeypatch.setattr(run_agent, "_default_policy_engine", lambda: PolicyEngine([_rule()], "audit"))
    monkeypatch.setattr(run_agent, "build_proxy", lambda **kwargs: proxy)
    monkeypatch.setattr(run_agent.core_strategy, "BASKET", ("AAPL",))
    monkeypatch.setattr(run_agent.core_strategy, "_read_dynamic_policy_config", lambda engine: {
        "account_equity": 1000, "position_cap_max_pct_of_equity": 1.0,
        "notional_cap_max_usd": 10_000, "order_rate_max_orders": 20,
        "order_rate_window_seconds": 60,
    })
    monkeypatch.setattr(run_agent.core_strategy, "compute_rebalance_orders", lambda **kwargs: {"AAPL": 100})
    monkeypatch.setattr(run_agent, "fetch_daily_bars", _bars)
    monkeypatch.setattr(run_agent.account_data, "fetch_session_pnl", lambda **kwargs:
        account_data.AccountPnLResult(ok=True, session_pnl_usd=0, equity=1000, account_id="old-account"))
    monkeypatch.setattr(
        run_agent.account_data, "fetch_consistent_exposure_snapshot",
        lambda prices, **kwargs: _snapshot(),
    )
    monkeypatch.setattr(run_agent.account_data, "fetch_positions", lambda **kwargs: _positions())
    monkeypatch.setattr(run_agent, "schedule_market_brief_generation", None)
    monkeypatch.setattr(run_agent, "WORKSPACE_ROOT", tmp_path)

    runner = run_agent.HumanReadableCycleRunner(
        budget_override=1000, drift_threshold=0.05, include_options_overlay=False,
        cycle_id="audit-cycle", dry_run=False, lifecycle_poll_attempts=0,
    )
    result = asyncio.run(runner.execute_cycle())
    assert result["blocked_count"] == 1
    assert received == []
    artifact = __import__("json").loads(
        (tmp_path / "data" / "cycles" / "audit-cycle.json").read_text(encoding="utf-8")
    )
    assert artifact["blocked_orders"][0]["rule"] == "pending-order-exposure"
    assert "remaining capacity 20" in artifact["blocked_orders"][0]["reason"]
