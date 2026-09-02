"""run_agent — Single entry point that runs the full trading system end to end.

Starts the MCP proxy firewall connected to the real Alpaca paper account,
executes one full inverse-volatility rebalancing cycle with policy governance,
and outputs a clear, human-readable terminal summary of all risk checks,
allowed orders, blocked orders, resulting basket state, and audit trail verification.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure UTF-8 output streams across all platforms (especially Windows consoles)
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Ensure workspace root and src/ are in sys.path
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = WORKSPACE_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastmcp import Client, FastMCP
from rich.box import ROUNDED, SIMPLE
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import core_strategy
from broker_orders import (
    BrokerOrderReceipt,
    LifecycleJournal,
    parse_broker_order_result,
    poll_broker_order_terminal,
    recover_pending_order_events,
)
from cycle_control import (
    CycleAlreadyRunning,
    CycleLock,
    client_order_id,
    cycle_id_for_run,
    load_cycle_state,
    new_cycle_id,
    write_cycle_state,
)
from firewall import account_data
from firewall.audit import find_unresolved_pending, verify_chain
from firewall.market_data import fetch_daily_bars
from firewall.proxy import _default_policy_engine, build_proxy
from firewall.rules._util import matches_any
from firewall.rules.hedge_proposal import compute_scheduled_overlay

try:
    from narration.market_brief import build_narration_context_from_cache, get_latest_cached_brief, schedule_market_brief_generation, MarketBriefResult
except ImportError:
    try:
        from src.narration.market_brief import build_narration_context_from_cache, get_latest_cached_brief, schedule_market_brief_generation, MarketBriefResult
    except ImportError:
        build_narration_context_from_cache = None  # type: ignore
        schedule_market_brief_generation = None  # type: ignore
        get_latest_cached_brief = None  # type: ignore
        MarketBriefResult = None  # type: ignore


console = Console(highlight=False)


def _format_currency(val: float | None) -> str:
    if val is None:
        return "N/A"
    return f"${val:,.2f}"


def _format_pct(val: float | None) -> str:
    if val is None:
        return "N/A"
    return f"{val:.1%}"


def _count_stock_order_rules(engine: Any) -> tuple[int, int]:
    """(total configured rules, rules that gate on place_stock_order).

    Reads each rule's own declared tool-name matcher -- `stock_tool_match`,
    `place_tool_match`, or the generic `tool_match` (checked in that order
    of specificity), the same attributes and the same `matches_any`
    substring check the rules use internally to decide relevance -- rather
    than a hardcoded number that drifts from policies/default.yaml. A rule
    with none of those attributes (e.g. unrecognized_tool_catchall, which
    evaluates every tool_name against a whitelist; hedge_proposal, whose
    `check()` is an always-no-op by design -- see its module docstring)
    is counted as in-scope, since nothing restricts it away from stock
    orders.
    """
    total = len(engine.rules)
    stock_relevant = 0
    for rule in engine.rules:
        cfg = getattr(rule, "cfg", None)
        patterns = None
        if cfg is not None:
            patterns = (
                getattr(cfg, "stock_tool_match", None)
                or getattr(cfg, "place_tool_match", None)
                or getattr(cfg, "tool_match", None)
            )
        if patterns is None or matches_any("place_stock_order", patterns):
            stock_relevant += 1
    return total, stock_relevant


class HumanReadableCycleRunner:
    """Orchestrates and displays a full core_strategy rebalancing cycle."""

    def __init__(
        self,
        *,
        drift_threshold: float = core_strategy.DEFAULT_DRIFT_THRESHOLD,
        lookback_days: int = core_strategy.VOLATILITY_LOOKBACK_DAYS,
        include_options_overlay: bool = True,
        budget_override: float | None = None,
        verbose: bool = False,
        cycle_id: str | None = None,
        expected_account_id: str | None = None,
        dry_run: bool = True,
        lifecycle_poll_attempts: int = 3,
        recover_interrupted: bool = False,
    ) -> None:
        self.drift_threshold = drift_threshold
        self.lookback_days = lookback_days
        self.include_options_overlay = include_options_overlay
        self.budget_override = budget_override
        self.verbose = verbose
        self.cycle_id = cycle_id or new_cycle_id()
        self.expected_account_id = expected_account_id
        self.dry_run = dry_run
        self.lifecycle_poll_attempts = max(0, lifecycle_poll_attempts)
        self.recover_interrupted = recover_interrupted
        self.console = console

    def display_header(self, equity: float, session_pnl: float, policy_cfg: dict[str, Any]) -> None:
        """Display the initialization header and system status."""
        pnl_color = "green" if session_pnl >= 0 else "red"
        pnl_sign = "+" if session_pnl >= 0 else ""
        
        pos_cap_pct = policy_cfg.get("position_cap_max_pct_of_equity", 0.25)
        pos_cap_usd = equity * pos_cap_pct
        notional_cap_usd = policy_cfg.get("notional_cap_max_usd", equity * 0.05)
        max_rate = policy_cfg.get("order_rate_max_orders", 20)
        rate_window = policy_cfg.get("order_rate_window_seconds", 60.0)

        header_text = Text()
        header_text.append("[*] MCP TRADE FIREWALL -- LIVE DEMONSTRATION RUN\n", style="bold cyan")
        header_text.append("    Autonomous Inverse-Volatility Basket Rebalancing with Real-Time Governance\n\n", style="dim italic")
        
        header_text.append("[+] Environment: ", style="bold")
        header_text.append("Alpaca Paper Trading Account\n", style="green")
        
        header_text.append("[+] Account Equity: ", style="bold")
        header_text.append(f"{_format_currency(equity)}", style="bold white")
        header_text.append(" | Session P&L: ", style="bold")
        header_text.append(f"{pnl_sign}{_format_currency(session_pnl)}\n", style=pnl_color)
        
        header_text.append("[+] Target Allocation: ", style="bold")
        header_text.append("90% NAV in 11 Basket Equities, 10% Cash Reserve\n", style="white")

        header_text.append("[+] Active Firewall Limits: ", style="bold")
        header_text.append(f"Position Cap: {pos_cap_pct:.0%} ({_format_currency(pos_cap_usd)}) | ", style="yellow")
        header_text.append(f"Notional Cap: {_format_currency(notional_cap_usd)}/order | ", style="yellow")
        header_text.append(f"Rate Throttle: {max_rate} orders/{rate_window:.0f}s", style="yellow")

        panel = Panel(
            header_text,
            title="[bold green]System Status: Online & Connected[/bold green]",
            subtitle="[dim]Policy File: policies/default.yaml | Audit: audit.jsonl[/dim]",
            border_style="cyan",
            box=ROUNDED,
        )
        self.console.print(panel)
        self.console.print()

    async def execute_cycle(self) -> dict[str, Any]:
        """Execute one complete cycle and render human-readable terminal output."""
        # 1. Initialize engine and dynamic policy config
        engine = _default_policy_engine()
        proxy = build_proxy(policy_engine=engine, dry_run=self.dry_run)
        audit_writer = engine.audit_writer
        lifecycle_journal = LifecycleJournal()
        dynamic_cfg = core_strategy._read_dynamic_policy_config(engine)

        account_equity = dynamic_cfg.get("account_equity")
        pnl_res = account_data.fetch_session_pnl()
        if not pnl_res.ok or account_equity is None:
            self.console.print(
                f"[bold red][X] Failed to fetch live account equity: "
                f"{escape(str(pnl_res.reason))}[/bold red]"
            )
            return {"ok": False, "reason": pnl_res.reason}
        if self.expected_account_id and pnl_res.account_id != self.expected_account_id:
            actual = pnl_res.account_id or "unavailable"
            self.console.print(
                "[bold red][X] Account identity mismatch; refusing all submissions. "
                f"Expected {self.expected_account_id}, received {actual}.[/bold red]"
            )
            return {"ok": False, "reason": "paper account identity mismatch"}

        session_pnl = pnl_res.session_pnl_usd or 0.0
        self.display_header(account_equity, session_pnl, dynamic_cfg)

        # Sizing thresholds
        base_budget_usd = self.budget_override or core_strategy.compute_total_budget_usd(
            account_equity, core_strategy.DEFAULT_BASKET_PCT_OF_NAV
        )
        total_budget_usd = base_budget_usd
        pos_cap_pct = dynamic_cfg.get("position_cap_max_pct_of_equity", 0.25)
        notional_cap_ceiling = dynamic_cfg.get("notional_cap_max_usd", account_equity * 0.05)
        order_rate_max_orders = dynamic_cfg.get("order_rate_max_orders", core_strategy.DEFAULT_ORDER_RATE_MAX_ORDERS)
        order_rate_window_seconds = dynamic_cfg.get("order_rate_window_seconds", core_strategy.DEFAULT_ORDER_RATE_WINDOW_SECONDS)

        # 2. Step 1: Market Data & Volatility Pipeline
        self.console.print("[bold cyan]================================================================================[/bold cyan]")
        self.console.print("[bold yellow]STEP 1: Market Data & Inverse-Volatility Sizing[/bold yellow]")
        self.console.print(
            "Fetching historical daily price history to compute trailing 90-day realized volatility.\n"
            "Positions are sized inversely proportional to volatility so each asset contributes equal risk.",
            style="dim",
        )
        self.console.print()

        prices: dict[str, float] = {}
        volatilities: dict[str, float] = {}
        failed_symbols: set[str] = set()

        for symbol in core_strategy.BASKET:
            bars = fetch_daily_bars(symbol, self.lookback_days)
            if not bars.ok or not bars.closes:
                failed_symbols.add(symbol)
                continue
            prices[symbol] = bars.closes[-1]
            try:
                volatilities[symbol] = core_strategy.compute_realized_volatility(bars.closes)
            except Exception:
                failed_symbols.add(symbol)

        priced_basket = tuple(s for s in core_strategy.BASKET if s not in failed_symbols)
        exposure_snapshot = account_data.fetch_consistent_exposure_snapshot(prices)
        if not exposure_snapshot.get("ok"):
            reason = exposure_snapshot.get("reason") or "open-order exposure is unavailable"
            self.console.print(
                "[bold red][X] OPEN-ORDER RECONCILIATION FAILED CLOSED:[/bold red] "
                f"{reason}. No cycle orders were sized or submitted."
            )
            if audit_writer is not None:
                audit_writer.append(
                    tool_name="cycle_start:open_order_reconciliation",
                    arguments={},
                    verdict="hard_block",
                    reason=reason,
                    forwarded=False,
                    upstream_status="not_forwarded",
                    rule_id="open-order-reconciliation",
                    regulation_ref=None,
                )
            return {"ok": False, "reason": reason, "fail_closed": True}

        target_weights = core_strategy.compute_inverse_vol_weights(volatilities)
        
        # Position Cap Clipping
        raw_weights = dict(target_weights)
        target_weights, weight_clips = core_strategy.clip_weights_to_position_cap(
            target_weights, total_budget_usd, account_equity, pos_cap_pct
        )
        for clip in weight_clips.values():
            if audit_writer is not None:
                audit_writer.append(
                    tool_name="basket_rebalance:weight_clipped",
                    arguments={
                        "symbol": clip.symbol,
                        "raw_target_weight": clip.raw_weight,
                        "capped_target_weight": clip.capped_weight,
                        "ceiling_usd": clip.ceiling_usd,
                        "total_budget_usd": total_budget_usd,
                    },
                    verdict="info",
                    reason=(
                        f"{clip.symbol}'s raw target {clip.raw_weight:.1%} exceeds position cap "
                        f"{pos_cap_pct:.1%}. Clipped to {clip.capped_weight:.1%}; remainder held as cash."
                    ),
                    forwarded=None,
                    upstream_status="not_forwarded",
                    rule_id="basket-rebalance-position-cap-clip",
                    regulation_ref=None,
                )

        target_quantities = core_strategy.compute_target_quantities(target_weights, prices, total_budget_usd)

        # Market Data Table
        vol_table = Table(title="11-Asset Inverse-Volatility Basket Allocation", box=SIMPLE)
        vol_table.add_column("Symbol", style="bold cyan")
        vol_table.add_column("Price", justify="right")
        vol_table.add_column("90D Vol", justify="right")
        vol_table.add_column("Raw Wt", justify="right")
        vol_table.add_column("Capped Wt", justify="right")
        vol_table.add_column("Target Budget", justify="right")
        vol_table.add_column("Target Qty", justify="right", style="bold")
        vol_table.add_column("Risk Note", style="dim")

        for sym in priced_basket:
            p = prices[sym]
            v = volatilities[sym]
            rw = raw_weights.get(sym, 0.0)
            cw = target_weights.get(sym, 0.0)
            tb = total_budget_usd * cw
            tq = target_quantities.get(sym, 0)
            note = f"Clipped from {rw:.1%}" if sym in weight_clips else "Within position cap"
            vol_table.add_row(
                sym,
                _format_currency(p),
                f"{v:.2%}",
                f"{rw:.1%}",
                f"{cw:.1%}",
                _format_currency(tb),
                f"{tq} shares",
                note,
            )
        self.console.print(vol_table)
        self.console.print()

        # 3. Step 2: Query Holdings and Evaluate Drift
        self.console.print("[bold cyan]================================================================================[/bold cyan]")
        self.console.print("[bold yellow]STEP 2: Portfolio Drift & Rebalancing Decisions[/bold yellow]")
        self.console.print(
            f"Comparing current paper account holdings to targets (Drift threshold = {self.drift_threshold:.1%}).\n"
            "Orders are only generated when portfolio allocation has drifted beyond threshold.",
            style="dim",
        )
        self.console.print()

        async with Client(proxy) as client:
            if self.recover_interrupted:
                pending_events = find_unresolved_pending(
                    audit_writer.log_path, stale_after_seconds=0
                )
                recovered = await recover_pending_order_events(
                    client,
                    pending_events,
                    journal=lifecycle_journal,
                    max_attempts=self.lifecycle_poll_attempts,
                    poll_interval_seconds=0.5,
                )
                unresolved_count = sum(
                    receipt.submitted and receipt.status not in {
                        "canceled", "done_for_day", "expired", "filled", "rejected", "stopped"
                    }
                    for receipt in recovered
                )
                self.console.print(
                    f"[bold cyan]Recovery-only pass complete:[/bold cyan] "
                    f"{len(recovered)} orphaned order call(s) checked; "
                    f"{unresolved_count} remain non-terminal. No replacement orders were generated."
                )
                return {
                    "ok": unresolved_count == 0,
                    "recovery_only": True,
                    "recovered_count": len(recovered),
                    "unresolved_count": unresolved_count,
                }
            current_positions = {
                symbol: exposure_snapshot["positions"].get(symbol, 0.0)
                for symbol in core_strategy.BASKET
            }
            pending_by_symbol = exposure_snapshot["pending_signed_qty"]
            committed_positions = {
                symbol: current_positions.get(symbol, 0.0) + pending_by_symbol.get(symbol, 0.0)
                for symbol in core_strategy.BASKET
            }
            self.console.print(
                "[bold cyan]Open-order reconciliation:[/bold cyan] "
                f"{len(exposure_snapshot['open_orders'])} outstanding order(s) | aggregate unfilled notional "
                f"{_format_currency(exposure_snapshot['aggregate_outstanding_notional'])} | "
                "pending quantities included in cycle sizing"
            )
            order_deltas = core_strategy.compute_rebalance_orders(
                current_positions=committed_positions,
                target_quantities=target_quantities,
                target_weights=target_weights,
                prices=prices,
                drift_threshold=self.drift_threshold,
                reference_value_usd=total_budget_usd,
            )
            # The strategy submits whole shares. Truncation toward zero is
            # conservative when an external fractional open order exists:
            # it cannot push the combined filled+pending position past target.
            order_deltas = {symbol: int(delta) for symbol, delta in order_deltas.items()}

            drift_table = Table(title="Position Drift vs. Target Allocation", box=SIMPLE)
            drift_table.add_column("Symbol", style="bold cyan")
            drift_table.add_column("Current Holdings", justify="right")
            drift_table.add_column("Target Holdings", justify="right")
            drift_table.add_column("Order Delta", justify="right", style="bold")
            drift_table.add_column("Estimated Value", justify="right")
            drift_table.add_column("Action Decision", style="bold")

            rebalance_needed_count = 0
            for sym in priced_basket:
                curr_q = current_positions.get(sym, 0)
                targ_q = target_quantities.get(sym, 0)
                delta = order_deltas.get(sym, 0)
                p = prices[sym]
                val = abs(delta) * p

                if delta == 0:
                    action = Text(
                        f"HOLD (Drift within ±{self.drift_threshold:.1%})",
                        style="green",
                    )
                elif delta > 0:
                    action = Text(f"BUY +{delta} sh", style="bold green")
                    rebalance_needed_count += 1
                else:
                    action = Text(f"SELL {delta} sh", style="bold yellow")
                    rebalance_needed_count += 1

                drift_table.add_row(
                    sym,
                    f"{curr_q} sh ({_format_currency(curr_q * p)})",
                    f"{targ_q} sh ({_format_currency(targ_q * p)})",
                    f"{'+' if delta > 0 else ''}{delta} sh",
                    _format_currency(val) if delta != 0 else "$0.00",
                    action,
                )
            self.console.print(drift_table)
            self.console.print()

            # 4. Step 3: Order Execution & Policy Governance
            total_rules, stock_relevant_rules = _count_stock_order_rules(engine)
            self.console.print("[bold cyan]================================================================================[/bold cyan]")
            self.console.print("[bold yellow]STEP 3: Live Firewall Policy Enforcement & Order Execution[/bold yellow]")
            self.console.print(
                "Submitting proposed rebalance orders through the MCP proxy firewall.\n"
                f"Each order is pre-trade evaluated against {stock_relevant_rules} of the firewall's "
                f"{total_rules} configured policy rules (the remainder gate options or bulk-cancel calls only).",
                style="dim",
            )
            self.console.print()

            pacer = core_strategy.ThrottlePacer(order_rate_max_orders, order_rate_window_seconds)
            attempts: list[core_strategy.OrderAttempt] = []
            executed_orders: list[dict[str, Any]] = []
            blocked_orders: list[dict[str, Any]] = []
            option_orders: list[dict[str, Any]] = []
            submission_sequence = 0

            for symbol in priced_basket:
                delta = order_deltas.get(symbol, 0)
                price = prices[symbol]
                curr_qty = current_positions.get(symbol, 0)
                target_qty = target_quantities.get(symbol, 0)
                weight = target_weights.get(symbol, 0.0)

                if delta == 0:
                    attempts.append(
                        core_strategy.OrderAttempt(
                            symbol=symbol,
                            qty=0,
                            price=price,
                            forwarded=True,
                            detail=f"no rebalance needed for {symbol}: drift within threshold",
                        )
                    )
                    continue

                side = "buy" if delta > 0 else "sell"
                order_qty = abs(delta)
                chunk_sizes = core_strategy.split_order_into_chunks(order_qty, price, notional_cap_ceiling)

                if len(chunk_sizes) > 1:
                    self.console.print(
                        f"  [Chunking Notice] [bold yellow]{symbol}[/bold yellow]: Order of {order_qty} shares (~{_format_currency(order_qty * price)}) "
                        f"exceeds per-order notional cap ({_format_currency(notional_cap_ceiling)}). "
                        f"Split into [bold]{len(chunk_sizes)} chunks[/bold] ({', '.join(str(c) for c in chunk_sizes)} sh)."
                    )
                    if audit_writer is not None:
                        audit_writer.append(
                            tool_name="basket_rebalance:order_chunked",
                            arguments={
                                "symbol": symbol,
                                "side": side,
                                "total_qty": order_qty,
                                "total_notional": order_qty * price,
                                "chunk_count": len(chunk_sizes),
                                "chunk_sizes": chunk_sizes,
                                "per_chunk_ceiling_usd": notional_cap_ceiling,
                            },
                            verdict="info",
                            reason=f"Chunked into {len(chunk_sizes)} orders to satisfy notional_cap limit.",
                            forwarded=None,
                            upstream_status="not_forwarded",
                            rule_id="basket-rebalance-chunking",
                            regulation_ref=None,
                        )

                for chunk_index, chunk_qty in enumerate(chunk_sizes, start=1):
                    submission_sequence += 1
                    chunk_label = f" (chunk {chunk_index}/{len(chunk_sizes)})" if len(chunk_sizes) > 1 else ""
                    order_notional = chunk_qty * price
                    order_client_id = client_order_id(
                        self.cycle_id, symbol, side, submission_sequence
                    )
                    payload = core_strategy.build_market_order_payload(
                        symbol, chunk_qty, side=side, client_order_id=order_client_id
                    )
                    payload["_firewall_reconciliation"] = {
                        "target_qty": target_qty,
                    }

                    if lifecycle_journal.unresolved(order_client_id):
                        self.console.print(
                            f"    [bold yellow]DUPLICATE SUPPRESSED[/bold yellow]: "
                            f"{order_client_id} still has a non-terminal broker lifecycle"
                        )
                        attempts.append(core_strategy.OrderAttempt(
                            symbol=symbol, qty=chunk_qty, price=price, forwarded=False,
                            detail=f"duplicate suppressed for unresolved {order_client_id}",
                        ))
                        continue

                    self.console.print(
                        f"  -> [bold white]Evaluating Order[/bold white]: {side.upper()} {chunk_qty} {symbol}{chunk_label} "
                        f"@ ~{_format_currency(price)} ({_format_currency(order_notional)})"
                    )

                    if not self.dry_run:
                        await pacer.before_submit()
                    try:
                        result = await client.call_tool(core_strategy.symbol_tool_name(), payload, raise_on_error=False)
                    except Exception as exc:
                        self.console.print(
                            f"    [bold red]Transport Error:[/bold red] {escape(str(exc))}"
                        )
                        attempts.append(
                            core_strategy.OrderAttempt(
                                symbol=symbol,
                                qty=chunk_qty,
                                price=price,
                                forwarded=False,
                                detail=f"transport error: {exc}",
                            )
                        )
                        continue

                    if getattr(result, "is_error", False):
                        # Extract blocking reason
                        error_text = ""
                        if result.content:
                            error_text = getattr(result.content[0], "text", str(result))
                        
                        # Parse firing rule name and clean message
                        rule_name = "policy_rule"
                        clean_reason = error_text
                        if "BLOCKED by rule '" in error_text:
                            parts = error_text.split("BLOCKED by rule '", 1)[1].split("':", 1)
                            if len(parts) == 2:
                                rule_name = parts[0]
                                clean_reason = parts[1].strip()

                        self.console.print(
                            f"    [bold red][X] BLOCKED BY FIREWALL RULE: "
                            f"'{escape(str(rule_name))}'[/bold red]\n"
                            f"       [yellow]Reason:[/yellow] {escape(str(clean_reason))}\n"
                            f"       [dim]Protection Note: This block is the policy engine working as designed to prevent risk violation.[/dim]"
                        )
                        blocked_orders.append({
                            "symbol": symbol,
                            "side": side,
                            "qty": chunk_qty,
                            "price": price,
                            "rule": rule_name,
                            "reason": clean_reason,
                        })
                        attempts.append(
                            core_strategy.OrderAttempt(
                                symbol=symbol,
                                qty=chunk_qty,
                                price=price,
                                forwarded=False,
                                detail=f"blocked by {rule_name}: {clean_reason}",
                            )
                        )
                    else:
                        receipt = parse_broker_order_result(result)
                        if receipt.client_order_id is None:
                            receipt = replace(receipt, client_order_id=order_client_id)
                        lifecycle_journal.record(
                            receipt, cycle_id=self.cycle_id, symbol=symbol, side=side,
                            quantity=chunk_qty,
                        )
                        call_ts = datetime.now(timezone.utc).isoformat()
                        if receipt.order_id:
                            identity_line = (
                                f"Broker status: {receipt.status} | Order ID: {receipt.order_id}"
                            )
                        else:
                            identity_line = (
                                f"Broker status: {receipt.status} (no parseable order ID) | "
                                f"Audit correlation: {call_ts} {side.upper()} {chunk_qty} {symbol}"
                            )

                        outcome_label = (
                            "DRY RUN: FIREWALL ALLOWED; UPSTREAM MUTATION SUPPRESSED"
                            if receipt.status == "dry_run"
                            else "FIREWALL ALLOWED & BROKER SUBMISSION RETURNED"
                        )
                        self.console.print(
                            f"    [bold green][OK] {outcome_label}[/bold green]\n"
                            f"       [dim]{identity_line}[/dim]"
                        )
                        for item in getattr(result, "content", []) or []:
                            note = getattr(item, "text", "")
                            if isinstance(note, str) and note.startswith(
                                "DELEVERAGING_EXCEPTION_ALLOW:"
                            ):
                                self.console.print(
                                    "       [bold cyan]Killswitch exception:[/bold cyan] "
                                    f"{escape(note)}"
                                )
                        executed_orders.append({
                            "symbol": symbol,
                            "side": side,
                            "qty": chunk_qty,
                            "price": price,
                            "broker": receipt.to_dict(),
                            "client_order_id": order_client_id,
                            "timestamp": call_ts,
                        })
                        attempts.append(
                            core_strategy.OrderAttempt(
                                symbol=symbol,
                                qty=chunk_qty,
                                price=price,
                                forwarded=receipt.submitted,
                                detail=(
                                    f"{'submitted' if receipt.submitted else 'simulated'} {side} order for {chunk_qty} sh of {symbol}; "
                                    f"broker status={receipt.status}, filled={receipt.filled}"
                                ),
                            )
                        )

            # 5. Step 4: Scheduled Options Overlay
            effective_overlay = self.include_options_overlay
            if effective_overlay:
                self.console.print()
                self.console.print("[bold cyan]================================================================================[/bold cyan]")
                self.console.print("[bold yellow]STEP 4: Scheduled Options Overlay (Standing Portfolio Insurance)[/bold yellow]")
                self.console.print(
                    "Evaluating standing protective put option overlay on largest basket position.\n"
                    "Subject to option spread guard, hedge cost cap (max 2% NAV), and delta floor.",
                    style="dim",
                )
                self.console.print()

                overlay_positions = dict(current_positions)
                for sym, delta in order_deltas.items():
                    overlay_positions[sym] = overlay_positions.get(sym, 0) + delta

                overlay = compute_scheduled_overlay(overlay_positions, prices)
                if overlay is not None:
                    self.console.print(
                        f"  [+] [bold white]Proposed Overlay:[/bold white] BUY {overlay.contracts} PUT contract(s) on "
                        f"[bold cyan]{overlay.symbol}[/bold cyan] ({overlay.occ_symbol})\n"
                        f"      Strike: {_format_currency(overlay.strike)} | Expiry: {overlay.target_expiry} | Rationale: {overlay.reason}"
                    )
                    
                    if audit_writer is not None:
                        audit_writer.append(
                            tool_name="scheduled_overlay:proposed",
                            arguments={
                                "symbol": overlay.symbol,
                                "occ_symbol": overlay.occ_symbol,
                                "contracts": overlay.contracts,
                                "strike": overlay.strike,
                                "target_expiry": overlay.target_expiry,
                            },
                            verdict="info",
                            reason=overlay.reason,
                            forwarded=None,
                            upstream_status="not_forwarded",
                            rule_id="scheduled-options-overlay",
                            regulation_ref=None,
                        )

                    submission_sequence += 1
                    option_client_id = client_order_id(
                        self.cycle_id, overlay.occ_symbol, "buy", submission_sequence
                    )
                    option_payload = core_strategy.build_option_order_payload(
                        overlay.occ_symbol, overlay.contracts, side="buy",
                        client_order_id=option_client_id,
                    )
                    if not self.dry_run:
                        await pacer.before_submit()
                    try:
                        res = await client.call_tool(core_strategy.option_tool_name(), option_payload, raise_on_error=False)
                        if getattr(res, "is_error", False):
                            err_txt = getattr(res.content[0], "text", str(res)) if res.content else str(res)
                            self.console.print(
                                f"    [bold yellow]Option Order Filtered / Blocked:[/bold yellow] "
                                f"{escape(str(err_txt))}\n"
                                f"    [dim](Standing insurance proposal recorded in audit log; options trading restrictions applied).[/dim]"
                            )
                        else:
                            receipt = parse_broker_order_result(res)
                            if receipt.client_order_id is None:
                                receipt = replace(receipt, client_order_id=option_client_id)
                            lifecycle_journal.record(
                                receipt, cycle_id=self.cycle_id,
                                symbol=overlay.occ_symbol, side="buy",
                                quantity=overlay.contracts,
                            )
                            option_orders.append({
                                "underlying": overlay.symbol,
                                "occ_symbol": overlay.occ_symbol,
                                "side": "buy",
                                "contracts": overlay.contracts,
                                "strike": overlay.strike,
                                "expiry": overlay.target_expiry,
                                "resolved_delta": getattr(overlay, "delta", None),
                                "broker": receipt.to_dict(),
                                "client_order_id": option_client_id,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            })
                            self.console.print(
                                f"    [bold green][OK] OPTION ORDER ALLOWED & SUBMITTED[/bold green]: "
                                f"{overlay.occ_symbol} | broker status: "
                                f"{escape(str(receipt.status))} | "
                                f"order ID: {escape(str(receipt.order_id or 'unavailable'))} | "
                                f"filled: {receipt.filled}"
                            )
                    except Exception as exc:
                        self.console.print(
                            f"    [dim]Options transport note: {escape(str(exc))}[/dim]"
                        )
                else:
                    self.console.print("  [dim]No position met threshold for scheduled options overlay this cycle.[/dim]")

            # One read-only broker reconciliation pass. Submission success is
            # never labeled as a fill unless Alpaca's latest order state says so.
            for submitted in [*executed_orders, *option_orders]:
                raw = submitted["broker"]
                receipt = BrokerOrderReceipt(**raw)
                refreshed = await poll_broker_order_terminal(
                    client,
                    receipt,
                    max_attempts=self.lifecycle_poll_attempts,
                    poll_interval_seconds=0.5,
                    journal=lifecycle_journal,
                    context={
                        "cycle_id": self.cycle_id,
                        "symbol": submitted.get("symbol") or submitted.get("occ_symbol"),
                        "side": submitted.get("side"),
                        "quantity": submitted.get("qty") or submitted.get("contracts"),
                    },
                )
                submitted["broker"] = refreshed.to_dict()
                if refreshed.order_id:
                    self.console.print(
                        f"[dim]Broker reconciliation {refreshed.order_id}: "
                        f"{refreshed.status}, filled_qty={refreshed.filled_qty or 0:g}, "
                        f"avg_fill={_format_currency(refreshed.filled_avg_price)}[/dim]"
                    )

            # 6. Step 5: Final Resulting Basket State & Audit Summary
            self.console.print()
            self.console.print("[bold cyan]================================================================================[/bold cyan]")
            self.console.print("[bold yellow]STEP 5: Resulting Basket State & Audit Verification[/bold yellow]")
            self.console.print()

            # Refresh positions after orders
            final_position_state = account_data.fetch_positions(cache_ttl_seconds=0)
            final_positions_verified = bool(
                final_position_state.ok and final_position_state.quantities is not None
            )
            updated_positions = (
                {symbol: final_position_state.quantities.get(symbol, 0.0) for symbol in core_strategy.BASKET}
                if final_positions_verified
                else dict(current_positions)
            )
            if not final_positions_verified:
                self.console.print(
                    "[bold red]FINAL POSITION STATE UNVERIFIED:[/bold red] "
                    f"{final_position_state.reason or 'broker quantities unavailable'}. "
                    "Starting holdings are shown only as a placeholder, not as confirmed final holdings."
                )
            summary_table = Table(title="Final Basket Position State", box=ROUNDED)
            summary_table.add_column("Symbol", style="bold cyan")
            summary_table.add_column("Starting", justify="right")
            summary_table.add_column("Trades Executed", justify="right")
            summary_table.add_column("Final Holding", justify="right", style="bold")
            summary_table.add_column("Market Value", justify="right")
            summary_table.add_column("Portfolio %", justify="right")
            summary_table.add_column("Status / Governance", style="white")

            for sym in priced_basket:
                start_q = current_positions.get(sym, 0)
                fin_q = updated_positions.get(sym, 0)
                p = prices[sym]
                val = fin_q * p
                pct = val / account_equity if account_equity > 0 else 0.0
                
                # Check status
                sym_executed = [
                    e for e in executed_orders
                    if e["symbol"] == sym and e["broker"].get("submitted")
                ]
                sym_simulated = [
                    e for e in executed_orders
                    if e["symbol"] == sym and not e["broker"].get("submitted")
                ]
                sym_blocked = [b for b in blocked_orders if b["symbol"] == sym]
                
                if sym_executed:
                    signed_qty = sum(
                        e["qty"] if e["side"] == "buy" else -e["qty"] for e in sym_executed
                    )
                    trades_str = f"{signed_qty:+d} sh"
                    gov_status = "[bold green]Submitted[/bold green]"
                elif sym_simulated:
                    signed_qty = sum(
                        e["qty"] if e["side"] == "buy" else -e["qty"]
                        for e in sym_simulated
                    )
                    trades_str = f"{signed_qty:+d} sh (Simulated)"
                    gov_status = "[bold yellow]Dry Run — Not Submitted[/bold yellow]"
                elif sym_blocked:
                    trades_str = "0 sh (Blocked)"
                    gov_status = (
                        f"[bold red]Cap Protected "
                        f"({escape(str(sym_blocked[0]['rule']))})[/bold red]"
                    )
                else:
                    trades_str = "0 sh"
                    gov_status = "[green]In Target Range[/green]"

                summary_table.add_row(
                    sym,
                    f"{start_q} sh",
                    trades_str,
                    f"{fin_q} sh",
                    _format_currency(val),
                    f"{pct:.1%}",
                    gov_status,
                )

            self.console.print(summary_table)
            self.console.print()

            # STEP 6: AI Market Commentary (Informational, generated by Featherless)
            if schedule_market_brief_generation is not None:
                self.console.print("=" * 80)
                self.console.print("[bold cyan]STEP 6: AI Market Commentary (Informational / Non-Trading)[/bold cyan]")
                self.console.print("[dim]Queued a non-blocking periodic commentary refresh; showing the latest cached result.[/dim]\n")
                try:
                    # Same core_strategy.compute_weights_and_drift the dashboard's
                    # periodic refresh uses, fed the same kind of inputs (a
                    # per-symbol USD value + target weights) -- one shared
                    # source of "current basket state," not two independently
                    # computed versions of it.
                    final_values_usd = {
                        s: updated_positions.get(s, 0) * prices.get(s, 0.0) for s in priced_basket
                    }
                    weights_and_drift = core_strategy.compute_weights_and_drift(
                        final_values_usd, target_weights
                    )
                    basket_state = {
                            "weights": weights_and_drift,
                            "cash_w": max(0.0, 1.0 - sum(target_weights.values())),
                    }
                    context_data = build_narration_context_from_cache(basket_state)
                    schedule_market_brief_generation(context_data)
                    brief = get_latest_cached_brief()
                    comm_box = Text()
                    time_display = brief.timestamp[11:19] if len(brief.timestamp) >= 19 else "Just now"
                    comm_box.append(f"Model: {brief.model} | Latency: {brief.latency_seconds:.1f}s | Updated: {time_display} UTC\n\n", style="bold #38bdf8")
                    comm_box.append(f'"{brief.text}"\n\n', style="white")
                    comm_box.append("[Notice] Strictly educational/monitoring text. Zero tool-call access; zero feedback into trading or risk decisions.", style="dim italic #64748b")
                    self.console.print(
                        Panel(
                            comm_box,
                            title="[bold #38bdf8]AI COMMENTARY (informational, generated by Featherless, not a trading input)[/bold #38bdf8]",
                            box=ROUNDED,
                            border_style="#0284c7",
                        )
                    )
                    self.console.print()
                except Exception as exc:
                    self.console.print(
                        f"[dim yellow]AI commentary standby ({escape(str(exc))}) -- "
                        "trading pipeline running normally.[/dim yellow]\n"
                    )

            # Audit Trail Verification
            audit_path = Path("audit.jsonl")
            chain_ok = False
            audit_entries_count = 0
            if audit_path.exists():
                lines = [l for l in audit_path.read_text(encoding="utf-8").splitlines() if l.strip()]
                audit_entries_count = len(lines)
                try:
                    chain_ok = verify_chain(audit_path)
                except Exception:
                    chain_ok = False

            audit_box = Text()
            audit_box.append("[*] AUDIT TRAIL VERIFICATION\n", style="bold cyan")
            audit_box.append(f"  * Audit Log File: {audit_path.resolve()}\n", style="white")
            audit_box.append(f"  * Total Logged Events: {audit_entries_count} records\n", style="white")
            audit_box.append(f"  * SHA-256 Tamper-Evident Chain: ", style="white")
            if chain_ok:
                audit_box.append("VALID & VERIFIED (Cryptographically sealed)\n", style="bold green")
            else:
                audit_box.append("WARNING (Check format)\n", style="bold yellow")
            audit_box.append(f"  * Timestamp (UTC): {datetime.now(timezone.utc).isoformat()}\n\n", style="dim")
            audit_box.append("Independent Verification Steps:\n", style="bold yellow")
            audit_box.append("1. Inspect recent events: tail -n 10 audit.jsonl\n", style="dim")
            audit_box.append("2. Open Live Terminal Dashboard: python dashboard/app.py\n", style="dim")
            audit_box.append(
                "3. Confirm on Alpaca's own order history (https://app.alpaca.markets/paper/dashboard/overview) "
                "by matching timestamp + symbol + qty against the lines above and audit.jsonl -- \n"
                "   this system does not surface a broker order ID, so match on those fields instead.\n",
                style="dim",
            )

            self.console.print(Panel(audit_box, title="[bold green]Execution Cycle Complete[/bold green]", box=ROUNDED, border_style="green"))

            cycle_finished_at = datetime.now(timezone.utc)
            weights_and_drift = core_strategy.compute_weights_and_drift(
                {s: updated_positions.get(s, 0) * prices.get(s, 0.0) for s in priced_basket},
                target_weights,
                total_budget_usd,
            )
            cycle_record = {
                "cycle_id": self.cycle_id,
                "execution_mode": "dry_run" if self.dry_run else "paper",
                "finished_at": cycle_finished_at.isoformat(),
                "account_equity": account_equity,
                "session_pnl_usd": session_pnl,
                "budget_usd": total_budget_usd,
                "position_outcome_verification": {
                    "verified": final_positions_verified,
                    "reason": None if final_positions_verified else final_position_state.reason,
                },
                "open_order_reconciliation": {
                    "count": len(exposure_snapshot["open_orders"]),
                    "aggregate_outstanding_notional": exposure_snapshot["aggregate_outstanding_notional"],
                    "pending_signed_qty_by_symbol": pending_by_symbol,
                    "snapshot_fingerprint": exposure_snapshot["fingerprint"],
                },
                "prices": prices,
                "realized_volatility": volatilities,
                "target_weights": target_weights,
                "weights_and_drift": weights_and_drift,
                "submitted_stock_orders": executed_orders,
                "submitted_option_orders": option_orders,
                "blocked_orders": blocked_orders,
            }
            cycles_dir = WORKSPACE_ROOT / "data" / "cycles"
            cycles_dir.mkdir(parents=True, exist_ok=True)
            cycle_path = cycles_dir / f"{cycle_record['cycle_id']}.json"
            cycle_path.write_text(json.dumps(cycle_record, indent=2), encoding="utf-8")
            self.console.print(
                f"[dim]Cycle execution artifact: {cycle_path}[/dim]"
            )

        return {
            "ok": final_positions_verified,
            "reason": None if final_positions_verified else (
                final_position_state.reason or "post-cycle position outcome is unverified"
            ),
            "equity": account_equity,
            "executed_count": sum(1 for order in executed_orders if order["broker"]["filled"]),
            "submitted_count": sum(
                bool(order["broker"].get("submitted"))
                for order in [*executed_orders, *option_orders]
            ),
            "blocked_count": len(blocked_orders),
            "reconciliation_status": "verified",
            "position_outcome_verified": final_positions_verified,
            "attempts": attempts,
            "cycle_record": str(cycle_path),
        }


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one full end-to-end cycle of the trading system with firewall governance.")
    parser.add_argument("--budget", type=float, default=None, help="Explicit total budget USD override")
    parser.add_argument("--drift-threshold", type=float, default=core_strategy.DEFAULT_DRIFT_THRESHOLD, help="Drift threshold (default: 0.05)")
    parser.add_argument("--no-overlay", action="store_true", help="Disable scheduled options overlay")
    parser.add_argument("--verbose", action="store_true", help="Show raw RPC logs on stderr")
    parser.add_argument(
        "--expected-account-id",
        default=os.environ.get("ALPACA_EXPECTED_ACCOUNT_ID"),
        help="refuse submissions unless Alpaca returns this exact paper account ID",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="read and validate paper account state without constructing an order cycle",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--execute",
        action="store_true",
        help="enable paper submissions; requires an exact expected account ID",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="explicitly select the default safe mode; all upstream mutations are suppressed",
    )
    parser.add_argument(
        "--lifecycle-poll-attempts",
        type=int,
        default=3,
        help="bounded broker status refresh attempts per submitted order (default: 3)",
    )
    parser.add_argument(
        "--recover", action="store_true",
        help="explicitly acknowledge and recover an interrupted prior cycle",
    )
    args = parser.parse_args(argv)
    dry_run = not args.execute

    if args.execute and not args.expected_account_id:
        console.print(
            "[bold red]Execution refused: --execute requires --expected-account-id "
            "or ALPACA_EXPECTED_ACCOUNT_ID.[/bold red]"
        )
        return 2

    if args.preflight_only:
        pnl = account_data.fetch_session_pnl(cache_ttl_seconds=0)
        positions = account_data.fetch_positions(cache_ttl_seconds=0)
        if not pnl.ok:
            console.print(
                f"[bold red]Paper-account preflight failed: "
                f"{escape(str(pnl.reason))}[/bold red]"
            )
            return 1
        if args.expected_account_id and pnl.account_id != args.expected_account_id:
            console.print("[bold red]Paper-account preflight failed: account ID mismatch.[/bold red]")
            return 1
        account_label = pnl.account_id or "unavailable"
        masked = account_label if len(account_label) <= 8 else f"{account_label[:4]}...{account_label[-4:]}"
        position_count = len(positions.positions or {}) if positions.ok else "unavailable"
        console.print(
            "[bold green]Paper-account preflight passed[/bold green] | "
            f"account={masked} | equity={_format_currency(pnl.equity)} | "
            f"positions={position_count} | submissions=DISABLED"
        )
        return 0

    prior_state = load_cycle_state()
    cycle_id, recovering = cycle_id_for_run(prior_state, args.recover)
    lock = CycleLock(cycle_id)
    try:
        lock.acquire()
    except CycleAlreadyRunning as exc:
        console.print(
            f"[bold red]Refusing overlapping run: {escape(str(exc))}[/bold red]"
        )
        return 1
    if prior_state and prior_state.get("status") in {"starting", "running"} and not args.recover:
        console.print(
            "[bold red]Refusing automatic restart: the prior cycle ended without a terminal "
            "state. Reconcile Alpaca orders, then rerun with --recover.[/bold red]"
        )
        lock.release()
        return 1
    write_cycle_state(cycle_id, "starting")

    # Suppress verbose RPC logs and warnings unless --verbose is given
    import warnings
    warnings.filterwarnings("ignore")

    try:
        if not args.verbose:
            from contextlib import redirect_stderr
            logging.getLogger("fastmcp").setLevel(logging.CRITICAL)
            logging.getLogger("mcp").setLevel(logging.CRITICAL)
            with open(os.devnull, "w", encoding="utf-8") as devnull, redirect_stderr(devnull):
                runner = HumanReadableCycleRunner(
                    budget_override=args.budget,
                    drift_threshold=args.drift_threshold,
                    include_options_overlay=not args.no_overlay,
                    verbose=args.verbose,
                    cycle_id=cycle_id,
                    expected_account_id=args.expected_account_id,
                    dry_run=dry_run,
                    lifecycle_poll_attempts=args.lifecycle_poll_attempts,
                    recover_interrupted=recovering,
                )
                result = await runner.execute_cycle()
        else:
            runner = HumanReadableCycleRunner(
                budget_override=args.budget,
                drift_threshold=args.drift_threshold,
                include_options_overlay=not args.no_overlay,
                verbose=args.verbose,
                cycle_id=cycle_id,
                expected_account_id=args.expected_account_id,
                dry_run=dry_run,
                lifecycle_poll_attempts=args.lifecycle_poll_attempts,
                recover_interrupted=recovering,
            )
            result = await runner.execute_cycle()
        status = "completed" if result.get("ok") else "failed"
        write_cycle_state(cycle_id, status, details={"result": result})
        return 0 if result.get("ok") else 1
    except Exception as exc:
        write_cycle_state(cycle_id, "failed", details={"error": repr(exc)})
        console.print(
            f"[bold red]Cycle failed during initialization or execution: "
            f"{escape(str(exc))}[/bold red]"
        )
        return 1
    finally:
        lock.release()


def main() -> None:
    # Silence asyncio proactor pipe cleanup notices during Python interpreter exit
    sys.unraisablehook = lambda unraisable: None
    try:
        exit_code = asyncio.run(main_async())
    finally:
        import gc
        gc.collect()
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
