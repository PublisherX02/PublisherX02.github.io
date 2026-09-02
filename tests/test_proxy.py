"""Tests for the firewall proxy against a fake upstream MCP server."""

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from firewall import account_data
from firewall.market_data import BarsResult, DailyBar
from firewall.audit import (
    AuditEvent,
    AuditLogWriter,
    compute_record_hash,
    find_unresolved_pending,
    verify_chain,
)
from firewall.policy import PolicyEngine
from firewall.proxy import (
    FirewallMiddleware, PaperTradeGuardError, _alpaca_client,
    _normalize_real_upstream_quantity, _parse_order_result, build_proxy,
)
from firewall.rules.base import RuleConfig
from firewall.rules.cvar_gate import CVaRGateRule
from firewall.rules.order_rate_throttle import OrderRateThrottleRule
from fastmcp.tools import ToolResult
from mcp.types import TextContent


def _default_test_bars_fetcher(symbol, lookback_days, **kwargs):
    """Hermetic stand-in for firewall.market_data.fetch_daily_bars -- flat
    $100 closes, real-looking volume. notional_cap/position_cap's
    reference-price fallback (see notional_cap.py's module docstring)
    fetches bars for any qty-only place_stock_order call reaching them; a
    module elsewhere in this suite (core_strategy.py, imported by
    test_core_strategy.py) calls `load_dotenv()` at import time, which
    leaks real Alpaca credentials into this whole pytest PROCESS once that
    module is collected -- so without this default, whether these tests
    hit the real live Alpaca API (or fail closed on missing credentials)
    would depend on test collection order, not on anything these tests
    actually declare. This default makes the outcome independent of that."""
    from firewall.market_data import BarsResult, DailyBar

    return BarsResult(
        ok=True,
        bars=[DailyBar(close=100.0, volume=1_000_000.0) for _ in range(max(lookback_days, 1))],
    )


def _real_policy_engine(tmp_path, bars_fetcher=None):
    """The real, unmodified default.yaml policy, wired to a tmp_path audit
    log -- never the old proof-of-concept hardcoded rule. `bars_fetcher`
    defaults to `_default_test_bars_fetcher` (see its own docstring for
    why); pass an explicit one for a test that cares about the specific
    reference price notional_cap/position_cap's fallback computes, or that
    needs cvar-gate/pct-of-adv to see specific historical data."""
    log_path = tmp_path / "audit.jsonl"
    writer = AuditLogWriter(log_path, session_id="test-session")
    engine = PolicyEngine.from_yaml(
        "policies/default.yaml",
        audit_writer=writer,
        bars_fetcher=bars_fetcher or _default_test_bars_fetcher,
    )
    return engine, log_path


def _read_records(log_path):
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def _real_policy_engine_without_market_data_rules(tmp_path):
    """Same real default.yaml, with cvar-gate and pct-of-adv disabled.
    Needed for tests that exercise a *different* rule's behavior on a
    computable-notional limit order: neither rule has a bars_fetcher
    injection point through build_proxy() (only through constructing a
    Rule directly, which these end-to-end tests don't do), so their real
    network calls run against whatever real/no Alpaca credentials happen to
    be present in the environment -- not hermetic, and not what these
    tests are about. `state["account_equity"]` IS now populated from a
    real GET /v2/account fetch (see FirewallMiddleware._populate_account_
    state and account_data.AccountPnLResult.equity) -- that specific gap
    is closed; the remaining reason to disable these two here is purely
    the missing bars_fetcher injection point. Disabling them here doesn't
    hide anything; it keeps them out of the way of a test that isn't about
    either rule."""
    import yaml

    raw = yaml.safe_load(Path("policies/default.yaml").read_text(encoding="utf-8"))
    for rule in raw["rules"]:
        if rule["id"] in ("cvar-gate", "pct-of-adv"):
            rule["enabled"] = False
    policy_path = tmp_path / "policy_no_market_data_rules.yaml"
    policy_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    log_path = tmp_path / "audit.jsonl"
    writer = AuditLogWriter(log_path, session_id="test-session")
    engine = PolicyEngine.from_yaml(policy_path, audit_writer=writer)
    return engine, log_path


def make_fake_upstream() -> tuple[FastMCP, list[tuple[str, dict]]]:
    """A fake Alpaca-like upstream server that records every call it receives."""
    upstream = FastMCP("fake-alpaca")
    received: list[tuple[str, dict]] = []

    @upstream.tool
    def get_account_info() -> dict:
        received.append(("get_account_info", {}))
        return {"status": "ACTIVE"}

    @upstream.tool
    def close_position(symbol_or_asset_id: str) -> dict:
        received.append(("close_position", {"symbol_or_asset_id": symbol_or_asset_id}))
        return {"closed": symbol_or_asset_id}

    @upstream.tool
    def close_all_positions(cancel_orders: bool = False) -> dict:
        received.append(("close_all_positions", {"cancel_orders": cancel_orders}))
        return {"closed": "all"}

    @upstream.tool
    def place_stock_order(
        symbol: str,
        side: str,
        qty: str | None = None,
        limit_price: str | None = None,
        client_order_id: str | None = None,
    ) -> dict:
        received.append(("place_stock_order", {"symbol": symbol, "side": side}))
        return {"id": "should-not-run", "status": "accepted"}

    return upstream, received


def test_raw_million_contract_option_order_never_reaches_upstream(tmp_path):
    """Exact critical-audit reproduction using the current legs-based schema."""
    upstream = FastMCP("fake-current-alpaca")
    received: list[dict] = []

    @upstream.tool
    def place_option_order(
        legs: list[dict],
        quantity: int,
        order_type: str,
        time_in_force: str,
    ) -> dict:
        received.append(
            {
                "legs": legs,
                "quantity": quantity,
                "order_type": order_type,
                "time_in_force": time_in_force,
            }
        )
        return {"id": "must-never-exist", "status": "accepted"}

    log_path = tmp_path / "audit.jsonl"
    engine = PolicyEngine(
        rules=[],
        version="option-disable-test",
        audit_writer=AuditLogWriter(log_path, session_id="option-disable-test"),
    )
    proxy = build_proxy(
        upstream,
        policy_engine=engine,
        account_pnl_fetcher=lambda: account_data.AccountPnLResult(
            ok=True, session_pnl_usd=0.0, equity=100_000.0
        ),
    )
    payload = {
        "legs": [
            {"symbol": "AAPL261218C00300000", "side": "buy", "ratio_qty": 1}
        ],
        "quantity": 1_000_000,
        "order_type": "market",
        "time_in_force": "day",
    }

    async def run():
        async with Client(proxy) as client:
            return await client.call_tool(
                "place_option_order", payload, raise_on_error=False
            )

    result = asyncio.run(run())

    assert result.is_error
    assert "option-orders-disabled" in result.content[0].text
    assert received == []
    records = _read_records(log_path)
    block_records = [r for r in records if r["rule_id"] == "option-orders-disabled"]
    assert len(block_records) == 1
    assert block_records[0]["forwarded"] is False
    assert block_records[0]["upstream_status"] == "not_forwarded"


def test_dry_run_suppresses_mutation_at_proxy_boundary(tmp_path):
    async def _runner():
        upstream, received = make_fake_upstream()
        engine, log_path = _real_policy_engine(tmp_path)
        proxy = build_proxy(
            upstream,
            engine,
            dry_run=True,
            account_pnl_fetcher=lambda: account_data.AccountPnLResult(
                ok=True, session_pnl_usd=0.0, equity=100_000.0
            ),
            positions_fetcher=lambda: account_data.PositionsResult(
                ok=True, positions={}, fetched_at=0.0
            ),
        )
        async with Client(proxy) as client:
            result = await client.call_tool(
                "place_stock_order",
                {
                    "symbol": "AAPL", "side": "buy", "qty": "1",
                    "limit_price": "100", "client_order_id": "dry-client-1",
                },
                raise_on_error=False,
            )
        assert received == []
        payload = json.loads(result.content[0].text)
        assert json.loads(payload["result"])["status"] == "dry_run"
        records = _read_records(log_path)
        assert records[-1]["forwarded"] is False
        assert records[-1]["upstream_status"] == "not_forwarded"

    asyncio.run(_runner())


def test_dry_run_surfaces_deleveraging_exception_note(tmp_path):
    async def _runner():
        upstream, received = make_fake_upstream()
        engine, _ = _real_policy_engine(tmp_path)
        proxy = build_proxy(
            upstream,
            engine,
            dry_run=True,
            account_pnl_fetcher=lambda: account_data.AccountPnLResult(
                ok=True, session_pnl_usd=-1_500.0, equity=100_000.0
            ),
            positions_fetcher=lambda: account_data.PositionsResult(
                ok=True,
                positions={"AAPL": 1_000.0},
                quantities={"AAPL": 10.0},
                current_prices={"AAPL": 100.0},
                fetched_at=time.time(),
            ),
            open_orders_fetcher=lambda prices: account_data.OpenOrdersResult(
                ok=True, orders=(), aggregate_outstanding_notional=0.0
            ),
        )
        async with Client(proxy) as client:
            result = await client.call_tool(
                "place_stock_order",
                {
                    "symbol": "AAPL",
                    "side": "sell",
                    "qty": "5",
                    "client_order_id": "dry-deleveraging-1",
                },
                raise_on_error=False,
            )

        assert not result.is_error
        assert received == []
        notes = [getattr(item, "text", "") for item in result.content[1:]]
        assert notes == [
            "DELEVERAGING_EXCEPTION_ALLOW: drawdown killswitch is tripped; "
            "plain-equity sell for AAPL is provably exposure-reducing "
            "(broker_confirmed_held_qty=10, order_qty=5)"
        ]

    asyncio.run(_runner())


