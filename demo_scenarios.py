"""Hermetic, film-ready firewall scenarios.

This is deliberately a thin CLI over the controlled-double pattern proven in
``tests/test_proxy.py``.  It does not implement policy decisions: every
verdict comes from the real PolicyEngine loaded from ``policies/default.yaml``
through the real proxy middleware.  The only doubles are the broker backend
and the explicitly injected account/position reads.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parent
SRC_DIR = WORKSPACE_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from fastmcp import Client, FastMCP
from rich.console import Console
from rich.markup import escape

from firewall import account_data
from firewall.audit import AuditLogWriter, verify_chain
from firewall.market_data import BarsResult, DailyBar
from firewall.policy import PolicyEngine
from firewall.proxy import build_proxy

console = Console(highlight=False)
REAL_AUDIT_PATH = WORKSPACE_ROOT / "audit.jsonl"
DEMO_AUDIT_PATH = WORKSPACE_ROOT / "data" / "demo_audit.jsonl"


def _default_test_bars_fetcher(symbol: str, lookback_days: int, **_: Any) -> BarsResult:
    """The same deterministic bars double used by tests/test_proxy.py.

    Keeping market data hermetic is essential: the real default policy has
    market-data-aware rules, so no call in these demos may fall through to an
    Alpaca-backed fetcher.
    """
    return BarsResult(
        ok=True,
        bars=[DailyBar(close=100.0, volume=1_000_000.0) for _ in range(max(lookback_days, 1))],
    )


def _make_fake_upstream() -> FastMCP:
    """Same controlled FastMCP upstream shape used by the proxy tests."""
    upstream = FastMCP("demo-fake-alpaca")

    @upstream.tool
    def place_stock_order(
        symbol: str,
        side: str,
        qty: str | None = None,
        limit_price: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, str]:
        # dry_run=True prevents this from being reached.  It exists only to
        # provide the same fake broker tool contract used in test_proxy.py.
        return {"id": "demo-fake-order", "status": "accepted"}

    return upstream


def _audit_snapshot(path: Path) -> dict[str, Any]:
    raw = path.read_bytes() if path.exists() else b""
    lines = [line for line in raw.splitlines() if line.strip()]
    chain_ok, bad_index = verify_chain(path) if path.exists() else (True, None)
    return {
        "record_count": len(lines),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "chain_ok": chain_ok,
        "bad_index": bad_index,
    }


def _format_result_like_step_3(result: Any, *, showcase: bool = False) -> None:
    """Use the same human-readable result blocks as run_agent.py STEP 3."""
    if result.is_error:
        error_text = getattr(result.content[0], "text", str(result)) if result.content else str(result)
        rule_name = "policy_rule"
        clean_reason = error_text
        if "BLOCKED by rule '" in error_text:
            parts = error_text.split("BLOCKED by rule '", 1)[1].split("':", 1)
            if len(parts) == 2:
                rule_name, clean_reason = parts[0], parts[1].strip()
        console.print(
            f"    [bold red][X] BLOCKED BY FIREWALL RULE: '{escape(str(rule_name))}'[/bold red]\n"
            f"       [yellow]Reason:[/yellow] {escape(str(clean_reason))}\n"
            "       [dim]Protection Note: This block is the policy engine working as designed to prevent risk violation.[/dim]"
        )
        return

    console.print(
        "    [bold green][OK] DRY RUN: FIREWALL ALLOWED; UPSTREAM MUTATION SUPPRESSED[/bold green]\n"
        "       [dim]Broker status: dry_run (no parseable order ID) | "
        f"Audit correlation: {'scenario order' if showcase else 'demo controlled double'}[/dim]"
    )
    for item in getattr(result, "content", []) or []:
        note = getattr(item, "text", "")
        if isinstance(note, str) and note.startswith("DELEVERAGING_EXCEPTION_ALLOW:"):
            console.print(
                "       [bold cyan]Killswitch exception:[/bold cyan] " + escape(note)
            )


def _print_killswitch_policy_context() -> None:
    """Show the exact configured rule/citation alongside the film verdict."""
    engine = PolicyEngine.from_yaml(WORKSPACE_ROOT / "policies" / "default.yaml")
    rule = next(rule for rule in engine.rules if rule.id == "drawdown-killswitch")
    console.print(
        "       [bold cyan]Policy rule:[/bold cyan] drawdown-killswitch  |  "
        f"[bold cyan]Regulation:[/bold cyan] {escape(str(rule.regulation_ref))}"
    )


async def _run_scenario(name: str) -> Any:
    DEMO_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    engine = PolicyEngine.from_yaml(
        WORKSPACE_ROOT / "policies" / "default.yaml",
        audit_writer=AuditLogWriter(DEMO_AUDIT_PATH, session_id=f"demo-{name}"),
        bars_fetcher=_default_test_bars_fetcher,
    )

    held_qty = 10.0 if name in ("deleveraging", "oversell") else 0.0
    proxy = build_proxy(
        _make_fake_upstream(),
        policy_engine=engine,
        dry_run=True,
        account_pnl_fetcher=lambda: account_data.AccountPnLResult(
            ok=True, session_pnl_usd=-1_500.0, equity=100_000.0
        ),
        positions_fetcher=lambda: account_data.PositionsResult(
            ok=True,
            positions={"AAPL": 1_000.0} if held_qty else {},
            quantities={"AAPL": held_qty} if held_qty else {},
            current_prices={"AAPL": 100.0} if held_qty else {},
            fetched_at=time.time(),
        ),
        open_orders_fetcher=lambda prices: account_data.OpenOrdersResult(
            ok=True, orders=(), aggregate_outstanding_notional=0.0
        ),
    )
    order = (
        {"symbol": "AAPL", "side": "sell", "qty": "10" if name == "deleveraging" else "11", "client_order_id": f"demo-{name}-1"}
        if name in ("deleveraging", "oversell")
        else {"symbol": "AAPL", "side": "buy", "qty": "1", "limit_price": "100", "client_order_id": "demo-killswitch-1"}
    )
    async with Client(proxy) as client:
        return await client.call_tool("place_stock_order", order, raise_on_error=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--showcase",
        action="store_true",
        help="render the Step 3-style presentation view while retaining the isolated test boundary",
    )
    parser.add_argument("scenario", choices=("killswitch", "deleveraging", "oversell"))
    args = parser.parse_args(argv)

    before = _audit_snapshot(REAL_AUDIT_PATH)
    if args.showcase:
        console.print("[bold cyan]================================================================================[/bold cyan]")
        console.print("[bold yellow]STEP 3: Live Firewall Policy Enforcement & Order Execution[/bold yellow]")
        console.print("Submitting proposed order through the MCP proxy firewall.", style="dim")
        console.print("[dim]Isolated scenario environment - broker mutation suppressed[/dim]")
    else:
        console.print(f"[bold cyan]DEMO SCENARIO: {args.scenario.upper()} (controlled broker double)[/bold cyan]")
        console.print("[dim]Policy: policies/default.yaml | P&L: injected -$1,500 | Upstream: fake | Proxy mutation: suppressed[/dim]")
    result = asyncio.run(_run_scenario(args.scenario))
    _format_result_like_step_3(result, showcase=args.showcase)
    _print_killswitch_policy_context()
    after = _audit_snapshot(REAL_AUDIT_PATH)

    if before != after:
        console.print("[bold red]REAL AUDIT INTEGRITY CHECK FAILED: audit.jsonl changed.[/bold red]")
        return 1
    if not args.showcase:
        console.print(
            "[bold green]REAL AUDIT INTEGRITY CHECK PASSED:[/bold green] "
            f"audit.jsonl unchanged ({after['record_count']} records; chain "
            f"{'valid' if after['chain_ok'] else 'invalid'}; SHA-256 unchanged)."
        )
        console.print(f"[dim]Demo audit log: {DEMO_AUDIT_PATH}[/dim]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
