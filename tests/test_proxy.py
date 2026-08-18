"""Tests for the firewall proxy against a fake upstream MCP server."""

import asyncio
import json

import pytest
from fastmcp import Client, FastMCP

from firewall.audit import AuditLogWriter
from firewall.policy import PolicyEngine
from firewall.proxy import PaperTradeGuardError, _alpaca_client, build_proxy


def _real_policy_engine(tmp_path):
    """The real, unmodified default.yaml policy, wired to a tmp_path audit
    log -- never the old proof-of-concept hardcoded rule."""
    log_path = tmp_path / "audit.jsonl"
    writer = AuditLogWriter(log_path, session_id="test-session")
    engine = PolicyEngine.from_yaml("policies/default.yaml", audit_writer=writer)
    return engine, log_path


def _read_records(log_path):
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def make_fake_upstream() -> tuple[FastMCP, list[tuple[str, dict]]]:
    """A fake Alpaca-like upstream server that records every call it receives."""
    upstream = FastMCP("fake-alpaca")
    received: list[tuple[str, dict]] = []

    @upstream.tool
    def get_account() -> dict:
        received.append(("get_account", {}))
        return {"status": "ACTIVE"}

    @upstream.tool
    def close_position(symbol_or_asset_id: str) -> dict:
        received.append(("close_position", {"symbol_or_asset_id": symbol_or_asset_id}))
        return {"closed": symbol_or_asset_id}

    @upstream.tool
    def close_all_positions(cancel_orders: bool = False) -> dict:
        received.append(("close_all_positions", {"cancel_orders": cancel_orders}))
        return {"closed": "all"}

    return upstream, received


def test_normal_call_is_forwarded_upstream(tmp_path):
    upstream, received = make_fake_upstream()
    engine, _ = _real_policy_engine(tmp_path)
    proxy = build_proxy(upstream, policy_engine=engine)

    async def run():
        async with Client(proxy) as client:
            return await client.call_tool("get_account", {})

    result = asyncio.run(run())

    assert not result.is_error
    assert received == [("get_account", {})]


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


def test_close_single_position_is_not_blocked(tmp_path):
    upstream, received = make_fake_upstream()
    engine, _ = _real_policy_engine(tmp_path)
    proxy = build_proxy(upstream, policy_engine=engine)

    async def run():
        async with Client(proxy) as client:
            return await client.call_tool(
                "close_position", {"symbol_or_asset_id": "AAPL"}
            )

    result = asyncio.run(run())

    assert not result.is_error
    assert received == [("close_position", {"symbol_or_asset_id": "AAPL"})]


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


def test_allow_via_real_policy_forwards_and_writes_one_audit_record(tmp_path):
    upstream, received = make_fake_upstream()
    engine, log_path = _real_policy_engine(tmp_path)
    proxy = build_proxy(upstream, policy_engine=engine)

    async def run():
        async with Client(proxy) as client:
            return await client.call_tool("get_account", {})

    result = asyncio.run(run())

    assert not result.is_error
    assert received == [("get_account", {})]

    records = _read_records(log_path)
    assert len(records) == 1
    assert records[0]["verdict"] == "allow"
    assert records[0]["forwarded"] is True
    assert records[0]["upstream_status"] == "ok"


def test_upstream_error_after_allow_is_still_reported_and_logged(tmp_path):
    """An allowed call that then errors upstream must still be logged --
    exactly once, as forwarded=True/upstream_status=error -- not silently
    dropped."""
    upstream = FastMCP("fake-alpaca")

    @upstream.tool
    def get_account() -> dict:
        raise RuntimeError("upstream boom")

    engine, log_path = _real_policy_engine(tmp_path)
    proxy = build_proxy(upstream, policy_engine=engine)

    async def run():
        async with Client(proxy) as client:
            return await client.call_tool("get_account", {}, raise_on_error=False)

    result = asyncio.run(run())

    assert result.is_error

    records = _read_records(log_path)
    assert len(records) == 1
    assert records[0]["verdict"] == "allow"
    assert records[0]["forwarded"] is True
    assert records[0]["upstream_status"] == "error"


def test_build_proxy_uses_real_default_policy_when_none_given(tmp_path, monkeypatch):
    """The production default: no policy_engine passed in means build_proxy
    constructs the real PolicyEngine from policies/default.yaml, wired to a
    real AuditLogWriter -- not the old no-op hardcoded rule."""
    monkeypatch.setenv("FIREWALL_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    upstream, received = make_fake_upstream()
    proxy = build_proxy(upstream)

    async def run():
        async with Client(proxy) as client:
            return await client.call_tool("get_account", {})

    result = asyncio.run(run())

    assert not result.is_error
    assert received == [("get_account", {})]
    assert (tmp_path / "audit.jsonl").exists()
    records = _read_records(tmp_path / "audit.jsonl")
    assert len(records) == 1
    assert records[0]["verdict"] == "allow"


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