def test_parallel_mutations_are_serialized_through_history_update():
    upstream = FastMCP("blocking-upstream")
    started = asyncio.Event()
    release = asyncio.Event()
    received: list[str] = []

    @upstream.tool
    async def place_stock_order(symbol: str, side: str, qty: str) -> dict:
        received.append(symbol)
        started.set()
        await release.wait()
        return {"data": {"result": {"id": f"order-{symbol}", "status": "accepted"}}}

    rule = OrderRateThrottleRule(RuleConfig.model_validate({
        "id": "order-rate-throttle", "type": "order_rate_throttle",
        "enabled": True, "severity": "hard", "regulation_ref": None, "max_orders": 1,
        "window_seconds": 60, "place_tool_match": ["place_stock_order"],
    }))
    proxy = build_proxy(
        upstream, PolicyEngine([rule], version="concurrency-test"),
        account_pnl_fetcher=lambda: account_data.AccountPnLResult(
            ok=True, session_pnl_usd=0.0, equity=100_000.0
        ),
    )

    async def run():
        async with Client(proxy) as client:
            first = asyncio.create_task(client.call_tool(
                "place_stock_order", {"symbol": "AAPL", "side": "buy", "qty": "1"},
                raise_on_error=False,
            ))
            await started.wait()
            second = asyncio.create_task(client.call_tool(
                "place_stock_order", {"symbol": "MSFT", "side": "buy", "qty": "1"},
                raise_on_error=False,
            ))
            await asyncio.sleep(0.05)
            assert not second.done()
            release.set()
            return await first, await second

    first, second = asyncio.run(run())
    assert not first.is_error
    assert second.is_error
    assert "order-rate-throttle" in second.content[0].text
    assert received == ["AAPL"]


def test_concurrent_same_symbol_inside_multi_order_cycle_is_capped_without_fingerprint_self_block(
    tmp_path,
):
    """One scenario reproducing the concurrency, fingerprint, and TTL windows."""
    import yaml

    raw = yaml.safe_load(Path("policies/default.yaml").read_text(encoding="utf-8"))
    for rule in raw["rules"]:
        if rule["id"] == "position-cap-per-symbol":
            rule["max_usd_per_symbol"] = 10_000
            rule["max_pct_of_equity"] = None
        elif rule["id"] == "notional-cap-single-order":
            rule["max_usd"] = 10_000
            rule["max_pct_of_equity"] = None
        elif rule["id"] in {"cvar-gate", "pct-of-adv"}:
            rule["enabled"] = False
    policy_path = tmp_path / "integrated_concurrency_policy.yaml"
    policy_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    log_path = tmp_path / "audit.jsonl"
    engine = PolicyEngine.from_yaml(
        policy_path,
        audit_writer=AuditLogWriter(log_path, session_id="integrated-concurrency"),
        bars_fetcher=_default_test_bars_fetcher,
    )

    upstream = FastMCP("timed-multi-order-broker")
    first_aapl_started = asyncio.Event()
    timeline: list[dict] = []
    accepted: list[tuple[str, float]] = []
    position_reads: list[float] = []

    def stamp(event: str, **fields):
        timeline.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "monotonic": round(time.monotonic(), 6),
            "event": event,
            **fields,
        })

    @upstream.tool
    async def place_stock_order(symbol: str, side: str, qty: str) -> dict:
        numeric_qty = float(qty)
        stamp("upstream_enter", symbol=symbol, qty=numeric_qty)
        if symbol == "AAPL" and not first_aapl_started.is_set():
            first_aapl_started.set()
            # Keep the first broker call open while the other cycle calls
            # are genuinely dispatched as concurrent asyncio tasks.
            await asyncio.sleep(0.15)
        accepted.append((symbol, numeric_qty))
        stamp("upstream_accept", symbol=symbol, qty=numeric_qty)
        return {"data": {"order": {
            "id": f"accepted-{symbol}-{numeric_qty:g}", "status": "accepted"
        }}}

    def positions_fetcher():
        position_reads.append(time.monotonic())
        stamp("position_read", read=len(position_reads))
        return account_data.PositionsResult(
            ok=True,
            positions={},
            quantities={"AAPL": 0.0, "MSFT": 0.0, "SPY": 0.0},
            current_prices={"AAPL": 100.0, "MSFT": 100.0, "SPY": 100.0},
            fetched_at=time.time(),
        )

    proxy = build_proxy(
        upstream,
        engine,
        positions_fetcher=positions_fetcher,
        open_orders_fetcher=lambda prices: account_data.OpenOrdersResult(
            ok=True, orders=(), aggregate_outstanding_notional=0.0
        ),
        account_pnl_fetcher=lambda: account_data.AccountPnLResult(
            ok=True, session_pnl_usd=0.0, equity=100_000.0
        ),
    )
    fingerprint = "one-cycle-start-fingerprint"

    def args(symbol: str, qty: int, target: int) -> dict:
        return {
            "symbol": symbol, "side": "buy", "qty": str(qty),
            "_firewall_reconciliation": {
                "target_qty": target, "snapshot_fingerprint": fingerprint,
            },
        }

    async def run_cycle():
        async with Client(proxy) as client:
            first = asyncio.create_task(client.call_tool(
                "place_stock_order", args("AAPL", 60, 100), raise_on_error=False
            ), name="cycle-aapl-60")
            await first_aapl_started.wait()
            stamp("parallel_dispatch", calls=["AAPL-50", "MSFT-10", "SPY-10"])
            rest = [
                asyncio.create_task(client.call_tool(
                    "place_stock_order", args("AAPL", 50, 100), raise_on_error=False
                ), name="cycle-aapl-50"),
                asyncio.create_task(client.call_tool(
                    "place_stock_order", args("MSFT", 10, 10), raise_on_error=False
                ), name="cycle-msft-10"),
                asyncio.create_task(client.call_tool(
                    "place_stock_order", args("SPY", 10, 10), raise_on_error=False
                ), name="cycle-spy-10"),
            ]
            return [await first, *(await asyncio.gather(*rest))]

    results = asyncio.run(run_cycle())
    labels = ["AAPL-60", "AAPL-50", "MSFT-10", "SPY-10"]
    outcomes = {
        label: {
            "blocked": result.is_error,
            "detail": result.content[0].text,
        }
        for label, result in zip(labels, results)
    }
    accepted_aapl_notional = sum(qty * 100.0 for symbol, qty in accepted if symbol == "AAPL")
    window_seconds = max(position_reads) - min(position_reads)

    print("INTEGRATED_SCENARIO_TIMELINE=" + json.dumps(timeline, indent=2))
    print("INTEGRATED_SCENARIO_OUTCOMES=" + json.dumps(outcomes, indent=2))
    print("INTEGRATED_SCENARIO_SUMMARY=" + json.dumps({
        "accepted": accepted,
        "accepted_aapl_notional": accepted_aapl_notional,
        "position_cap_usd": 10_000.0,
        "position_read_window_seconds": round(window_seconds, 6),
        "configured_cache_ttl_seconds": 5.0,
    }, indent=2))

    assert not outcomes["AAPL-60"]["blocked"]
    assert outcomes["AAPL-50"]["blocked"]
    assert "position-cap-per-symbol" in outcomes["AAPL-50"]["detail"]
    assert "$11,000.00" in outcomes["AAPL-50"]["detail"]
    assert not outcomes["MSFT-10"]["blocked"]
    assert not outcomes["SPY-10"]["blocked"]
    assert accepted_aapl_notional == 6_000.0
    assert accepted_aapl_notional <= 10_000.0
    assert window_seconds < 5.0


def test_live_reconciliation_reader_forces_zero_ttl(monkeypatch):
    cache_ttls: list[float] = []

    def fake_positions(**kwargs):
        cache_ttls.append(kwargs["cache_ttl_seconds"])
        return account_data.PositionsResult(
            ok=True, positions={}, quantities={"AAPL": 0.0},
            current_prices={"AAPL": 100.0}, fetched_at=1.0,
        )

    monkeypatch.setattr(account_data, "fetch_positions", fake_positions)
    middleware = FirewallMiddleware(
        PolicyEngine([], version="fresh-reader-test"),
        open_orders_fetcher=lambda prices: account_data.OpenOrdersResult(
            ok=True, orders=(), aggregate_outstanding_notional=0.0
        ),
        is_real_upstream=True,
    )
    snapshot = account_data.fetch_consistent_exposure_snapshot(
        {}, positions_fetcher=middleware._fresh_positions_fetcher,
        open_orders_fetcher=middleware._open_orders_fetcher,
    )
    assert snapshot["ok"] is True
    assert cache_ttls == [0, 0]


def test_tripped_drawdown_deleveraging_exception_six_scenario_reproduction(tmp_path):
    upstream = FastMCP("drawdown-deleveraging-broker")
    accepted: list[dict] = []
    timeline: list[dict] = []
    position_reads: list[str] = []

    def stamp(event: str, **fields):
        timeline.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        })

    @upstream.tool
    def place_stock_order(symbol: str, side: str, qty: str) -> dict:
        order = {"symbol": symbol, "side": side, "qty": float(qty)}
        accepted.append(order)
        stamp("upstream_accept", **order)
        return {"order": {"id": f"accepted-{len(accepted)}", "status": "accepted"}}

    @upstream.tool
    def place_option_order(symbol: str, side: str, qty: str) -> dict:
        raise AssertionError("option deleveraging exception must never reach upstream")

    log_path = tmp_path / "drawdown_deleveraging_audit.jsonl"
    engine = PolicyEngine.from_yaml(
        "policies/default.yaml",
        audit_writer=AuditLogWriter(log_path, session_id="drawdown-deleveraging"),
        bars_fetcher=_default_test_bars_fetcher,
    )

    def positions_fetcher():
        timestamp = datetime.now(timezone.utc).isoformat()
        position_reads.append(timestamp)
        stamp("fresh_position_read", read=len(position_reads))
        return account_data.PositionsResult(
            ok=True,
            positions={"AAPL": 1_000.0},
            quantities={"AAPL": 10.0},
            current_prices={"AAPL": 100.0},
            fetched_at=time.time(),
        )

    proxy = build_proxy(
        upstream,
        engine,
        positions_fetcher=positions_fetcher,
        open_orders_fetcher=lambda prices: account_data.OpenOrdersResult(
            ok=True, orders=(), aggregate_outstanding_notional=0.0
        ),
        account_pnl_fetcher=lambda: account_data.AccountPnLResult(
            ok=True, session_pnl_usd=-1_500.0, equity=100_000.0
        ),
    )

    scenarios = [
        ("sell_exact_holding", "place_stock_order",
         {"symbol": "AAPL", "side": "sell", "qty": "10"}),
        ("sell_flip_short", "place_stock_order",
         {"symbol": "AAPL", "side": "sell", "qty": "11"}),
        ("sell_zero_holding", "place_stock_order",
         {"symbol": "MSFT", "side": "sell", "qty": "1"}),
        ("option_sell", "place_option_order",
         {"symbol": "AAPL260918C00100000", "side": "sell", "qty": "1"}),
        ("buy_while_tripped", "place_stock_order",
         {"symbol": "AAPL", "side": "buy", "qty": "1"}),
    ]

    async def run():
        verdicts = {}
        async with Client(proxy) as client:
            for label, tool, arguments in scenarios:
                stamp("attempt", scenario=label, tool=tool, arguments=arguments)
                result = await client.call_tool(tool, arguments, raise_on_error=False)
                verdicts[label] = {
                    "blocked": result.is_error,
                    "detail": result.content[0].text,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
        return verdicts

    verdicts = asyncio.run(run())
    audit_records = _read_records(log_path)
    exception_records = [
        record for record in audit_records
        if record["verdict"] == "info"
        and "DELEVERAGING_EXCEPTION_ALLOW" in record["reason"]
    ]
    freshness = {
        "position_read_count": len(position_reads),
        "reader": "explicit uncached fetcher used by _fresh_positions_fetcher",
        "production_cache_ttl_assertion": "covered by test_live_reconciliation_reader_forces_zero_ttl",
        "first_read": position_reads[0],
        "last_read": position_reads[-1],
    }
    print("DELEVERAGING_TIMELINE=" + json.dumps(timeline, indent=2))
    print("DELEVERAGING_VERDICTS=" + json.dumps(verdicts, indent=2))
    print("DELEVERAGING_EXCEPTION_AUDIT=" + json.dumps(exception_records, indent=2))
    print("DELEVERAGING_FRESHNESS=" + json.dumps(freshness, indent=2))

    assert not verdicts["sell_exact_holding"]["blocked"]
    assert verdicts["sell_flip_short"]["blocked"]
    assert verdicts["sell_zero_holding"]["blocked"]
    assert verdicts["option_sell"]["blocked"]
    assert "option-orders-disabled" in verdicts["option_sell"]["detail"]
    assert verdicts["buy_while_tripped"]["blocked"]
    assert accepted == [{"symbol": "AAPL", "side": "sell", "qty": 10.0}]
    assert len(exception_records) == 1
    assert "broker_confirmed_held_qty=10" in exception_records[0]["reason"]
    assert "order_qty=10" in exception_records[0]["reason"]
    # Four plain-stock scenarios each perform the existing positions read
    # plus two zero-TTL reconciliation reads. The option-shaped call performs
    # the common pre-policy positions read, then hard-blocks before snapshot
    # reconciliation or upstream submission: 4*3 + 1 = 13.
    assert len(position_reads) == 13


def test_nested_broker_order_result_is_parsed_recursively():
    result = ToolResult(content=[TextContent(
        type="text",
        text=json.dumps({"data": {"result": json.dumps({
            "order": {"id": "nested-123", "status": "filled"}
        })}}),
    )])
    assert _parse_order_result(result) == ("nested-123", "filled")


def test_fractional_quantity_normalization_is_exact():
    arguments = {"qty": "0.75"}
    _normalize_real_upstream_quantity(arguments)
    assert arguments["quantity"] == 0.75


def test_normal_call_is_forwarded_upstream(tmp_path):
    upstream, received = make_fake_upstream()
    engine, _ = _real_policy_engine(tmp_path)
    proxy = build_proxy(upstream, policy_engine=engine)

    async def run():
        async with Client(proxy) as client:
            return await client.call_tool("get_account_info", {})

    result = asyncio.run(run())

    assert not result.is_error
    assert received == [("get_account_info", {})]


def test_close_all_positions_is_blocked_and_never_reaches_upstream(tmp_path):
    """close_all_positions is hard-blocked by the real blast_radius rule in
    policies/default.yaml (bulk tools require human approval) -- not by any
    hardcoded proof-of-concept check."""
    upstream, received = make_fake_upstream()
    engine, _ = _real_policy_engine(tmp_path)
    proxy = build_proxy(upstream, policy_engine=engine)

    async def run():
        async with Client(proxy) as client:
            return await client.call_tool(
                "close_all_positions", {}, raise_on_error=False
            )

    result = asyncio.run(run())

    assert result.is_error
    assert "human approval" in result.content[0].text
    assert received == []


def test_close_single_position_is_hard_blocked_by_catchall_until_a_dedicated_rule_exists(
    tmp_path,
):
    """close_position is a real Alpaca action tool that no rule in
    default.yaml pattern-matches (its tool_match substrings are "order",
    "cancel_all", "close_all", "liquidate" -- none match "close_position").
    Conformance-audit finding A2 flagged this as one of 10 real, currently-
    uncovered action tools; unrecognized-tool-catchall (policies/default.yaml,
    last rule) now hard-blocks it by design until a dedicated rule is built,
    rather than letting it through as an unconditional allow."""
    upstream, received = make_fake_upstream()
    engine, log_path = _real_policy_engine(tmp_path)
    proxy = build_proxy(upstream, policy_engine=engine)

    async def run():
        async with Client(proxy) as client:
            return await client.call_tool(
                "close_position", {"symbol_or_asset_id": "AAPL"}, raise_on_error=False
            )

    result = asyncio.run(run())

    assert result.is_error
    assert received == []

    records = _read_records(log_path)
    assert len(records) == 1
    assert records[0]["verdict"] == "hard_block"
    assert records[0]["rule_id"] == "unrecognized-tool-catchall"
    assert records[0]["forwarded"] is False


def test_unrecognized_tool_name_with_large_money_shaped_argument_is_hard_blocked(
    tmp_path,
):
    """Reproduces the conformance audit's A2 probe: a tool name that matches
    no rule's tool_match pattern, carrying a huge dollar-shaped argument.
    Before unrecognized-tool-catchall existed, this was forwarded upstream
    verbatim with verdict "allow" / reason "no rule triggered" -- see
    AUDIT.md section A2. It must now hard_block and never reach upstream."""
    upstream = FastMCP("fake-alpaca-with-unknown-tool")
    received: list[tuple[str, dict]] = []

    @upstream.tool
    def zzz_never_seen_tool_xyz(amount_usd: float = 0.0) -> dict:
        received.append(("zzz_never_seen_tool_xyz", {"amount_usd": amount_usd}))
        return {"ok": True, "amount_usd": amount_usd}

    engine, log_path = _real_policy_engine(tmp_path)
    proxy = build_proxy(upstream, policy_engine=engine)

    async def run():
        async with Client(proxy) as client:
            return await client.call_tool(
                "zzz_never_seen_tool_xyz",
                {"amount_usd": 999999999.0},
                raise_on_error=False,
            )

    result = asyncio.run(run())

    assert result.is_error
    assert received == []  # never reached the fake upstream

    records = _read_records(log_path)
    assert len(records) == 1
    assert records[0]["verdict"] == "hard_block"
    assert records[0]["rule_id"] == "unrecognized-tool-catchall"
    assert records[0]["forwarded"] is False
    assert records[0]["upstream_status"] == "not_forwarded"
    assert "unrecognized tool" in records[0]["reason"]


def test_read_only_whitelisted_tool_is_not_touched_by_catchall(tmp_path):
    """A real, whitelisted read-only tool that no other rule's tool_match
    touches either (e.g. get_clock) must still be allowed -- the catchall
    must not turn into an accidental default-deny-everything rule."""
    upstream = FastMCP("fake-alpaca")
    received: list[tuple[str, dict]] = []

    @upstream.tool
    def get_clock() -> dict:
        received.append(("get_clock", {}))
        return {"is_open": True}

    engine, log_path = _real_policy_engine(tmp_path)
    proxy = build_proxy(upstream, policy_engine=engine)

    async def run():
        async with Client(proxy) as client:
            return await client.call_tool("get_clock", {})

    result = asyncio.run(run())

    assert not result.is_error
    assert received == [("get_clock", {})]

    records = _read_records(log_path)
    assert len(records) == 2  # pending, then outcome
    assert records[0]["upstream_status"] == "pending"
    assert records[1]["verdict"] == "allow"
    assert records[1]["upstream_status"] == "ok"


# --- policy engine integration: hard_block / allow both write exactly one
# audit record, and a hard_block never reaches upstream -------------------


def test_hard_block_via_real_policy_rule_never_reaches_upstream_with_one_audit_record(
    tmp_path,
):
    upstream, received = make_fake_upstream()
    engine, log_path = _real_policy_engine(tmp_path)
    proxy = build_proxy(upstream, policy_engine=engine)

    async def run():
        async with Client(proxy) as client:
            return await client.call_tool(
                "close_all_positions", {}, raise_on_error=False
            )

    result = asyncio.run(run())

    assert result.is_error
    assert received == []

    records = _read_records(log_path)
    assert len(records) == 1
    assert records[0]["verdict"] == "hard_block"
    assert records[0]["forwarded"] is False
    assert records[0]["upstream_status"] == "not_forwarded"
    assert records[0]["rule_id"] == "blast-radius-bulk-actions"


def test_allow_via_real_policy_forwards_and_writes_a_linked_pending_outcome_pair(tmp_path):
    """An allowed call now writes two records, not one: a "pending" record
    before forwarding, and a linked "outcome" record after -- see
    FirewallMiddleware.on_call_tool and PolicyEngine.record_call_pending.
    This is what makes the audit trail crash-safe: a process death between
    the two writes still leaves the pending record as proof the call was
    attempted (see test_process_crash_between_pending_write_and_call_next_
    leaves_pending_record_alone below)."""
    upstream, received = make_fake_upstream()
    engine, log_path = _real_policy_engine(tmp_path)
    proxy = build_proxy(upstream, policy_engine=engine)

    async def run():
        async with Client(proxy) as client:
            return await client.call_tool("get_account_info", {})

    result = asyncio.run(run())

    assert not result.is_error
    assert received == [("get_account_info", {})]

    records = _read_records(log_path)
    assert len(records) == 2
    pending_record, outcome_record = records

    assert pending_record["verdict"] == "allow"
    assert pending_record["forwarded"] is None
    assert pending_record["upstream_status"] == "pending"
    assert pending_record["call_id"] is not None

    assert outcome_record["verdict"] == "allow"
    assert outcome_record["forwarded"] is True
    assert outcome_record["upstream_status"] == "ok"
    assert outcome_record["call_id"] == pending_record["call_id"]

    assert outcome_record["pending_hash"] == compute_record_hash(
        AuditEvent.model_validate(pending_record)
    )

    ok, bad_index = verify_chain(log_path)
    assert ok is True
    assert bad_index is None


def test_upstream_error_after_allow_is_still_reported_and_logged(tmp_path):
    """An allowed call that then errors upstream must still be logged as
    forwarded=True/upstream_status=error on its outcome record -- not
    silently dropped -- alongside the pending record written before the
    (failed) forwarding attempt."""
    upstream = FastMCP("fake-alpaca")

    @upstream.tool
    def get_account_info() -> dict:
        raise RuntimeError("upstream boom")

    engine, log_path = _real_policy_engine(tmp_path)
    proxy = build_proxy(upstream, policy_engine=engine)

    async def run():
        async with Client(proxy) as client:
            return await client.call_tool("get_account_info", {}, raise_on_error=False)

    result = asyncio.run(run())

    assert result.is_error

    records = _read_records(log_path)
    assert len(records) == 2
    pending_record, outcome_record = records

    assert pending_record["upstream_status"] == "pending"

    assert outcome_record["verdict"] == "allow"
    assert outcome_record["forwarded"] is True
    assert outcome_record["upstream_status"] == "error"
    assert outcome_record["call_id"] == pending_record["call_id"]


def test_build_proxy_uses_real_default_policy_when_none_given(tmp_path, monkeypatch):
    """The production default: no policy_engine passed in means build_proxy
    constructs the real PolicyEngine from policies/default.yaml, wired to a
    real AuditLogWriter -- not the old no-op hardcoded rule."""
    monkeypatch.setenv("FIREWALL_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    upstream, received = make_fake_upstream()
    proxy = build_proxy(upstream)

    async def run():
        async with Client(proxy) as client:
            return await client.call_tool("get_account_info", {})

    result = asyncio.run(run())

    assert not result.is_error
    assert received == [("get_account_info", {})]
    assert (tmp_path / "audit.jsonl").exists()
    records = _read_records(tmp_path / "audit.jsonl")
    assert len(records) == 2
    assert records[0]["upstream_status"] == "pending"
    assert records[1]["verdict"] == "allow"


# --- paper-trading startup guard: ALPACA_PAPER_TRADE (hard) and the
# ALPACA_API_KEY prefix heuristic (soft, audit-logged only) -----------------


@pytest.mark.parametrize(
    "bad_value", [None, "false", "False", "0", "no", "", "live", "garbage"]
)
def test_paper_trade_guard_refuses_to_start_on_non_paper_value(tmp_path, monkeypatch, bad_value):
    """Any ALPACA_PAPER_TRADE value outside {"true","1","yes"} (case-
    insensitive) is exactly the set of values that make alpaca-mcp-server's
    own _get_trading_base_url() resolve to the LIVE base URL -- so this
    must hard-refuse regardless of what ALPACA_API_KEY looks like. Unset
    (None) is included here deliberately: this guard does not rely on
    alpaca-mcp-server's own "unset defaults to paper" behavior holding --
    it requires an explicit paper value every time."""
    if bad_value is None:
        monkeypatch.delenv("ALPACA_PAPER_TRADE", raising=False)
    else:
        monkeypatch.setenv("ALPACA_PAPER_TRADE", bad_value)
    monkeypatch.setenv("ALPACA_API_KEY", "PK5XVNGP4R05QXHZM99Y")  # even a paper-shaped key
    engine, log_path = _real_policy_engine(tmp_path)

    with pytest.raises(PaperTradeGuardError, match="ALPACA_PAPER_TRADE"):
        _alpaca_client(engine)

    # The hard guard fires before anything is logged.
    assert not log_path.exists() or _read_records(log_path) == []


@pytest.mark.parametrize("good_value", ["true", "TRUE", "True", "1", "yes", "YES"])
def test_paper_trade_guard_allows_start_on_paper_value(tmp_path, monkeypatch, good_value):
    monkeypatch.setenv("ALPACA_PAPER_TRADE", good_value)
    monkeypatch.setenv("ALPACA_API_KEY", "PK5XVNGP4R05QXHZM99Y")
    engine, log_path = _real_policy_engine(tmp_path)

    client = _alpaca_client(engine)

    assert isinstance(client, Client)
    assert not log_path.exists() or _read_records(log_path) == []


def test_paper_key_prefix_heuristic_warns_but_does_not_block_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER_TRADE", "true")
    monkeypatch.setenv("ALPACA_API_KEY", "AKLIVESHAPEDKEY123")  # does not start with "PK"
    engine, log_path = _real_policy_engine(tmp_path)

    client = _alpaca_client(engine)

    assert isinstance(client, Client)
    records = _read_records(log_path)
    assert len(records) == 1
    assert records[0]["verdict"] == "soft_block"
    assert records[0]["rule_id"] == "paper_key_format_heuristic"
    assert records[0]["forwarded"] is False
    assert records[0]["upstream_status"] == "not_forwarded"


def test_paper_key_prefix_heuristic_silent_when_key_matches(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER_TRADE", "true")
    monkeypatch.setenv("ALPACA_API_KEY", "PK5XVNGP4R05QXHZM99Y")
    engine, log_path = _real_policy_engine(tmp_path)

    client = _alpaca_client(engine)

    assert isinstance(client, Client)
    assert not log_path.exists() or _read_records(log_path) == []


# --- order_history wiring: reactivates order_rate_throttle/
# wash_trade_detector/place_cancel_ratio/layering_detector, which previously
# read a permanently-empty order_history no matter how many orders were
# placed (AUDIT.md findings E3/E4) ------------------------------------------


def test_e4_reactivation_25_rapid_place_orders_trip_the_rate_throttle(tmp_path):
    """Directly reproduces AUDIT.md's own E4 probe: 25 rapid
    place_stock_order calls through the full proxy against the real,
    unmodified default.yaml policy (order-rate-throttle: max_orders=20,
    window_seconds=60).

    Before order_history was wired into FirewallMiddleware.on_call_tool,
    OrderHistory.record() was never called anywhere in src/ -- order_history
    stayed empty for the life of the process regardless of how many orders
    were placed, so order_rate_throttle's `len(recent_place_ids) + 1 >
    max_orders` check always saw 0 recent orders. AUDIT.md recorded the
    result of running exactly this probe: "25 rapid orders -> 0 blocked by
    rate throttle". With order_history now populated from each call's real
    outcome, the throttle must see the first `max_orders` calls accumulate
    and hard-block at least one of the remaining five."""
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
        return {"order_id": "fake-order", "status": "accepted"}

    engine, log_path = _real_policy_engine(tmp_path)
    proxy = build_proxy(
        upstream,
        policy_engine=engine,
        account_pnl_fetcher=lambda: account_data.AccountPnLResult(
            ok=True, session_pnl_usd=0.0, equity=100_000.0
        ),
    )

    async def run():
        blocked = 0
        async with Client(proxy) as client:
            for _ in range(25):
                result = await client.call_tool(
                    "place_stock_order",
                    {
                        "symbol": "AAPL",
                        "side": "buy",
                        "qty": "1",
                        "type": "market",
                        "time_in_force": "day",
                        "limit_price": "100.00",
                    },
                    raise_on_error=False,
                )
                if result.is_error:
                    blocked += 1
        return blocked

    blocked_count = asyncio.run(run())

    assert blocked_count > 0, (
        "order_rate_throttle should hard-block at least one of 25 rapid "
        "place_stock_order calls (max_orders=20/window_seconds=60 in "
        "policies/default.yaml) now that order_history is populated -- "
        "this is the exact scenario AUDIT.md's E4 found 0/25 blocked on"
    )

    records = _read_records(log_path)
    rate_throttle_blocks = [
        r
        for r in records
        if r["verdict"] == "hard_block" and r["rule_id"] == "order-rate-throttle"
    ]
    assert len(rate_throttle_blocks) > 0


def test_hard_blocked_place_order_attempts_are_recorded_in_order_history(tmp_path):
    """Explicit coverage of the design decision made in
    FirewallMiddleware._track_order_lifecycle: a hard-blocked place_*
    attempt is recorded into order_history (outcome="blocked"), not
    excluded -- so a rapid sequence of blocked retries still consumes
    order_rate_throttle's rate budget. Symbol_allowlist (not
    order_rate_throttle) does the blocking here, to isolate the behavior
    from the throttle itself."""
    upstream = FastMCP("fake-alpaca")

    @upstream.tool
    def place_stock_order(symbol: str, side: str, qty: str | None = None) -> dict:
        return {"order_id": "should-not-be-reached", "status": "accepted"}

    engine, _ = _real_policy_engine(tmp_path)
    middleware = FirewallMiddleware(engine)

    context = SimpleNamespace(
        message=SimpleNamespace(
            name="place_stock_order",
            arguments={"symbol": "NOTALLOWED", "side": "buy", "qty": "1", "limit_price": "100.00"},
        )
    )

    async def call_next_should_not_run(_context):
        raise AssertionError("hard-blocked call must never reach call_next")

    async def run():
        await middleware.on_call_tool(context, call_next_should_not_run)

    with pytest.raises(ToolError, match="symbol-allowlist"):
        asyncio.run(run())

    events = list(middleware._order_history)
    assert len(events) == 1
    assert events[0].outcome == "blocked"
    assert events[0].symbol == "NOTALLOWED"
    assert events[0].side == "buy"
    assert events[0].qty == 1.0


# --- A4: string-typed qty/limit_price must be parsed, not silently
# unassessable (AUDIT.md finding A4) ----------------------------------------


def test_a4_string_typed_qty_and_price_over_notional_cap_hard_blocks(tmp_path):
    """Directly reproduces AUDIT.md's own A4 probe end-to-end: a
    place_stock_order call shaped exactly like Alpaca's real input schema
    (qty/limit_price typed as JSON strings, verified against the live
    inputSchema) with qty="100000", limit_price="500.00" -- true notional
    $50,000,000 -- against the real, unmodified default.yaml's $5,000
    notional_cap and $20,000 position_cap.

    Before the fix, firewall.rules._util._as_number() accepted only
    int/float and returned None for str, so extract_notional() returned
    None for every real place_stock_order call; AUDIT.md recorded the
    result of running exactly this call through build_proxy() with the
    real policy: is_error=False -- accepted. This must now hard-block."""
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
        return {"order_id": "should-not-be-reached", "status": "accepted"}

    engine, log_path = _real_policy_engine(tmp_path)
    proxy = build_proxy(upstream, policy_engine=engine)

    async def run():
        async with Client(proxy) as client:
            return await client.call_tool(
                "place_stock_order",
                {
                    "symbol": "AAPL",
                    "side": "buy",
                    "qty": "100000",
                    "type": "limit",
                    "time_in_force": "day",
                    "limit_price": "500.00",
                },
                raise_on_error=False,
            )

    result = asyncio.run(run())

    assert result.is_error, (
        "place_stock_order with string-typed qty=\"100000\", "
        "limit_price=\"500.00\" (true notional $50,000,000) must hard-block "
        "against the real $5,000 notional_cap -- this is the exact "
        "live-execution shape AUDIT.md's A4 finding used"
    )
    assert "notional-cap-single-order" in result.content[0].text
    assert "50,000,000.00" in result.content[0].text

    records = _read_records(log_path)
    notional_blocks = [
        r
        for r in records
        if r["verdict"] == "hard_block" and r["rule_id"] == "notional-cap-single-order"
    ]
    assert len(notional_blocks) == 1


# --- session_pnl_usd: sourced from Alpaca's own account endpoint, not
# computed locally (AUDIT.md E3/E4 follow-up decision) -----------------------


def test_session_pnl_fetch_reactivates_drawdown_killswitch(tmp_path):
    """default.yaml's drawdown-killswitch has session_pnl_threshold_usd:
    -1000. A fake account_pnl_fetcher reporting a -5000 session loss must
    trip it and hard-block the next place_stock_order -- proving
    session_pnl_usd now reaches the rule from a real (faked) account
    fetch, not just from a test manually stuffing state."""
    upstream = FastMCP("fake-alpaca")

    @upstream.tool
    def place_stock_order(symbol: str, side: str, qty: str | None = None) -> dict:
        return {"order_id": "should-not-be-reached", "status": "accepted"}

    engine, log_path = _real_policy_engine(tmp_path)

    def fake_pnl_fetcher():
        return account_data.AccountPnLResult(ok=True, session_pnl_usd=-5000.0)

    proxy = build_proxy(
        upstream, policy_engine=engine, account_pnl_fetcher=fake_pnl_fetcher
    )

    async def run():
        async with Client(proxy) as client:
            return await client.call_tool(
                "place_stock_order",
                {"symbol": "AAPL", "side": "buy", "qty": "1", "limit_price": "150.00"},
                raise_on_error=False,
            )

    result = asyncio.run(run())

    assert result.is_error
    assert "drawdown-killswitch" in result.content[0].text

    records = _read_records(log_path)
    killswitch_blocks = [
        r
        for r in records
        if r["verdict"] == "hard_block" and r["rule_id"] == "drawdown-killswitch"
    ]
    assert len(killswitch_blocks) == 1


def test_account_equity_reaches_cvar_gate_from_the_same_fetch(tmp_path):
    """state["account_equity"] must now be populated from the same account
    fetch that feeds session_pnl_usd, letting cvar_gate evaluate a genuine
    CVaR figure against real account scale instead of hard-blocking on
    "insufficient account state" (missing/non-numeric account_equity).
    Uses PolicyEngine.from_yaml's bars_fetcher injection point (see
    policy.py's _BARS_FETCHER_AWARE_RULE_TYPES) to give cvar-gate/pct-of-adv
    a deterministic, low-volatility fake series -- keeps this hermetic
    while still exercising cvar_gate's own real CVaR math against a
    real-scale equity figure ($100k, matching this project's real paper
    account -- see the live check this test formalizes)."""
    from firewall.market_data import BarsResult, DailyBar

    calls = []

    def fake_bars_fetcher(symbol, lookback_days, **kwargs):
        calls.append(symbol)
        # Flat, low-vol closes around $100 with real volume -- a $100
        # order's CVaR-implied loss stays far under 2% of $100k equity
        # ($2,000), and 1 share is far under 1% of a 1M-share ADV.
        closes = [100.0 + (i % 3) * 0.1 for i in range(lookback_days)]
        return BarsResult(
            ok=True,
            bars=[DailyBar(close=c, volume=1_000_000.0) for c in closes],
        )

    upstream = FastMCP("fake-alpaca")

    @upstream.tool
    def place_stock_order(
        symbol: str, side: str, qty: str | None = None, limit_price: str | None = None
    ) -> dict:
        return {"order_id": "fake-order-1", "status": "accepted"}

    engine, log_path = _real_policy_engine(tmp_path, bars_fetcher=fake_bars_fetcher)

    def fake_account_fetcher():
        return account_data.AccountPnLResult(ok=True, session_pnl_usd=0.0, equity=100_000.0)

    proxy = build_proxy(
        upstream, policy_engine=engine, account_pnl_fetcher=fake_account_fetcher
    )

    async def run():
        async with Client(proxy) as client:
            return await client.call_tool(
                "place_stock_order",
                {"symbol": "AAPL", "side": "buy", "qty": "1", "limit_price": "100.00"},
                raise_on_error=False,
            )

    result = asyncio.run(run())

    assert not result.is_error, result
    assert "AAPL" in calls, "cvar_gate/pct_of_adv must have actually run against real data"

    records = _read_records(log_path)
    cvar_records = [r for r in records if r.get("rule_id") == "cvar-gate"]
    # cvar-gate only writes a record when it actually blocks (RuleOutcome
    # triggered=True writes hard_block; triggered=False writes nothing of
    # its own -- the overall "allow" record covers it). Either way, the
    # fail-closed-on-missing-state message must never appear: proof this
    # ran against real equity, not the documented gap.
    assert not any(
        "insufficient account state" in (r.get("reason") or "") for r in cvar_records
    )
    assert not any(
        "missing or not numeric" in (r.get("reason") or "") for r in records
    )


def test_position_cap_cross_chunk_breach_is_caught_with_a_tight_cap(tmp_path):
    """Same setup as above, but with a cap tight enough that only the SUM
    of two same-symbol chunks breaches it -- proves the in-flight add is
    the thing actually catching this, not just documenting intent."""
    import yaml

    raw = yaml.safe_load(Path("policies/default.yaml").read_text(encoding="utf-8"))
    for rule in raw["rules"]:
        if rule["id"] == "position-cap-per-symbol":
            rule["max_usd_per_symbol"] = 10000  # each chunk is $6,000; sum is $12,000
            rule["max_pct_of_equity"] = None  # isolate the static-cap path being tested
        if rule["id"] == "notional-cap-single-order":
            rule["max_usd"] = 10000  # each $6,000 chunk must clear this individually
            rule["max_pct_of_equity"] = None
        if rule["id"] in ("cvar-gate", "pct-of-adv"):
            rule["enabled"] = False
    policy_path = tmp_path / "tight_position_cap.yaml"
    policy_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    log_path = tmp_path / "audit.jsonl"
    writer = AuditLogWriter(log_path, session_id="test-session")
    engine = PolicyEngine.from_yaml(
        policy_path, audit_writer=writer, bars_fetcher=_default_test_bars_fetcher
    )

    upstream = FastMCP("fake-alpaca")

    @upstream.tool
    def place_stock_order(
        symbol: str, side: str, qty: str | None = None, limit_price: str | None = None
    ) -> dict:
        return {"order_id": "fake-order", "status": "accepted"}

    def fake_positions_fetcher():
        return account_data.PositionsResult(ok=True, positions={"AAPL": 0.0}, fetched_at=0.0)

    def fake_account_fetcher():
        return account_data.AccountPnLResult(ok=True, session_pnl_usd=0.0, equity=100_000.0)

    proxy = build_proxy(
        upstream,
        policy_engine=engine,
        positions_fetcher=fake_positions_fetcher,
        account_pnl_fetcher=fake_account_fetcher,
    )

    async def run():
        async with Client(proxy) as client:
            first = await client.call_tool(
                "place_stock_order",
                {"symbol": "AAPL", "side": "buy", "qty": "20", "limit_price": "300.00"},
                raise_on_error=False,
            )
            second = await client.call_tool(
                "place_stock_order",
                {"symbol": "AAPL", "side": "buy", "qty": "20", "limit_price": "300.00"},
                raise_on_error=False,
            )
            return first, second

    first, second = asyncio.run(run())

    assert not first.is_error, first
    assert second.is_error
    assert "position-cap-per-symbol" in second.content[0].text

    records = _read_records(log_path)
    breach = next(r for r in records if r["rule_id"] == "position-cap-per-symbol")
    # $6,000 in-flight (first chunk) + $6,000 (this order) = $12,000, not
    # $6,000 -- proof the sum, not just this call's own notional, was used.
    assert "12,000.00" in breach["reason"]


def test_session_pnl_fetch_failure_is_recorded_in_audit_log(tmp_path):
    """A fetch failure must not be a silent skip: drawdown_killswitch
    already fails *open* on missing session_pnl_usd (AUDIT.md E3 flags
    this as a separate, pre-existing defect not in scope here), but the
    fetch failure itself must be visible in the audit log so an operator
    can tell "no loss data was available" from "loss data said we're
    fine."""
    upstream = FastMCP("fake-alpaca")

    @upstream.tool
    def place_stock_order(
        symbol: str, side: str, qty: str | None = None, limit_price: str | None = None
    ) -> dict:
        return {"order_id": "fake-order-1", "status": "accepted"}

    engine, log_path = _real_policy_engine_without_market_data_rules(tmp_path)

    def failing_pnl_fetcher():
        return account_data.AccountPnLResult(
            ok=False, reason="timed out after 5.0s fetching account"
        )

    proxy = build_proxy(
        upstream, policy_engine=engine, account_pnl_fetcher=failing_pnl_fetcher
    )

    async def run():
        async with Client(proxy) as client:
            return await client.call_tool(
                "place_stock_order",
                {"symbol": "AAPL", "side": "buy", "qty": "1", "limit_price": "150.00"},
                raise_on_error=False,
            )

    result = asyncio.run(run())

    assert not result.is_error  # drawdown_killswitch fails open here -- documented, in scope elsewhere

    records = _read_records(log_path)
    fetch_failure_records = [
        r
        for r in records
        if r["verdict"] == "soft_block" and "timed out after 5.0s" in r["reason"]
    ]
    assert len(fetch_failure_records) == 1


def test_pnl_fetch_is_not_triggered_for_non_order_calls(tmp_path):
    upstream = FastMCP("fake-alpaca")

    @upstream.tool
    def get_account_info() -> dict:
        return {"status": "ACTIVE"}

    engine, _ = _real_policy_engine(tmp_path)
    calls = []

    def counting_pnl_fetcher():
        calls.append(1)
        return account_data.AccountPnLResult(ok=True, session_pnl_usd=0.0)

    proxy = build_proxy(
        upstream, policy_engine=engine, account_pnl_fetcher=counting_pnl_fetcher
    )

    async def run():
        async with Client(proxy) as client:
            return await client.call_tool("get_account_info", {})

    asyncio.run(run())

    assert calls == []


def test_pnl_fetch_gate_also_matches_read_only_order_queries(tmp_path):
    # Documents the actual (not aspirational) precision of
    # _SESSION_PNL_RELEVANT_TOOLS: it's the same "order"-substring pattern
    # drawdown_killswitch's own default tool_match uses, so a read-only
    # get_orders call also triggers a fetch -- accepted, since matching the
    # rule's own reach exactly is more defensible than a narrower gate that
    # disagrees with it.
    upstream = FastMCP("fake-alpaca")

    @upstream.tool
    def get_orders() -> dict:
        return {"orders": []}

    engine, _ = _real_policy_engine(tmp_path)
    calls = []

    def counting_pnl_fetcher():
        calls.append(1)
        return account_data.AccountPnLResult(ok=True, session_pnl_usd=0.0)

    proxy = build_proxy(
        upstream, policy_engine=engine, account_pnl_fetcher=counting_pnl_fetcher
    )

    async def run():
        async with Client(proxy) as client:
            return await client.call_tool("get_orders", {})

    asyncio.run(run())

    assert calls == [1]


def test_build_proxy_threads_session_pnl_timeout_and_ttl_env_vars_through(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("FIREWALL_SESSION_PNL_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("FIREWALL_SESSION_PNL_CACHE_TTL_SECONDS", "7.0")

    captured = {}

    def spy_fetch_session_pnl(*, timeout_seconds, cache_ttl_seconds):
        captured["timeout_seconds"] = timeout_seconds
        captured["cache_ttl_seconds"] = cache_ttl_seconds
        return account_data.AccountPnLResult(ok=True, session_pnl_usd=0.0)

    monkeypatch.setattr(account_data, "fetch_session_pnl", spy_fetch_session_pnl)

    upstream = FastMCP("fake-alpaca")

    @upstream.tool
    def place_stock_order(symbol: str, side: str, qty: str | None = None) -> dict:
        return {"order_id": "fake-order", "status": "accepted"}

    engine, _ = _real_policy_engine(tmp_path)
    # No account_pnl_fetcher passed -- must fall back to the real
    # account_data.fetch_session_pnl (monkeypatched above), with the
    # timeout/TTL read from the env vars just set.
    proxy = build_proxy(upstream, policy_engine=engine)

    async def run():
        async with Client(proxy) as client:
            return await client.call_tool(
                "place_stock_order",
                {"symbol": "AAPL", "side": "buy", "qty": "1"},
                raise_on_error=False,
            )

    asyncio.run(run())

    assert captured == {"timeout_seconds": 2.5, "cache_ttl_seconds": 7.0}


# --- hedge_proposal: detection + audit only, no submission ------------------


def test_hedge_proposal_survives_even_when_the_same_call_hard_blocks(tmp_path, monkeypatch):
    """The exact case hedge_proposal.py's module docstring says matters
    most: drawdown_killswitch hard-blocking on a real breach must not
    silently swallow the hedge proposal computed on the same call --
    PolicyEngine's own Warning-collection pipeline would drop it
    (policy.py: record_call_pending/record_call_outcome both no-op on
    hard_block), so this proves the direct-audit-write path survives it."""
    upstream = FastMCP("fake-alpaca")

    @upstream.tool
    def place_stock_order(
        symbol: str, side: str, qty: str | None = None, limit_price: str | None = None
    ) -> dict:
        return {"order_id": "fake-order", "status": "accepted"}

    # See _real_policy_engine_without_market_data_rules's docstring: cvar-gate
    # hard-blocks every computable-notional limit order today (a separate,
    # documented, out-of-scope gap -- account_equity is never populated),
    # which would otherwise make it impossible to isolate
    # drawdown_killswitch/hedge_proposal's own interaction here.
    engine, log_path = _real_policy_engine_without_market_data_rules(tmp_path)

    calls = {"n": 0}

    def fake_pnl_fetcher():
        calls["n"] += 1
        # First call: fine, doesn't trip drawdown_killswitch -- lets the
        # AAPL order through so order_history has a live position to flag.
        if calls["n"] == 1:
            return account_data.AccountPnLResult(ok=True, session_pnl_usd=0.0)
        # Every call after: a real breach of default.yaml's -$1,000
        # session_pnl_threshold_usd (drawdown_killswitch's sticky latch,
        # once tripped, stays tripped -- see drawdown_killswitch.py).
        return account_data.AccountPnLResult(ok=True, session_pnl_usd=-5000.0)

    def fake_bars_fetcher(symbol, lookback_days):
        return BarsResult(ok=True, bars=[DailyBar(close=150.0, volume=1000.0)])

    import firewall.rules.hedge_proposal as hedge_proposal_module

    monkeypatch.setattr(hedge_proposal_module, "fetch_daily_bars", fake_bars_fetcher)

    proxy = build_proxy(
        upstream, policy_engine=engine, account_pnl_fetcher=fake_pnl_fetcher
    )

    async def run():
        async with Client(proxy) as client:
            first = await client.call_tool(
                "place_stock_order",
                {"symbol": "AAPL", "side": "buy", "qty": "10", "limit_price": "150.00"},
                raise_on_error=False,
            )
            second = await client.call_tool(
                "place_stock_order",
                {"symbol": "MSFT", "side": "buy", "qty": "1", "limit_price": "300.00"},
                raise_on_error=False,
            )
            return first, second

    first, second = asyncio.run(run())

    assert not first.is_error
    assert second.is_error
    assert "drawdown-killswitch" in second.content[0].text

    records = _read_records(log_path)
    killswitch_blocks = [
        r
        for r in records
        if r["verdict"] == "hard_block" and r["rule_id"] == "drawdown-killswitch"
    ]
    assert len(killswitch_blocks) == 1

    hedge_records = [r for r in records if r["rule_id"] == "hedge-proposal"]
    assert len(hedge_records) == 1
    assert hedge_records[0]["verdict"] == "soft_block"
    assert "DEFENSIVE HEDGE PROPOSAL" in hedge_records[0]["reason"]
    assert "PROPOSAL ONLY" in hedge_records[0]["reason"]
    assert "not a market view" in hedge_records[0]["reason"]
    # Flags AAPL (the position established by call 1), not MSFT (the
    # symbol of the call that actually triggered the killswitch) -- proves
    # order_history, not the triggering call's own arguments, supplies the
    # "which position" context, exactly as the module docstring specifies.
    assert "AAPL" in hedge_records[0]["reason"]
    assert "MSFT" not in hedge_records[0]["reason"]


def test_hedge_proposal_fires_for_position_seeded_by_plain_market_order(
    tmp_path, monkeypatch
):
    """AUDIT.md's own live reproduction of the fixed bug: a plain market
    order (symbol/side/qty only, no `limit_price` -- the schema-documented
    default order shape, and the single most common real order shape)
    used to leave `_largest_open_position_from_history` unable to see the
    seeded position at all (OrderEvent.price stayed None, and the old
    implementation excluded every price=None event), so the
    drawdown-triggered hedge proposal silently produced zero audit
    records. This proves the same scenario now fires exactly one."""
    upstream = FastMCP("fake-alpaca")

    @upstream.tool
    def place_stock_order(
        symbol: str, side: str, qty: str | None = None, limit_price: str | None = None
    ) -> dict:
        return {"order_id": "fake-order", "status": "accepted"}

    engine, log_path = _real_policy_engine_without_market_data_rules(tmp_path)

    calls = {"n": 0}

    def fake_pnl_fetcher():
        calls["n"] += 1
        if calls["n"] == 1:
            return account_data.AccountPnLResult(ok=True, session_pnl_usd=0.0)
        return account_data.AccountPnLResult(ok=True, session_pnl_usd=-5000.0)

    def fake_bars_fetcher(symbol, lookback_days):
        return BarsResult(ok=True, bars=[DailyBar(close=150.0, volume=1000.0)])

    import firewall.rules.hedge_proposal as hedge_proposal_module

    monkeypatch.setattr(hedge_proposal_module, "fetch_daily_bars", fake_bars_fetcher)

    proxy = build_proxy(
        upstream, policy_engine=engine, account_pnl_fetcher=fake_pnl_fetcher
    )

    async def run():
        async with Client(proxy) as client:
            # Plain market order: no limit_price at all -- the audit's
            # exact reproduction of the bug.
            first = await client.call_tool(
                "place_stock_order",
                {"symbol": "AAPL", "side": "buy", "qty": "10", "limit_price": "150.00"},
                raise_on_error=False,
            )
            second = await client.call_tool(
                "place_stock_order",
                {"symbol": "MSFT", "side": "buy", "qty": "1", "limit_price": "300.00"},
                raise_on_error=False,
            )
            return first, second

    first, second = asyncio.run(run())

    assert not first.is_error
    assert second.is_error
    assert "drawdown-killswitch" in second.content[0].text

    records = _read_records(log_path)
    hedge_records = [r for r in records if r["rule_id"] == "hedge-proposal"]
    assert len(hedge_records) == 1
    assert hedge_records[0]["verdict"] == "soft_block"
    # Flags AAPL (the market-order-seeded position), not MSFT.
    assert "AAPL" in hedge_records[0]["reason"]
    assert "MSFT" not in hedge_records[0]["reason"]


def test_no_hedge_proposal_when_nothing_crosses_threshold(tmp_path):
    upstream = FastMCP("fake-alpaca")

    @upstream.tool
    def place_stock_order(
        symbol: str, side: str, qty: str | None = None, limit_price: str | None = None
    ) -> dict:
        return {"order_id": "fake-order", "status": "accepted"}

    engine, log_path = _real_policy_engine_without_market_data_rules(tmp_path)

    def fake_pnl_fetcher():
        return account_data.AccountPnLResult(ok=True, session_pnl_usd=0.0)

    proxy = build_proxy(
        upstream, policy_engine=engine, account_pnl_fetcher=fake_pnl_fetcher
    )

    async def run():
        async with Client(proxy) as client:
            return await client.call_tool(
                "place_stock_order",
                {"symbol": "AAPL", "side": "buy", "qty": "10", "limit_price": "150.00"},
                raise_on_error=False,
            )

    result = asyncio.run(run())

    assert not result.is_error
    records = _read_records(log_path)
    hedge_records = [r for r in records if r["rule_id"] == "hedge-proposal"]
    assert hedge_records == []


# --- crash-safe audit logging: pending record survives a process death
# between the pending write and call_next's return --------------------------


class _SimulatedProcessCrash(BaseException):
    """Stands in for an actual process death (SIGKILL, OOM, power loss) --
    deliberately a BaseException, not Exception, so it is NOT caught by
    on_call_tool's `except Exception:` safety net (which correctly
    recovers from ordinary upstream failures, e.g.
    test_upstream_error_after_allow_is_still_reported_and_logged above,
    by still writing an outcome record). A real crash wouldn't run any of
    our exception-handling code either; this is the closest a single
    Python process can get to simulating that without actually dying."""


def test_process_crash_between_pending_write_and_call_next_leaves_pending_record_alone(
    tmp_path,
):
    """If the process dies after record_call_pending() writes its record
    but before call_next() returns, the pending record must be the one
    and only record for that call: present, chain-valid, and (once stale)
    flaggable by find_unresolved_pending as a call whose outcome is
    unknown -- not silently lost, and not incorrectly 'completed' by a
    record that was never actually written."""
    engine, log_path = _real_policy_engine(tmp_path)
    middleware = FirewallMiddleware(engine)
    context = SimpleNamespace(
        message=SimpleNamespace(name="get_account_info", arguments={})
    )

    async def crashing_call_next(_context):
        raise _SimulatedProcessCrash("simulated SIGKILL")

    async def run():
        await middleware.on_call_tool(context, crashing_call_next)

    with pytest.raises(_SimulatedProcessCrash):
        asyncio.run(run())

    records = _read_records(log_path)
    assert len(records) == 1
    pending_record = records[0]
    assert pending_record["verdict"] == "allow"
    assert pending_record["upstream_status"] == "pending"
    assert pending_record["forwarded"] is None
    assert pending_record["call_id"] is not None

    ok, bad_index = verify_chain(log_path)
    assert ok is True
    assert bad_index is None

    stale = find_unresolved_pending(log_path, stale_after_seconds=0.0)
    assert len(stale) == 1
    assert stale[0].call_id == pending_record["call_id"]


# --- hedge normalization & release notification ----------------------------


def test_hedge_normalization_emits_audit_flag_and_attaches_note_to_next_tool_result(
    tmp_path, monkeypatch
):
    upstream = FastMCP("fake-alpaca")

    @upstream.tool
    def place_stock_order(
        symbol: str, side: str, qty: str | None = None, limit_price: str | None = None
    ) -> dict:
        return {"order_id": f"fake-{symbol}", "status": "accepted"}

    @upstream.tool
    def get_account_info() -> dict:
        return {"status": "ACTIVE"}

    engine, log_path = _real_policy_engine_without_market_data_rules(tmp_path)

    calls = {"n": 0}

    def fake_pnl_fetcher():
        calls["n"] += 1
        # Call 1: normal PnL -> establishes AAPL position
        if calls["n"] == 1:
            return account_data.AccountPnLResult(ok=True, session_pnl_usd=0.0)
        # Call 2: breach (-5000) -> triggers hedge proposal on AAPL
        if calls["n"] == 2:
            return account_data.AccountPnLResult(ok=True, session_pnl_usd=-5000.0)
        # Call 3+: PnL recovers to 0.0 -> normalizes trigger condition
        return account_data.AccountPnLResult(ok=True, session_pnl_usd=0.0)

    def fake_bars_fetcher(symbol, lookback_days):
        return BarsResult(ok=True, bars=[DailyBar(close=150.0, volume=1000.0)])

    import firewall.rules.hedge_proposal as hedge_proposal_module

    monkeypatch.setattr(hedge_proposal_module, "fetch_daily_bars", fake_bars_fetcher)

    proxy = build_proxy(
        upstream, policy_engine=engine, account_pnl_fetcher=fake_pnl_fetcher
    )

    async def run():
        async with Client(proxy) as client:
            # 1. Establish AAPL position
            res1 = await client.call_tool(
                "place_stock_order",
                {"symbol": "AAPL", "side": "buy", "qty": "10", "limit_price": "150.00"},
                raise_on_error=False,
            )
            # 2. Trigger drawdown breach & hedge proposal on AAPL
            res2 = await client.call_tool(
                "place_stock_order",
                {"symbol": "MSFT", "side": "buy", "qty": "1", "limit_price": "300.00"},
                raise_on_error=False,
            )
            # Reset killswitch latch so subsequent order placement is permitted once PnL recovers
            engine.reset("drawdown-killswitch")
            # 3. PnL normalizes; next natural tool call (order-related to refresh session PnL)
            res3 = await client.call_tool(
                "place_stock_order",
                {"symbol": "AAPL", "side": "buy", "qty": "1", "limit_price": "150.00"},
                raise_on_error=False,
            )
            # 4. Subsequent tool call (note should not be repeated)
            res4 = await client.call_tool("get_account_info", {})
            return res1, res2, res3, res4

    res1, res2, res3, res4 = asyncio.run(run())

    assert not res1.is_error
    assert res2.is_error  # blocked by killswitch

    # Call 3 received the plain informational note attached to its result
    assert not res3.is_error
    assert len(res3.content) >= 2
    attached_texts = [c.text for c in res3.content]
    expected_note = "hedge on $AAPL: trigger condition resolved, review for release"
    assert any(expected_note in text for text in attached_texts)

    # Call 4 does not repeat the note
    assert not res4.is_error
    assert not any(expected_note in c.text for c in res4.content)

    # Check audit records
    records = _read_records(log_path)
    release_records = [
        r for r in records if r["tool_name"] == "hedge_release:flagged"
    ]
    assert len(release_records) == 1
    assert release_records[0]["verdict"] == "soft_block"
    assert release_records[0]["rule_id"] == "hedge-proposal"
    assert release_records[0]["reason"] == expected_note
    assert release_records[0]["arguments"] == {"symbol": "AAPL", "trigger": "drawdown_killswitch"}


def test_hedge_normalization_delivers_note_after_blocked_call(tmp_path, monkeypatch):
    upstream = FastMCP("fake-alpaca")

    @upstream.tool
    def place_stock_order(
        symbol: str, side: str, qty: str | None = None, limit_price: str | None = None
    ) -> dict:
        return {"order_id": f"fake-{symbol}", "status": "accepted"}

    @upstream.tool
    def close_all_positions() -> dict:
        return {"closed": "all"}

    @upstream.tool
    def get_account_info() -> dict:
        return {"status": "ACTIVE"}

    engine, log_path = _real_policy_engine_without_market_data_rules(tmp_path)

    calls = {"n": 0}

    def fake_pnl_fetcher():
        calls["n"] += 1
        if calls["n"] == 1:
            return account_data.AccountPnLResult(ok=True, session_pnl_usd=0.0)
        if calls["n"] == 2:
            return account_data.AccountPnLResult(ok=True, session_pnl_usd=-5000.0)
        return account_data.AccountPnLResult(ok=True, session_pnl_usd=0.0)

    def fake_bars_fetcher(symbol, lookback_days):
        return BarsResult(ok=True, bars=[DailyBar(close=150.0, volume=1000.0)])

    import firewall.rules.hedge_proposal as hedge_proposal_module

    monkeypatch.setattr(hedge_proposal_module, "fetch_daily_bars", fake_bars_fetcher)

    proxy = build_proxy(
        upstream, policy_engine=engine, account_pnl_fetcher=fake_pnl_fetcher
    )

    async def run():
        async with Client(proxy) as client:
            # 1. Establish AAPL position
            await client.call_tool(
                "place_stock_order",
                {"symbol": "AAPL", "side": "buy", "qty": "10", "limit_price": "150.00"},
                raise_on_error=False,
            )
            # 2. Trigger drawdown breach & hedge proposal on AAPL
            await client.call_tool(
                "place_stock_order",
                {"symbol": "MSFT", "side": "buy", "qty": "1", "limit_price": "300.00"},
                raise_on_error=False,
            )
            # 3. An order call that triggers normalization but hard-blocks (e.g. symbol not allowlisted)
            res3 = await client.call_tool(
                "place_stock_order",
                {"symbol": "DISALLOWED", "side": "buy", "qty": "1"},
                raise_on_error=False,
            )
            # 4. Subsequent successful call naturally receives the pending note
            res4 = await client.call_tool("get_account_info", {})
            return res3, res4

    res3, res4 = asyncio.run(run())

    assert res3.is_error
    assert not res4.is_error
    expected_note = "hedge on $AAPL: trigger condition resolved, review for release"
    attached_texts = [c.text for c in res4.content]
    assert any(expected_note in text for text in attached_texts)


def test_cvar_hedge_normalization_emits_audit_and_attaches_note(tmp_path):
    engine, log_path = _real_policy_engine_without_market_data_rules(tmp_path)

    bars_calm = BarsResult(ok=True, bars=[DailyBar(close=100.0, volume=1000.0) for _ in range(30)])
    cvar_rule = CVaRGateRule(
        RuleConfig.model_validate(
            {
                "id": "cvar-gate",
                "type": "cvar_gate",
                "severity": "hard",
                "regulation_ref": None,
                "cvar_max_loss_pct_of_equity": 0.05,
                "cvar_alpha": 0.95,
                "cvar_lookback_days": 30,
            }
        ),
        bars_fetcher=lambda sym, lookback: bars_calm,
    )

    middleware = FirewallMiddleware(engine)
    middleware._cvar_gate_rule = cvar_rule
    middleware.register_open_hedge("AAPL", "cvar_gate")

    # Record open position in AAPL into order_history
    middleware._order_history.record(
        timestamp=1.0, tool="place_stock_order", symbol="AAPL", side="buy",
        qty=10, price=100.0, order_id="1", outcome="filled",
    )

    upstream = FastMCP("fake-alpaca")

    @upstream.tool
    def get_account_info() -> dict:
        return {"status": "ACTIVE"}

    proxy = FastMCP("proxy-test")
    proxy.add_middleware(middleware)
    # Wire upstream to proxy client
    async def run():
        async with Client(upstream) as client:
            context = SimpleNamespace(
                message=SimpleNamespace(name="get_account_info", arguments={})
            )
            async def call_next(_ctx):
                return await client.call_tool("get_account_info", {})
            # State with equity and calm bars
            middleware._account_pnl_fetcher = lambda: account_data.AccountPnLResult(ok=True, session_pnl_usd=0.0)
            orig_check = middleware._check_hedge_normalization
            def check_with_equity(state):
                state["account_equity"] = 10_000.0
                return orig_check(state)
            middleware._check_hedge_normalization = check_with_equity

            return await middleware.on_call_tool(context, call_next)

    result = asyncio.run(run())

    assert not result.is_error
    expected_note = "hedge on $AAPL: trigger condition resolved, review for release"
    attached_texts = [c.text for c in result.content]
    assert any(expected_note in text for text in attached_texts)

    records = _read_records(log_path)
    release_records = [
        r for r in records if r["tool_name"] == "hedge_release:flagged"
    ]
    assert len(release_records) == 1
    assert release_records[0]["verdict"] == "soft_block"
    assert release_records[0]["reason"] == expected_note
    assert release_records[0]["arguments"] == {"symbol": "AAPL", "trigger": "cvar_gate"}


def test_on_read_resource_fails_closed_and_audits(tmp_path):
    from fastmcp.exceptions import ResourceError

    engine, log_path = _real_policy_engine(tmp_path)
    middleware = FirewallMiddleware(engine)

    context = SimpleNamespace(message=SimpleNamespace(uri="resource://alpaca/market_overview"))

    async def dummy_call_next(_ctx):
        return "data"

    with pytest.raises(ResourceError, match="BLOCKED: resource access"):
        asyncio.run(middleware.on_read_resource(context, dummy_call_next))

    records = _read_records(log_path)
    assert len(records) == 1
    assert records[0]["tool_name"] == "read_resource"
    assert records[0]["verdict"] == "hard_block"
    assert records[0]["arguments"]["uri"] == "resource://alpaca/market_overview"
    assert records[0]["rule_id"] == "unsupported_endpoint_guard"


def test_on_get_prompt_fails_closed_and_audits(tmp_path):
    from fastmcp.exceptions import PromptError

    engine, log_path = _real_policy_engine(tmp_path)
    middleware = FirewallMiddleware(engine)

    context = SimpleNamespace(
        message=SimpleNamespace(name="trade_analysis_prompt", arguments={"symbol": "AAPL"})
    )

    async def dummy_call_next(_ctx):
        return "prompt text"

    with pytest.raises(PromptError, match="BLOCKED: prompt access"):
        asyncio.run(middleware.on_get_prompt(context, dummy_call_next))

    records = _read_records(log_path)
    assert len(records) == 1
    assert records[0]["tool_name"] == "get_prompt"
    assert records[0]["verdict"] == "hard_block"
    assert records[0]["arguments"]["name"] == "trade_analysis_prompt"
    assert records[0]["arguments"]["arguments"] == {"symbol": "AAPL"}
    assert records[0]["rule_id"] == "unsupported_endpoint_guard"
