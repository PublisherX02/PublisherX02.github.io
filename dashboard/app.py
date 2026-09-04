"""MCP Trade Firewall — Terminal Governance & Order-Flow Dashboard.

Dense, live-tailing Textual terminal application monitoring the audit log,
firewall risk controls, account equity, basket weights, hash-chain integrity,
and distinguishing reactive hedge proposals from scheduled options overlays.

Run directly in terminal:
    python dashboard/app.py

Or serve via web browser:
    textual serve dashboard/app.py
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure project src/ is on sys.path for direct execution
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = WORKSPACE_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import yaml
from rich.console import RenderableType
from rich.json import JSON
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Grid, Horizontal, ScrollableContainer, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Label,
    Rule,
    Static,
)

# Core Firewall & Strategy Modules
try:
    import core_strategy
    from firewall import account_data
    from firewall.audit import AuditEvent, find_unresolved_pending, verify_chain
    from firewall.market_data import fetch_daily_bars
    from firewall.policy import PolicyEngine
    from firewall.rules.cooldown_after_loss import CooldownAfterLossRule
    from firewall.rules.drawdown_killswitch import DrawdownKillswitchRule
    from firewall.rules.order_rate_throttle import OrderRateThrottleRule
    from firewall.rules.position_cap import PositionCapRule
    from performance import build_performance_summary
    from broker_orders import DEFAULT_LIFECYCLE_FILE
except ImportError:
    AuditEvent = None  # type: ignore
    find_unresolved_pending = None  # type: ignore
    verify_chain = None  # type: ignore
    account_data = None  # type: ignore
    fetch_daily_bars = None  # type: ignore
    core_strategy = None  # type: ignore
    PolicyEngine = None  # type: ignore
    CooldownAfterLossRule = None  # type: ignore
    PositionCapRule = None  # type: ignore
    build_performance_summary = None  # type: ignore
    DEFAULT_LIFECYCLE_FILE = WORKSPACE_ROOT / "data" / "order_lifecycle.json"  # type: ignore

try:
    from narration.market_brief import (
        MarketBriefResult,
        build_narration_context_from_cache,
        get_latest_cached_brief,
    )
except ImportError:
    try:
        from src.narration.market_brief import (
            MarketBriefResult,
            build_narration_context_from_cache,
            get_latest_cached_brief,
        )
    except ImportError:
        MarketBriefResult = None  # type: ignore
        build_narration_context_from_cache = None  # type: ignore
        get_latest_cached_brief = None  # type: ignore


DEFAULT_CYCLES_DIR = WORKSPACE_ROOT / "data" / "cycles"


def load_lifecycle_summary(path: Path | str = DEFAULT_LIFECYCLE_FILE) -> dict[str, Any]:
    """Summarize the durable broker journal without inferring terminal states."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"available": False, "total": 0, "unresolved": 0, "statuses": {}}
    if not isinstance(payload, dict):
        return {"available": False, "total": 0, "unresolved": 0, "statuses": {}}
    statuses: dict[str, int] = {}
    unresolved = 0
    recovered = 0
    for item in payload.values():
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "unknown"))
        statuses[status] = statuses.get(status, 0) + 1
        if item.get("submitted") and not item.get("terminal"):
            unresolved += 1
        if item.get("recovered_from_call_id"):
            recovered += 1
    return {
        "available": True,
        "total": sum(statuses.values()),
        "unresolved": unresolved,
        "recovered": recovered,
        "statuses": statuses,
    }


def load_latest_cycle_artifact(cycles_dir: Path | str = DEFAULT_CYCLES_DIR) -> dict[str, Any] | None:
    """Read the newest complete cycle JSON; malformed/partial files are ignored."""
    try:
        paths = sorted(Path(cycles_dir).glob("*.json"), reverse=True)
    except OSError:
        return None
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and payload.get("cycle_id"):
                payload["_artifact_path"] = str(path)
                return payload
        except (OSError, json.JSONDecodeError):
            continue
    return None


def summarize_cycle_artifact(
    cycle: dict[str, Any] | None, now: datetime | None = None
) -> dict[str, Any]:
    """Produce honest deterministic-budget/broker metrics from a cycle artifact."""
    if not cycle:
        return {"available": False}
    all_orders = [
        *cycle.get("submitted_stock_orders", []),
        *cycle.get("submitted_option_orders", []),
    ]
    orders = [o for o in all_orders if o.get("broker", {}).get("submitted") is not False]
    simulated = len(all_orders) - len(orders)
    filled = sum(bool(o.get("broker", {}).get("filled")) for o in orders)
    statuses: dict[str, int] = {}
    for order in orders:
        status = str(order.get("broker", {}).get("status", "unknown"))
        statuses[status] = statuses.get(status, 0) + 1

    return {
        "available": True,
        "cycle_id": cycle.get("cycle_id"),
        "finished_at": cycle.get("finished_at"),
        "budget_usd": cycle.get("budget_usd"),
        "position_outcome_verification": cycle.get("position_outcome_verification"),
        "submitted": len(orders),
        "simulated": simulated,
        "filled": filled,
        "statuses": statuses,
        "blocked": len(cycle.get("blocked_orders", [])),
        "artifact_path": cycle.get("_artifact_path"),
    }



def read_dashboard_policy_config(policy_path: Path | str) -> dict[str, Any]:
    """Read the REAL, loaded policy's drawdown_killswitch/cooldown_after_loss/
    order_rate_throttle/position_cap configuration directly off the live rule
    instances -- the same `_read_dynamic_policy_config` pattern
    `core_strategy.py` already established, so the dashboard's displayed
    thresholds can never silently drift from what the firewall actually
    enforces. Returns {} (all keys absent) if the policy can't be loaded --
    callers must fall back to an honest "unknown" display, never a guessed
    constant.
    """
    if PolicyEngine is None:
        return {}
    try:
        engine = PolicyEngine.from_yaml(policy_path)
    except Exception:
        return {}

    config: dict[str, Any] = {}
    for rule in engine.rules:
        if DrawdownKillswitchRule is not None and isinstance(rule, DrawdownKillswitchRule):
            config["killswitch_threshold_usd"] = rule.cfg.session_pnl_threshold_usd
        elif CooldownAfterLossRule is not None and isinstance(rule, CooldownAfterLossRule):
            config["cooldown_loss_threshold"] = rule.cfg.cooldown_loss_threshold
            config["cooldown_loss_window"] = rule.cfg.cooldown_loss_window
            config["cooldown_duration_seconds"] = rule.cfg.cooldown_duration_seconds
        elif OrderRateThrottleRule is not None and isinstance(rule, OrderRateThrottleRule):
            config["throttle_max_orders"] = rule.cfg.max_orders
            config["throttle_window_seconds"] = rule.cfg.window_seconds
        elif PositionCapRule is not None and isinstance(rule, PositionCapRule):
            config["position_cap_max_pct_of_equity"] = rule.cfg.max_pct_of_equity
    return config


# ─────────────────────────────────────────────────────────────────────────────
# STYLES & DENSE PALETTE (Order-Flow Terminal Monitor)
# ─────────────────────────────────────────────────────────────────────────────

DASHBOARD_CSS = """
/* Theme: Deep Obsidian & Electric Slate Order-Flow Monitor */

Screen {
    background: #080c14;
    color: #e2e8f0;
}

#header-container {
    height: auto;
    background: #0d1527;
    border-bottom: heavy #1e293b;
    padding: 0 1;
}

#title-bar {
    height: 1;
    layout: horizontal;
    width: 100%;
}

#app-title {
    width: 1fr;
    text-style: bold;
    color: #38bdf8;
}

#app-status-badges {
    width: auto;
    text-align: right;
}

#account-metrics-bar {
    height: auto;
    background: #0f1c36;
    border: solid #1e3a5f;
    margin: 1 0 0 0;
    padding: 0 1;
}

#basket-weights-bar {
    height: auto;
    background: #0c182f;
    margin: 0 0 1 0;
    padding: 0 1;
    color: #94a3b8;
}

/* Unresolved Pending Warning Banner */
#unresolved-warning-banner {
    display: none;
    height: auto;
    background: #451a03;
    border: solid #b45309;
    color: #fef08a;
    padding: 0 1;
    margin: 0 0 1 0;
    text-style: bold;
}

#unresolved-warning-banner.visible {
    display: block;
}

/* Main Split View */
#main-container {
    height: 1fr;
    layout: horizontal;
}

/* Left Pane: Scrolling Audit Table */
#table-pane {
    width: 68%;
    height: 100%;
    border: solid #1e293b;
    background: #090e1a;
    margin-right: 1;
}

#table-pane-header {
    height: 1;
    background: #111d35;
    color: #38bdf8;
    text-style: bold;
    padding: 0 1;
    border-bottom: solid #1e293b;
}

#audit-table {
    height: 1fr;
    background: #090e1a;
}

#audit-table:focus {
    border: heavy #0284c7;
}

/* Right Pane: Risk Controls & State */
#side-panel {
    width: 32%;
    height: 100%;
    border: solid #1e293b;
    background: #0a101d;
    padding: 0 1;
}

.panel-section {
    background: #0e172a;
    border: solid #1e293b;
    margin-bottom: 1;
    padding: 0 1;
    height: auto;
}

#sec-ai-commentary {
    background: #0b172a;
    border: solid #0284c7;
}

.section-title {
    text-style: bold;
    color: #38bdf8;
    border-bottom: solid #1e293b;
    margin-bottom: 0;
    padding: 0;
}

.metric-row {
    height: auto;
    color: #cbd5e1;
}

.status-badge {
    text-style: bold;
}

/* Bottom Event Inspector Drawer */
#detail-inspector {
    height: 11;
    border-top: heavy #1e293b;
    background: #070a12;
    padding: 0 1;
}

#detail-inspector.collapsed {
    height: 1;
}

#inspector-header {
    height: 1;
    text-style: bold;
    color: #38bdf8;
    background: #0e172a;
    padding: 0 1;
}

#inspector-body {
    height: 1fr;
    layout: horizontal;
}

#inspector-left {
    width: 55%;
    height: 100%;
    padding-right: 1;
}

#inspector-right {
    width: 45%;
    height: 100%;
    border-left: solid #1e293b;
    padding-left: 1;
}

/* Modal Help / Detail */
#modal-dialog {
    width: 70%;
    height: 70%;
    background: #0f172a;
    border: thick #38bdf8;
    padding: 1 2;
}
"""


# ─────────────────────────────────────────────────────────────────────────────
# AUDIT LOG PARSER & RESOLVER (Tail-Style & Pending Outcome Matching)
# ─────────────────────────────────────────────────────────────────────────────

class AuditRecordItem:
    """Normalized audit item ready for table rendering and detail inspection."""

    def __init__(self, raw_event: dict[str, Any], raw_line: str, is_unresolved: bool = False) -> None:
        self.raw = raw_event
        self.raw_line = raw_line
        self.is_unresolved = is_unresolved

        self.event_id = raw_event.get("event_id", "")
        self.timestamp_raw = raw_event.get("timestamp", "")
        self.session_id = raw_event.get("session_id", "")
        self.tool_name = raw_event.get("tool_name", "")
        self.arguments = raw_event.get("arguments", {}) or {}
        self.verdict = raw_event.get("verdict", "")
        self.rule_id = raw_event.get("rule_id")
        self.regulation_ref = raw_event.get("regulation_ref")
        self.reason = raw_event.get("reason", "")
        self.forwarded = raw_event.get("forwarded")
        self.upstream_status = raw_event.get("upstream_status", "")
        self.prev_hash = raw_event.get("prev_hash", "")
        self.call_id = raw_event.get("call_id")
        self.pending_hash = raw_event.get("pending_hash")

        # Parse formatted timestamp (HH:MM:SS)
        self.time_display = self._format_timestamp(self.timestamp_raw)

        # Categorization & Metadata
        self.category, self.category_badge, self.color_theme = self._classify_mechanism()
        self.symbol_display = self._extract_symbol()
        self.verdict_display = self._format_verdict()
        self.reason_summary = self._format_reason_summary()

    def _format_timestamp(self, ts: str) -> str:
        if not ts:
            return "--:--:--"
        try:
            # Handle ISO timestamp format
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.strftime("%H:%M:%S")
        except Exception:
            return ts[-12:-4] if len(ts) >= 12 else ts

    def _extract_symbol(self) -> str:
        """Extract relevant symbol / ticker from arguments or tool payload."""
        args = self.arguments
        if not isinstance(args, dict):
            return "-"
        if "symbol" in args and args["symbol"]:
            return str(args["symbol"])
        if "occ_symbol" in args and args["occ_symbol"]:
            return str(args["occ_symbol"])
        if "ticker" in args and args["ticker"]:
            return str(args["ticker"])
        # If place_option_order has symbol
        if "order" in self.tool_name and "symbol" in args:
            return str(args["symbol"])
        return "-"

    def _classify_mechanism(self) -> tuple[str, Text, str]:
        """Distinguish Reactive Hedge, Standing Overlay, Core Strategy Risk Controls, and Orders."""
        rule = str(self.rule_id or "")
        tool = str(self.tool_name or "")

        # 1. Reactive Hedge (CVaR/Drawdown breach triggered)
        if rule == "hedge-proposal" or tool.startswith("hedge_proposal") or tool.startswith("hedge_release"):
            badge = Text()
            badge.append("⚡ REACTIVE HEDGE", style="bold #ffffff on #b00060")
            return "reactive_hedge", badge, "#ff007f"

        # 2. Standing Options Overlay (Scheduled portfolio insurance)
        if rule == "scheduled-options-overlay" or tool == "scheduled_overlay:proposed":
            badge = Text()
            badge.append("🛡️ STANDING OVERLAY", style="bold #001f3f on #00c8ff")
            return "standing_overlay", badge, "#00c8ff"

        # 3. Core Strategy Weight Clip
        if tool == "basket_rebalance:weight_clipped" or rule == "basket-rebalance-position-cap-clip":
            badge = Text()
            badge.append("✂️ WEIGHT CLIPPED", style="bold #3a1c00 on #ffb703")
            return "weight_clipped", badge, "#ffb703"

        # 4. Core Strategy Order Chunked
        if tool == "basket_rebalance:order_chunked" or rule == "basket-rebalance-chunking":
            badge = Text()
            badge.append("📦 ORDER CHUNKED", style="bold #001f3f on #38bdf8")
            return "order_chunked", badge, "#38bdf8"

        # 5. Core Strategy Basket Rebalance
        if tool.startswith("basket_rebalance"):
            badge = Text()
            badge.append("⚖️ BASKET REBAL", style="bold #ffffff on #4338ca")
            return "basket_rebalance", badge, "#818cf8"

        # 6. Place Stock Order
        if tool == "place_stock_order":
            badge = Text()
            badge.append("📈 STOCK ORDER", style="bold #ffffff on #1e3a5f")
            return "stock_order", badge, "#60a5fa"

        # 7. Place Option Order
        if tool == "place_option_order":
            badge = Text()
            badge.append("📊 OPTION ORDER", style="bold #ffffff on #581c87")
            return "option_order", badge, "#c084fc"

        # 8. Positions / Account Info
        if tool in ("get_all_positions", "get_account_info", "get_orders"):
            badge = Text()
            badge.append(f"📋 {tool[:16]}", style="dim #94a3b8")
            return "read_tool", badge, "#94a3b8"

        # Default Tool Badge
        badge = Text()
        badge.append(tool[:18], style="#cbd5e1")
        return "general", badge, "#cbd5e1"

    def _format_verdict(self) -> Text:
        """Color-coded verdict badge."""
        v = (self.verdict or "").lower()
        txt = Text()

        if self.is_unresolved:
            txt.append("⚠️ UNRESOLVED", style="bold #ffffff on #b45309")
            return txt

        if v == "allow":
            status_suffix = ""
            if self.upstream_status == "ok":
                status_suffix = " ✓"
            elif self.upstream_status == "error":
                status_suffix = " ✗"
            txt.append(f"ALLOW{status_suffix}", style="bold #10b981")
        elif v == "soft_block":
            txt.append("SOFT BLOCK", style="bold #f59e0b")
        elif v == "hard_block":
            txt.append("HARD BLOCK", style="bold #ef4444")
        elif v == "info":
            txt.append("INFO", style="bold #06b6d4")
        elif v in ("state_entered", "state_exited"):
            txt.append("STATE CHG", style="bold #a855f7")
        else:
            txt.append((self.verdict or "UNKNOWN").upper(), style="dim #94a3b8")

        return txt

    def _format_reason_summary(self) -> str:
        """Produce clear, informative summaries for risk controls and orders."""
        cat = self.category
        args = self.arguments

        if cat == "weight_clipped":
            sym = args.get("symbol", self.symbol_display)
            raw = args.get("raw_target_weight")
            cap = args.get("capped_target_weight")
            ceil = args.get("ceiling_usd")
            if raw is not None and cap is not None:
                return f"{sym}: {raw:.1%} raw -> {cap:.1%} capped (${ceil:,.0f} ceiling)"
            return self.reason[:60]

        if cat == "order_chunked":
            sym = args.get("symbol", self.symbol_display)
            side = args.get("side", "").upper()
            total_qty = args.get("total_qty")
            chunks = args.get("chunk_sizes", [])
            count = args.get("chunk_count", len(chunks))
            if chunks:
                return f"{sym} {side}: {total_qty} shs split into {count} chunk(s) {chunks}"
            return self.reason[:60]

        if cat == "standing_overlay":
            sym = args.get("symbol", "SPY")
            occ = args.get("occ_symbol", "")
            contracts = args.get("contracts", 1)
            strike = args.get("strike", 0.0)
            exp = args.get("target_expiry", "")
            return f"Standing Put: BUY {contracts} PUT {sym} (${strike:,.1f} exp {exp})"

        if cat == "reactive_hedge":
            sym = args.get("symbol", self.symbol_display)
            return f"⚡ Triggered Put on {sym}: Tail-risk breach warning"

        # General Truncated Reason
        clean_reason = self.reason.replace("\n", " ").strip()
        if len(clean_reason) > 75:
            return clean_reason[:72] + "..."
        return clean_reason or "—"


class AuditLogWatcher:
    """Tail-style watcher that reads audit.jsonl incrementally without rereading."""

    def __init__(self, log_path: Path | str) -> None:
        self.log_path = Path(log_path)
        self._file_offset: int = 0
        self._pending_by_call_id: dict[str, dict[str, Any]] = {}
        self._all_records: list[AuditRecordItem] = []
        self._unresolved_records: list[AuditRecordItem] = []
        self._order_timestamps: list[float] = []

        # Sticky latch tracking: DrawdownKillswitchRule/OrderRateThrottleRule
        # trip in-memory on the live PolicyEngine and stay tripped until an
        # explicit `PolicyEngine.reset(rule_id)` call -- which writes NO
        # audit record at all (verified against src/firewall/policy.py).
        # A dashboard reading only audit.jsonl therefore has no way to
        # observe a reset; the only thing it CAN observe is "a hard_block
        # from this rule happened at least once." Once True, these flags
        # are never cleared by this watcher -- see RiskSidePanel's own
        # disclosure text for why that's the honest behavior, not a bug.
        self.killswitch_ever_tripped: bool = False
        self.throttle_ever_paused: bool = False

    def _maybe_track_order_timestamp(self, tool_name: str, data: dict[str, Any]) -> None:
        """Record one timestamp per REAL order-placement call for rate-window
        counting -- called exactly once per call (from the outcome record, or
        from a direct single hard_block record), never from the pending half
        of the two-record pattern. See `poll_new_records`."""
        if tool_name not in ("place_stock_order", "place_option_order", "place_crypto_order"):
            return
        try:
            ts = data.get("timestamp", "")
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            self._order_timestamps.append(dt.timestamp())
        except Exception:
            self._order_timestamps.append(time.time())

    def _track_latch_state(self, item: "AuditRecordItem") -> None:
        """Sticky-until-evidence-of-reset: once either rule is observed
        hard-blocking, latch that fact for the rest of this process's life."""
        if item.verdict != "hard_block":
            return
        if item.rule_id == "drawdown-killswitch":
            self.killswitch_ever_tripped = True
        elif item.rule_id == "order-rate-throttle":
            self.throttle_ever_paused = True

    def poll_new_records(self) -> list[AuditRecordItem]:
        """Reads new lines appended to audit.jsonl since last poll."""
        if not self.log_path.exists():
            return []

        file_size = self.log_path.stat().st_size
        if file_size < self._file_offset:
            # File was truncated or rotated
            self._file_offset = 0
            self._all_records.clear()
            self._pending_by_call_id.clear()

        new_items: list[AuditRecordItem] = []

        with self.log_path.open("r", encoding="utf-8") as f:
            f.seek(self._file_offset)
            for raw_line in f:
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                call_id = data.get("call_id")
                upstream_status = data.get("upstream_status")
                tool_name = data.get("tool_name", "")

                # Handle two-record pending -> outcome pattern:
                # 1. If upstream_status == "pending" and call_id is present:
                #    Store as pending, do NOT display as a final outcome row yet,
                #    and do NOT count it toward the order-rate window either --
                #    a pending record and its later outcome record are the SAME
                #    real order call, sharing one tool_name; counting both would
                #    double every real order against the throttle limit.
                if call_id and upstream_status == "pending":
                    self._pending_by_call_id[call_id] = data
                    continue

                # 2. If an outcome record arrives (matching call_id with non-pending status):
                if call_id and upstream_status != "pending":
                    self._pending_by_call_id.pop(call_id, None)
                    self._maybe_track_order_timestamp(tool_name, data)
                    item = AuditRecordItem(data, line, is_unresolved=False)
                    new_items.append(item)
                    self._all_records.insert(0, item)
                    self._track_latch_state(item)
                    continue

                # 3. Direct single records (hard_block, info, state transitions, etc.)
                # -- includes a place_order call hard-blocked before ever
                # reaching the pending write, which never gets an outcome
                # record of its own; still counts toward the real order-rate
                # window the same way the real rule's own OrderHistory does.
                self._maybe_track_order_timestamp(tool_name, data)
                item = AuditRecordItem(data, line, is_unresolved=False)
                new_items.append(item)
                self._all_records.insert(0, item)
                self._track_latch_state(item)

            self._file_offset = f.tell()

        return new_items

    def check_unresolved_pending(self, stale_after_seconds: float = 60.0) -> list[AuditRecordItem]:
        """Surface stale pending records with no matching outcome."""
        if find_unresolved_pending is None or not self.log_path.exists():
            return []

        try:
            stale_events = find_unresolved_pending(
                self.log_path, stale_after_seconds=stale_after_seconds
            )
            self._unresolved_records = [
                AuditRecordItem(e.model_dump(), e.model_dump_json(), is_unresolved=True)
                for e in stale_events
            ]
            return self._unresolved_records
        except Exception:
            return []

    def get_order_rate_in_window(self, window_seconds: float = 60.0) -> int:
        """Count orders placed in the rolling window."""
        now = time.time()
        # Clean older entries
        self._order_timestamps = [
            t for t in self._order_timestamps if (now - t) <= window_seconds
        ]
        return len(self._order_timestamps)


# ─────────────────────────────────────────────────────────────────────────────
# TEXTUAL UI WIDGETS
# ─────────────────────────────────────────────────────────────────────────────

class DashboardHeader(Static):
    """Top header displaying real account equity, day PnL, positions, basket weights, and status."""

    def compose(self) -> ComposeResult:
        with Container(id="header-container"):
            with Horizontal(id="title-bar"):
                yield Label("🛡️ MCP TRADE FIREWALL  |  ORDER-FLOW & RISK GOVERNANCE MONITOR", id="app-title")
                yield Label("[● LIVE TAIL]  [CHAIN: VERIFYING...]", id="app-status-badges")
            yield Static(id="account-metrics-bar")
            yield Static(id="basket-weights-bar")
            yield Static(id="unresolved-warning-banner")

    def update_metrics(
        self,
        equity: float | None,
        session_pnl: float | None,
        positions_count: int,
        unresolved_count: int,
        chain_pass: bool | None,
        is_paused: bool,
        basket_data: dict[str, Any] | None,
        account_unavailable: bool = False,
    ) -> None:
        # 1. Title bar status badges
        status_text = Text()
        if is_paused:
            status_text.append("⏸ TAIL PAUSED  ", style="bold #f59e0b")
        else:
            status_text.append("● LIVE TAIL  ", style="bold #10b981")

        if chain_pass is True:
            status_text.append("● CHAIN: PASS  ", style="bold #10b981")
        elif chain_pass is False:
            status_text.append("🚨 CHAIN: TAMPERED  ", style="bold #ef4444")
        else:
            status_text.append("○ CHAIN: CHECKING  ", style="dim #94a3b8")

        status_text.append(datetime.now(timezone.utc).strftime("%H:%M:%S UTC"), style="#94a3b8")

        badges_label = self.query_one("#app-status-badges", Label)
        badges_label.update(status_text)

        # 2. Account Metrics Bar
        if equity is not None:
            eq_str = f"${equity:,.2f}"
        elif account_unavailable:
            eq_str = "OFFLINE (public demo snapshot, not broker-connected)"
        else:
            eq_str = "Fetching..."
        pnl_val = session_pnl or 0.0
        pnl_color = "#10b981" if pnl_val >= 0 else "#ef4444"
        pnl_prefix = "+" if pnl_val >= 0 else ""
        if session_pnl is not None:
            pnl_str = f"{pnl_prefix}${pnl_val:,.2f}"
        elif account_unavailable:
            pnl_str = "N/A (offline)"
        else:
            pnl_str = "$0.00"

        budget_str = f"${equity * 0.90:,.2f} (90% NAV)" if equity else "—"

        metrics_text = Text()
        metrics_text.append("ACCOUNT EQUITY: ", style="bold #38bdf8")
        metrics_text.append(f"{eq_str}   ", style="bold #ffffff")
        metrics_text.append("SESSION P&L: ", style="bold #38bdf8")
        metrics_text.append(f"{pnl_str}   ", style=f"bold {pnl_color}")
        metrics_text.append("POSITIONS: ", style="bold #38bdf8")
        metrics_text.append(f"{positions_count} Active   ", style="bold #ffffff")
        metrics_text.append("BASKET BUDGET: ", style="bold #38bdf8")
        metrics_text.append(f"{budget_str}   ", style="#cbd5e1")
        metrics_text.append("UNRESOLVED CALLS: ", style="bold #38bdf8")
        if unresolved_count > 0:
            metrics_text.append(f"{unresolved_count} ALERT", style="bold #ef4444")
        else:
            metrics_text.append("0 (Clear)", style="bold #10b981")

        self.query_one("#account-metrics-bar", Static).update(metrics_text)

        # 3. Basket Weights vs Target Bar
        basket_text = Text()
        basket_text.append("BASKET ALLOCATION (Inv-Vol Weighted): ", style="bold #38bdf8")

        if basket_data and "weights" in basket_data:
            weights = basket_data["weights"]
            for sym, w_info in weights.items():
                curr = w_info.get("current_w", 0.0)
                tgt = w_info.get("target_w", 0.0)
                clipped = w_info.get("is_clipped", False)

                clip_badge = " [CAPPED]" if clipped else ""
                basket_text.append(f"{sym}: ", style="bold #ffffff")
                basket_text.append(f"{curr:.1%} ", style="#10b981" if abs(curr - tgt) <= 0.05 else "#f59e0b")
                basket_text.append(f"(tgt {tgt:.1%}{clip_badge})   ", style="#64748b")
            cash_w = basket_data.get("cash_w", 0.10)
            basket_text.append(f"CASH BUFFER: {cash_w:.1%}", style="dim #94a3b8")
            if basket_data.get("status") != "live":
                basket_text.append("  [STALE]", style="bold #f59e0b")
        else:
            status = (basket_data or {}).get("status", "loading").upper()
            basket_text.append(f"{status} — no verified allocation data", style="bold #f59e0b")

        self.query_one("#basket-weights-bar", Static).update(basket_text)

        # 4. Unresolved Warning Banner
        banner = self.query_one("#unresolved-warning-banner", Static)
        if unresolved_count > 0:
            banner.update(
                f"🚨 WARNING: {unresolved_count} Unresolved Pending Record(s) Detected -- "
                f"\"Call attempted, outcome unknown\" (Process termination during tool execution). "
                f"Check side panel for details."
            )
            banner.add_class("visible")
        else:
            banner.remove_class("visible")


class RiskSidePanel(Static):
    """Side panel displaying static status of firewall controls and hash chain."""

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="side-panel"):
            yield Label("⚡ FIREWALL RISK CONTROLS & STATE", classes="section-title")

            # 1. Drawdown Killswitch
            with Container(classes="panel-section", id="sec-killswitch"):
                yield Label("🚨 DRAWDOWN KILLSWITCH", classes="section-title")
                yield Static(id="killswitch-status", classes="metric-row")

            # 2. Order Rate Throttle
            with Container(classes="panel-section", id="sec-throttle"):
                yield Label("⏱️ ORDER RATE THROTTLE", classes="section-title")
                yield Static(id="throttle-status", classes="metric-row")

            # 3. Cooldown After Loss (Explicitly Disclosed)
            with Container(classes="panel-section", id="sec-cooldown"):
                yield Label("❄️ COOLDOWN AFTER LOSS", classes="section-title")
                yield Static(id="cooldown-status", classes="metric-row")

            # 4. Hash Chain Verification
            with Container(classes="panel-section", id="sec-hashchain"):
                yield Label("🔗 CRYPTOGRAPHIC HASH CHAIN", classes="section-title")
                yield Static(id="hashchain-status", classes="metric-row")

            # 5. Pending Reconciliation
            with Container(classes="panel-section", id="sec-pending"):
                yield Label("⚠️ PENDING CALL RECONCILIATION", classes="section-title")
                yield Static(id="pending-status", classes="metric-row")

            # 6. Deterministic budget and broker lifecycle
            with Container(classes="panel-section", id="sec-agent-cycle"):
                yield Label("DETERMINISTIC BUDGET & BROKER LIFECYCLE", classes="section-title")
                yield Static(id="agent-cycle-status", classes="metric-row")

            # 7. P&L attribution and benchmark
            with Container(classes="panel-section", id="sec-performance"):
                yield Label("P&L ATTRIBUTION & BENCHMARK", classes="section-title")
                yield Static(id="performance-status", classes="metric-row")

            # 8. AI Market Commentary (Informational / Non-Trading)
            with Container(classes="panel-section", id="sec-ai-commentary"):
                yield Label("🤖 AI COMMENTARY (informational, generated by Featherless, not a trading input)", classes="section-title")
                yield Static(id="ai-commentary-status", classes="metric-row")

    def update_panel(
        self,
        session_pnl: float | None,
        killswitch_tripped: bool,
        killswitch_sticky: bool,
        throttle_count: int,
        throttle_limit: int | None,
        throttle_paused: bool,
        throttle_sticky: bool,
        chain_pass: bool | None,
        chain_count: int,
        bad_idx: int | None,
        unresolved_items: list[AuditRecordItem],
        policy_cfg: dict[str, Any],
        brief: Any | None = None,
        cycle_summary: dict[str, Any] | None = None,
        performance_summary: dict[str, Any] | None = None,
        lifecycle_summary: dict[str, Any] | None = None,
    ) -> None:
        # 1. Drawdown Killswitch
        ks_threshold = policy_cfg.get("killswitch_threshold_usd")
        ks_text = Text()
        pnl_val = session_pnl or 0.0
        if killswitch_tripped:
            ks_text.append("Status: ", style="bold #ffffff")
            if killswitch_sticky:
                ks_text.append("🚨 TRIPPED (LAST-KNOWN, STICKY)\n", style="bold #ef4444")
            else:
                ks_text.append("🚨 TRIPPED (HALTED)\n", style="bold #ef4444")
        else:
            ks_text.append("Status: ", style="bold #ffffff")
            ks_text.append("● ARMED (NORMAL)\n", style="bold #10b981")
        ks_text.append(f"Session P&L: ${pnl_val:,.2f}\n", style="#ffffff" if pnl_val >= 0 else "#ef4444")
        threshold_str = f"${ks_threshold:,.2f}" if ks_threshold is not None else "UNKNOWN (policy unreadable)"
        ks_text.append(f"Threshold: {threshold_str} (Session Latch)\n", style="#94a3b8")
        ks_text.append("Rule: SEC Rule 15c3-5(c)(1)(i)\n", style="dim #64748b")
        ks_text.append(
            "Note: reset is not log-derivable; once tripped this stays\n"
            "\"TRIPPED\" for this session even if P&L later recovers.",
            style="dim italic #64748b",
        )
        self.query_one("#killswitch-status", Static).update(ks_text)

        # 2. Order Rate Throttle
        throttle_window = policy_cfg.get("throttle_window_seconds")
        th_text = Text()
        if throttle_paused:
            th_text.append("Status: ", style="bold #ffffff")
            if throttle_sticky:
                th_text.append("🚨 PAUSED (LAST-KNOWN, STICKY)\n", style="bold #ef4444")
            else:
                th_text.append("🚨 PAUSED (LATCHED)\n", style="bold #ef4444")
        else:
            th_text.append("Status: ", style="bold #ffffff")
            th_text.append("● ACTIVE\n", style="bold #10b981")
        limit_str = str(throttle_limit) if throttle_limit is not None else "?"
        th_text.append(f"Current Window: {throttle_count} / {limit_str} orders\n", style="bold #ffffff")
        window_str = f"{throttle_window:.0f}s" if throttle_window is not None else "UNKNOWN"
        th_text.append(f"Window: Rolling {window_str} (Paced submissions)\n", style="#94a3b8")
        th_text.append("Rule: SEC Rule 15c3-5(c)(1)(ii)\n", style="dim #64748b")
        th_text.append(
            "Note: reset is not log-derivable; once paused this stays\n"
            "\"PAUSED\" for this session even after the window clears.",
            style="dim italic #64748b",
        )
        self.query_one("#throttle-status", Static).update(th_text)

        # 3. Cooldown After Loss
        cd_loss_threshold = policy_cfg.get("cooldown_loss_threshold")
        cd_loss_window = policy_cfg.get("cooldown_loss_window")
        cd_duration = policy_cfg.get("cooldown_duration_seconds")
        cd_text = Text()
        cd_text.append("Status: ", style="bold #ffffff")
        cd_text.append("○ NOT CONNECTED\n", style="bold #f59e0b")
        cd_text.append("Disclosure: ", style="bold #38bdf8")
        cd_text.append("No realized-P&L data source available from Alpaca API endpoint (unrealized mark-to-market only).\n", style="#cbd5e1")
        if cd_loss_threshold is not None:
            cd_text.append(
                f"Config: ${cd_loss_threshold:,.0f} threshold / {cd_loss_window:.0f}s window "
                f"/ {cd_duration:.0f}s cooldown",
                style="dim #64748b",
            )
        else:
            cd_text.append("Config: UNKNOWN (policy unreadable)", style="dim #64748b")
        self.query_one("#cooldown-status", Static).update(cd_text)

        # 4. Hash Chain Verification
        hc_text = Text()
        if chain_pass is True:
            hc_text.append("Integrity: ", style="bold #ffffff")
            hc_text.append("● PASS (VERIFIED)\n", style="bold #10b981")
            hc_text.append(f"Records Verified: {chain_count} / {chain_count}\n", style="#ffffff")
            hc_text.append("Genesis: 0000000000000000...\n", style="dim #64748b")
            hc_text.append("Tamper Proof: SHA-256 Prev-Hash Chain", style="#94a3b8")
        elif chain_pass is False:
            hc_text.append("Integrity: ", style="bold #ffffff")
            hc_text.append("🚨 TAMPER DETECTED\n", style="bold #ef4444")
            hc_text.append(f"Broken at Record Index: #{bad_idx}\n", style="bold #ef4444")
            hc_text.append("Chain mismatch detected by verify_chain()", style="#ef4444")
        else:
            hc_text.append("Integrity: Checking...\n", style="dim #94a3b8")
        self.query_one("#hashchain-status", Static).update(hc_text)

        # 5. Pending Call Reconciliation
        pd_text = Text()
        if not unresolved_items:
            pd_text.append("Unresolved: ", style="bold #ffffff")
            pd_text.append("● 0 (All outcomes resolved)\n", style="bold #10b981")
            pd_text.append("Crash-safe pending->outcome pattern intact.", style="dim #94a3b8")
        else:
            pd_text.append("Unresolved: ", style="bold #ffffff")
            pd_text.append(f"⚠️ {len(unresolved_items)} CALLS UNKNOWN\n", style="bold #ef4444")
            for item in unresolved_items[:3]:
                pd_text.append(f"• {item.time_display} {item.tool_name} ({item.call_id[:8]}...)\n", style="#fef08a")
            if len(unresolved_items) > 3:
                pd_text.append(f"... and {len(unresolved_items) - 3} more", style="dim #94a3b8")
        self.query_one("#pending-status", Static).update(pd_text)

        # 6. Deterministic budget and broker lifecycle
        cycle_text = Text()
        summary = cycle_summary or {"available": False}
        if not summary.get("available"):
            cycle_text.append("No completed cycle artifact yet.\n", style="#f59e0b")
            cycle_text.append("No broker lifecycle claim is available.", style="dim #94a3b8")
        else:
            budget = summary.get("budget_usd")
            if isinstance(budget, (int, float)):
                cycle_text.append(f"Deterministic budget: ${budget:,.0f}\n", style="#cbd5e1")
            cycle_text.append(
                f"Broker: {summary.get('submitted', 0)} submitted | "
                f"{summary.get('filled', 0)} confirmed filled | "
                f"{summary.get('blocked', 0)} firewall blocked\n",
                style="bold #38bdf8",
            )
            verification = summary.get("position_outcome_verification") or {}
            if verification and not verification.get("verified"):
                cycle_text.append(
                    f"Final positions: UNVERIFIED ({verification.get('reason') or 'query failed'})\n",
                    style="bold #ef4444",
                )
            if summary.get("simulated"):
                cycle_text.append(
                    f"Dry-run simulated: {summary['simulated']} (0 sent upstream)\n",
                    style="bold #f59e0b",
                )
            statuses = summary.get("statuses", {})
            if statuses:
                cycle_text.append(
                    "Statuses: " + ", ".join(f"{k}={v}" for k, v in sorted(statuses.items())),
                    style="dim #94a3b8",
                )
            lifecycle = lifecycle_summary or {}
            if lifecycle.get("available"):
                unresolved_lifecycle = lifecycle.get("unresolved", 0)
                cycle_text.append(
                    f"\nDurable lifecycle: {lifecycle.get('total', 0)} tracked | "
                    f"{unresolved_lifecycle} non-terminal | "
                    f"{lifecycle.get('recovered', 0)} crash-recovered",
                    style="bold #ef4444" if unresolved_lifecycle else "#10b981",
                )
        self.query_one("#agent-cycle-status", Static).update(cycle_text)

        # 7. P&L attribution and benchmark
        perf = performance_summary or {}
        perf_text = Text()
        portfolio_return = perf.get("portfolio_return")
        benchmark_return = perf.get("benchmark_return")
        excess_return = perf.get("excess_return")
        if portfolio_return is None:
            perf_text.append("Portfolio return unavailable.\n", style="#f59e0b")
        else:
            perf_text.append(
                f"Portfolio: {portfolio_return:+.2%} (${perf.get('portfolio_pnl_usd', 0):+,.2f})\n",
                style="bold #10b981" if portfolio_return >= 0 else "bold #ef4444",
            )
        benchmark = perf.get("benchmark_symbol", "SPY")
        if benchmark_return is None:
            perf_text.append(f"{benchmark}: unavailable | Excess: unavailable\n", style="#94a3b8")
        else:
            perf_text.append(
                f"{benchmark}: {benchmark_return:+.2%} | Excess: {excess_return:+.2%}\n",
                style="#38bdf8",
            )
        attribution = perf.get("attribution", [])
        if attribution:
            perf_text.append("Top intraday contributors:\n", style="bold #ffffff")
            for symbol, value in attribution[:5]:
                perf_text.append(
                    f"  {symbol:<6} ${value:+,.2f}\n",
                    style="#10b981" if value >= 0 else "#ef4444",
                )
            residual = perf.get("residual_usd")
            if residual is not None:
                perf_text.append(
                    f"Residual (realized/cash/options/unmapped): ${residual:+,.2f}",
                    style="dim #94a3b8",
                )
        else:
            perf_text.append("Per-symbol intraday attribution unavailable.", style="dim #94a3b8")
        self.query_one("#performance-status", Static).update(perf_text)

        # 8. AI Commentary (Informational / Non-Trading)
        comm_text = Text()
        if brief is not None:
            model_name = getattr(brief, "model", "Qwen/Qwen2.5-7B-Instruct")
            ts = getattr(brief, "timestamp", "")
            time_display = ts[11:19] if len(ts) >= 19 else "Just now"
            comm_text.append(f"Model: {model_name}  |  Updated: {time_display} UTC\n", style="bold #38bdf8")
            comm_text.append(f"{getattr(brief, 'text', '')}\n\n", style="#f1f5f9")
            
            if getattr(brief, "cached", False):
                comm_text.append("Status: Displaying cached periodic brief.\n", style="dim italic #64748b")
            elif not getattr(brief, "ok", False):
                err = getattr(brief, "error_reason", "offline")
                comm_text.append(f"Status: Fallback mode ({err}).\n", style="dim italic #f59e0b")
            else:
                lat = getattr(brief, "latency_seconds", 0.0)
                comm_text.append(f"Status: Live generation ({lat:.1f}s).\n", style="dim #10b981")
            comm_text.append("Notice: Strictly educational/monitoring text. Zero feedback into trading or risk decisions.", style="dim italic #64748b")
        else:
            comm_text.append("Commentary loading...", style="dim #94a3b8")
        self.query_one("#ai-commentary-status", Static).update(comm_text)


class EventDetailInspector(Static):
    """Bottom expandable inspector showing full event JSON, unclipped reasons, and hashes."""

    def compose(self) -> ComposeResult:
        with Container(id="detail-inspector"):
            yield Label("📋 EVENT INSPECTOR (Select any row to view full payload & audit hashes)", id="inspector-header")
            with Horizontal(id="inspector-body"):
                yield Static(id="inspector-left")
                yield Static(id="inspector-right")

    def show_record(self, item: AuditRecordItem | None) -> None:
        if item is None:
            self.query_one("#inspector-header", Label).update("📋 EVENT INSPECTOR (No record selected)")
            self.query_one("#inspector-left", Static).update("Select a row in the audit table to view complete details.")
            self.query_one("#inspector-right", Static).update("")
            return

        hdr_text = Text()
        hdr_text.append("📋 EVENT INSPECTOR  |  ", style="bold #38bdf8")
        hdr_text.append(f"Time: {item.time_display}  |  ", style="#ffffff")
        hdr_text.append(f"Tool: {item.tool_name}  |  ", style="bold #38bdf8")
        hdr_text.append(f"Verdict: {item.verdict.upper()}  |  ", style="#10b981" if item.verdict == "allow" else "#ef4444")
        hdr_text.append(f"Symbol: {item.symbol_display}", style="bold #ffffff")
        self.query_one("#inspector-header", Label).update(hdr_text)

        # Left Column: Reason, Rules, Hashes
        left_text = Text()
        left_text.append("REASON / DETAIL:\n", style="bold #38bdf8")
        left_text.append(f"{item.reason}\n\n", style="#ffffff")

        left_text.append("RULE ID: ", style="bold #38bdf8")
        left_text.append(f"{item.rule_id or '—'}   ", style="#ffffff")
        left_text.append("REGULATION REF: ", style="bold #38bdf8")
        left_text.append(f"{item.regulation_ref or '—'}\n", style="#ffffff")

        left_text.append("UPSTREAM STATUS: ", style="bold #38bdf8")
        left_text.append(f"{item.upstream_status} (Forwarded: {item.forwarded})   ", style="#cbd5e1")
        left_text.append("SESSION ID: ", style="bold #38bdf8")
        left_text.append(f"{item.session_id[:8]}...\n", style="#64748b")

        left_text.append("EVENT ID: ", style="bold #38bdf8")
        left_text.append(f"{item.event_id}   ", style="dim #64748b")
        if item.call_id:
            left_text.append("CALL ID: ", style="bold #38bdf8")
            left_text.append(f"{item.call_id}", style="dim #64748b")

        self.query_one("#inspector-left", Static).update(left_text)

        # Right Column: Syntax-highlighted Arguments JSON
        try:
            formatted_json = json.dumps(item.arguments, indent=2)
            syntax = Syntax(formatted_json, "json", theme="monokai", word_wrap=True)
            self.query_one("#inspector-right", Static).update(syntax)
        except Exception:
            self.query_one("#inspector-right", Static).update(f"Arguments: {item.arguments}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN DASHBOARD APPLICATION
# ─────────────────────────────────────────────────────────────────────────────

class FirewallDashboardApp(App):
    """Main Textual Application for MCP Trade Firewall Order-Flow & Governance Monitor."""

    CSS = DASHBOARD_CSS
    TITLE = "MCP Trade Firewall Monitor"
    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("r", "refresh_data", "Refresh", show=True),
        Binding("space", "toggle_pause", "Pause/Resume Tail", show=True),
        Binding("f", "cycle_filter", "Filter", show=True),
        Binding("c", "verify_hash_chain", "Verify Chain", show=True),
        Binding("d", "toggle_inspector", "Toggle Detail", show=True),
        Binding("w", "show_web_info", "Web Serve Info", show=True),
    ]

    # Reactive Application States
    account_equity = reactive[float | None](None)
    session_pnl_usd = reactive[float | None](None)
    positions_count = reactive[int](0)
    is_tail_paused = reactive[bool](False)
    chain_pass = reactive[bool | None](None)
    active_filter = reactive[str]("ALL")

    def __init__(
        self,
        audit_path: Path | str = "audit.jsonl",
        policy_path: Path | str = "policies/default.yaml",
        poll_interval: float = 1.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.audit_path = Path(audit_path)
        self.policy_path = Path(policy_path)
        self.poll_interval = poll_interval

        self.watcher = AuditLogWatcher(self.audit_path)
        self._displayed_records: list[AuditRecordItem] = []
        self._selected_record: AuditRecordItem | None = None
        self._chain_count: int = 0
        self._bad_idx: int | None = None
        self._basket_cache: dict[str, Any] = {"status": "loading"}
        self._account_unavailable: bool = False
        self._performance_summary: dict[str, Any] = {}
        self._lifecycle_summary: dict[str, Any] = load_lifecycle_summary()
        self._throttle_count: int = 0
        self._killswitch_tripped: bool = False

        self._latest_market_brief: Any = get_latest_cached_brief() if get_latest_cached_brief else None
        self._latest_cycle_summary: dict[str, Any] = summarize_cycle_artifact(
            load_latest_cycle_artifact()
        )

        # Real thresholds read from the loaded PolicyEngine
        # (`read_dashboard_policy_config`) -- never a hardcoded guess. Empty
        # until `refresh_policy_config` succeeds at least once; every reader
        # of `self._policy_cfg` must treat a missing key as "unknown," not
        # silently substitute a fabricated default.
        self._policy_cfg: dict[str, Any] = {}
        self.refresh_policy_config()

    def compose(self) -> ComposeResult:
        yield DashboardHeader()
        with Container(id="main-container"):
            with Vertical(id="table-pane"):
                yield Label("LIVE AUDIT STREAM  [Filter: ALL]", id="table-pane-header")
                yield DataTable(id="audit-table", cursor_type="row")
            yield RiskSidePanel()
        yield EventDetailInspector()
        yield Footer()

    def on_mount(self) -> None:
        """Initialize table columns and start background polling."""
        table = self.query_one("#audit-table", DataTable)
        table.add_columns(
            "TIME",
            "MECHANISM / TOOL",
            "SYM",
            "VERDICT",
            "RULE ID",
            "REG REF",
            "REASON / SUMMARY",
        )

        # Initial Load
        self.refresh_all_data()

        # Set Periodic Polling Timers
        self.set_interval(self.poll_interval, self.poll_audit_stream)
        self.set_interval(5.0, self.refresh_account_and_basket)
        self.set_interval(10.0, self.verify_chain_periodically)
        self.set_interval(30.0, self.refresh_policy_config)
        self.set_interval(60.0, self.trigger_market_commentary_refresh)

        # Trigger background commentary generation on launch
        self.trigger_market_commentary_refresh()

    @work(exclusive=True, thread=True)
    def trigger_market_commentary_refresh(self) -> None:
        """Refresh cached commentary off the UI event loop."""
        if get_latest_cached_brief is None:
            return
        # Use the exact same context builder as the cycle, but perform only
        # local cache reads here. Generation is scheduled once by run_agent.
        if build_narration_context_from_cache is not None:
            build_narration_context_from_cache(self._basket_cache)
        self._latest_market_brief = get_latest_cached_brief()
        self._latest_cycle_summary = summarize_cycle_artifact(load_latest_cycle_artifact())
        self._lifecycle_summary = load_lifecycle_summary()
        self.call_from_thread(
            self._sync_ui_components, self.watcher._unresolved_records
        )

    def refresh_policy_config(self) -> None:
        """(Re-)load real thresholds off `self.policy_path` -- the actual
        loaded PolicyEngine, not a restated constant. Cheap (config parsing
        only, no rule .check() calls, no network), so reloading periodically
        costs nothing and picks up an edited policy file without a restart."""
        cfg = read_dashboard_policy_config(self.policy_path)
        if cfg:
            self._policy_cfg = cfg

    def refresh_all_data(self) -> None:
        """Full refresh of audit log, account state, basket weights, and hash chain."""
        self._latest_cycle_summary = summarize_cycle_artifact(load_latest_cycle_artifact())
        self.poll_audit_stream()
        self.refresh_account_and_basket()
        self.verify_chain_periodically()

    def poll_audit_stream(self) -> None:
        """Polls audit.jsonl for new records tail-style and updates UI."""
        if self.is_tail_paused:
            return

        new_items = self.watcher.poll_new_records()
        unresolved_items = self.watcher.check_unresolved_pending(stale_after_seconds=30.0)

        # Update order rate count in the REAL configured rolling window
        # (`order_rate_throttle`'s own window_seconds, read off the loaded
        # policy -- see `read_dashboard_policy_config`) -- 60s only as a
        # fallback when the policy couldn't be read at all.
        throttle_window = self._policy_cfg.get("throttle_window_seconds", 60.0)
        self._throttle_count = self.watcher.get_order_rate_in_window(throttle_window)

        if new_items or unresolved_items or not self._displayed_records:
            self._update_table_view()

        # Update Side Panel & Header
        self._sync_ui_components(unresolved_items)

    def _update_table_view(self) -> None:
        """Repopulates the DataTable according to current active filter."""
        table = self.query_one("#audit-table", DataTable)
        current_cursor = table.cursor_row

        # Combine all records and any unresolved pending markers
        all_candidates: list[AuditRecordItem] = []

        # If there are unresolved items, prepend them so they are immediately visible
        for unres in self.watcher._unresolved_records:
            all_candidates.append(unres)

        for rec in self.watcher._all_records:
            # Apply Filter
            if self.active_filter == "ALL":
                all_candidates.append(rec)
            elif self.active_filter == "ORDERS" and rec.category in ("stock_order", "option_order"):
                all_candidates.append(rec)
            elif self.active_filter == "HEDGES" and rec.category in ("reactive_hedge", "standing_overlay"):
                all_candidates.append(rec)
            elif self.active_filter == "RISKS" and rec.category in ("weight_clipped", "order_chunked", "basket_rebalance"):
                all_candidates.append(rec)
            elif self.active_filter == "BLOCKS" and rec.verdict in ("hard_block", "soft_block"):
                all_candidates.append(rec)

        self._displayed_records = all_candidates

        # Clear and refill table
        table.clear()
        for idx, item in enumerate(self._displayed_records):
            rule_str = item.rule_id or "—"
            reg_str = item.regulation_ref or "—"
            if len(rule_str) > 22:
                rule_str = rule_str[:20] + "…"
            if len(reg_str) > 20:
                reg_str = reg_str[:18] + "…"

            table.add_row(
                Text(item.time_display, style="#94a3b8"),
                item.category_badge,
                Text(item.symbol_display, style="bold #ffffff"),
                item.verdict_display,
                Text(rule_str, style="#cbd5e1"),
                Text(reg_str, style="dim #94a3b8"),
                Text(item.reason_summary, style="#e2e8f0"),
                key=str(idx),
            )

        # Restore or select first row
        if len(self._displayed_records) > 0:
            if current_cursor is not None and current_cursor < len(self._displayed_records):
                table.move_cursor(row=current_cursor)
            else:
                table.move_cursor(row=0)
            self._selected_record = self._displayed_records[table.cursor_row]
            self.query_one(EventDetailInspector).show_record(self._selected_record)

    def _sync_ui_components(self, unresolved_items: list[AuditRecordItem]) -> None:
        """Syncs metrics across Header, Side Panel, and Inspector."""
        # Header
        header = self.query_one(DashboardHeader)
        header.update_metrics(
            equity=self.account_equity,
            session_pnl=self.session_pnl_usd,
            positions_count=self.positions_count,
            unresolved_count=len(unresolved_items),
            chain_pass=self.chain_pass,
            is_paused=self.is_tail_paused,
            basket_data=self._basket_cache,
            account_unavailable=self._account_unavailable,
        )

        # Side Panel -- killswitch/throttle "tripped/paused" is the OR of the
        # instantaneous check (using the real threshold, when known) and the
        # sticky log-observed latch (`AuditLogWatcher._track_latch_state`):
        # the real rules never self-clear without an unlogged human reset,
        # so an instantaneous check alone can under-report a still-tripped
        # rule once the triggering condition passes.
        throttle_limit = self._policy_cfg.get("throttle_max_orders")
        instant_throttle_paused = throttle_limit is not None and self._throttle_count > throttle_limit
        throttle_paused = self.watcher.throttle_ever_paused or instant_throttle_paused

        killswitch_threshold = self._policy_cfg.get("killswitch_threshold_usd")
        instant_killswitch_tripped = (
            killswitch_threshold is not None
            and self.session_pnl_usd is not None
            and self.session_pnl_usd < killswitch_threshold
        )
        killswitch_tripped = self.watcher.killswitch_ever_tripped or instant_killswitch_tripped

        side_panel = self.query_one(RiskSidePanel)
        side_panel.update_panel(
            session_pnl=self.session_pnl_usd,
            killswitch_tripped=killswitch_tripped,
            killswitch_sticky=self.watcher.killswitch_ever_tripped and not instant_killswitch_tripped,
            throttle_count=self._throttle_count,
            throttle_limit=throttle_limit,
            throttle_paused=throttle_paused,
            throttle_sticky=self.watcher.throttle_ever_paused and not instant_throttle_paused,
            chain_pass=self.chain_pass,
            chain_count=self._chain_count,
            bad_idx=self._bad_idx,
            unresolved_items=unresolved_items,
            policy_cfg=self._policy_cfg,
            brief=self._latest_market_brief,
            cycle_summary=self._latest_cycle_summary,
            performance_summary=self._performance_summary,
            lifecycle_summary=self._lifecycle_summary,
        )

    def refresh_account_and_basket(self) -> None:
        """Fetches Alpaca account state and computes basket weights live."""
        if account_data is None:
            return

        try:
            # 1. Fetch Session PnL & Equity -- killswitch's instantaneous
            # trip-check against the REAL configured threshold (not a
            # hardcoded guess) now happens in `_sync_ui_components`, which
            # also has the real threshold from `self._policy_cfg`.
            pnl_res = account_data.fetch_session_pnl()
            if not pnl_res.ok or pnl_res.equity is None:
                raise RuntimeError(pnl_res.reason or "account equity unavailable")
            self.account_equity = pnl_res.equity
            self.session_pnl_usd = pnl_res.session_pnl_usd

            # 2. Fetch Positions
            pos_res = account_data.fetch_positions()
            if not pos_res.ok or pos_res.positions is None:
                raise RuntimeError(pos_res.reason or "positions unavailable")
            positions_map = pos_res.positions
            self.positions_count = len(positions_map)

            benchmark_closes = None
            if fetch_daily_bars is not None:
                benchmark_bars = fetch_daily_bars("SPY", 5)
                if benchmark_bars.ok:
                    benchmark_closes = benchmark_bars.closes
            if build_performance_summary is not None:
                self._performance_summary = build_performance_summary(
                    session_pnl_usd=pnl_res.session_pnl_usd if pnl_res.ok else None,
                    last_equity=getattr(pnl_res, "last_equity", None),
                    intraday_pnl=getattr(pos_res, "intraday_pnl", None) if pos_res.ok else None,
                    benchmark_closes=benchmark_closes,
                )

            # 3. Compute Inverse Volatility Weights & Clips
            if core_strategy is not None and fetch_daily_bars is not None:
                vols = {}
                for sym in core_strategy.BASKET:
                    bars = fetch_daily_bars(sym, 90)
                    if bars.ok and len(bars.closes) >= 2:
                        vols[sym] = core_strategy.compute_realized_volatility(bars.closes)
                if set(vols) != set(core_strategy.BASKET):
                    raise RuntimeError("incomplete basket market data")

                raw_weights = core_strategy.compute_inverse_vol_weights(vols)
                equity = self.account_equity or 100000.0
                budget = core_strategy.compute_total_budget_usd(equity)
                # Real position_cap ceiling read off the loaded policy
                # (`read_dashboard_policy_config`) -- 0.25 only as a last-
                # resort fallback when the policy couldn't be read at all,
                # matching position_cap's own shipped default.yaml value.
                position_cap_pct = self._policy_cfg.get("position_cap_max_pct_of_equity", 0.25)
                capped_weights, clips = core_strategy.clip_weights_to_position_cap(
                    raw_weights, budget, equity, position_cap_pct
                )

                # Same core_strategy.compute_weights_and_drift run_agent.py's
                # STEP 6 uses, fed the same kind of inputs (per-symbol USD
                # value + target weights) -- one shared source of "current
                # basket state," not a second, independently-computed
                # version of it. `positions_map` is already a USD market
                # value per symbol (account_data.fetch_positions), unlike
                # run_agent.py's qty-based positions, so it's passed
                # straight through rather than qty x price.
                weights_and_drift = core_strategy.compute_weights_and_drift(
                    positions_map, capped_weights, budget
                )
                weights_summary = {
                    sym: {**weights_and_drift.get(sym, {"current_w": 0.0, "target_w": 0.0, "drift": 0.0}),
                          "is_clipped": sym in clips}
                    for sym in core_strategy.BASKET
                }

                self._basket_cache = {
                    "status": "live",
                    "fetched_at": time.time(),
                    "weights": weights_summary,
                    "cash_w": max(0.0, 1.0 - sum(capped_weights.values())),
                }
        except Exception as exc:
            self._account_unavailable = True
            if "weights" in self._basket_cache:
                self._basket_cache["status"] = "stale"
                self._basket_cache["error"] = str(exc)
            else:
                self._basket_cache = {
                    "status": "unavailable",
                    "error": str(exc),
                }

    def verify_chain_periodically(self) -> None:
        """Verifies the SHA-256 cryptographic chain of the audit log."""
        if verify_chain is None or not self.audit_path.exists():
            return

        try:
            ok, bad_idx = verify_chain(self.audit_path)
            self.chain_pass = ok
            self._bad_idx = bad_idx

            # Count total lines
            with self.audit_path.open("r", encoding="utf-8") as f:
                self._chain_count = sum(1 for line in f if line.strip())
        except Exception:
            self.chain_pass = None

    # ─────────────────────────────────────────────────────────────────────────
    # EVENT HANDLERS & ACTIONS
    # ─────────────────────────────────────────────────────────────────────────

    @on(DataTable.RowHighlighted, "#audit-table")
    def on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Update Event Inspector when cursor moves."""
        if event.cursor_row is not None and 0 <= event.cursor_row < len(self._displayed_records):
            self._selected_record = self._displayed_records[event.cursor_row]
            self.query_one(EventDetailInspector).show_record(self._selected_record)

    @on(DataTable.RowSelected, "#audit-table")
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        """Selecting / pressing enter on a row toggles detailed inspector focus."""
        if event.cursor_row is not None and 0 <= event.cursor_row < len(self._displayed_records):
            self._selected_record = self._displayed_records[event.cursor_row]
            self.query_one(EventDetailInspector).show_record(self._selected_record)

    def action_refresh_data(self) -> None:
        """Action: Manual refresh trigger."""
        self.refresh_all_data()
        self.notify("Dashboard data refreshed", title="Refresh", severity="information")

    def action_toggle_pause(self) -> None:
        """Action: Toggle live tailing pause/resume."""
        self.is_tail_paused = not self.is_tail_paused
        status = "PAUSED" if self.is_tail_paused else "RESUMED"
        self.notify(f"Live tail stream {status}", title="Tail Control", severity="warning" if self.is_tail_paused else "information")
        self._sync_ui_components(self.watcher._unresolved_records)

    def action_cycle_filter(self) -> None:
        """Action: Cycle through event filters."""
        filters = ["ALL", "ORDERS", "HEDGES", "RISKS", "BLOCKS"]
        idx = filters.index(self.active_filter)
        self.active_filter = filters[(idx + 1) % len(filters)]
        self.query_one("#table-pane-header", Label).update(f"LIVE AUDIT STREAM  [Filter: {self.active_filter}]")
        self._update_table_view()
        self.notify(f"Active filter: {self.active_filter}", title="Filter Changed", severity="information")

    def action_verify_hash_chain(self) -> None:
        """Action: Re-verify cryptographic hash chain manually."""
        self.verify_chain_periodically()
        if self.chain_pass:
            self.notify(f"Chain Integrity Verified: {self._chain_count} records intact", title="Hash Chain", severity="information")
        else:
            self.notify(f"Chain Broken at Record #{self._bad_idx}", title="Hash Chain Tamper Alert", severity="error")

    def action_toggle_inspector(self) -> None:
        """Action: Expand or collapse the bottom detail drawer."""
        inspector = self.query_one("#detail-inspector")
        inspector.toggle_class("collapsed")

    def action_show_web_info(self) -> None:
        """Action: Display guidance on textual serve browser mode."""
        self.notify(
            "To serve this dashboard in browser: run 'textual serve dashboard/app.py'.\n"
            "Note: Browser mode is beta; use native terminal for live video demos.",
            title="Web Server Mode",
            timeout=8.0,
            severity="information",
        )


# ─────────────────────────────────────────────────────────────────────────────
# CLI ENTRYPOINT
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="MCP Trade Firewall Governance & Order-Flow Monitor")
    parser.add_argument("--audit-file", type=str, default="audit.jsonl", help="Path to audit.jsonl log file")
    parser.add_argument("--policy-file", type=str, default="policies/default.yaml", help="Path to policy YAML")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="Audit tail poll interval in seconds")
    parser.add_argument("--serve", action="store_true", help="Launch web server via textual serve")
    args = parser.parse_args()

    if args.serve:
        print("[*] Launching web server mode via textual serve...")
        print("[*] Command: textual serve dashboard/app.py")
        os.system("textual serve dashboard/app.py")
        return

    app = FirewallDashboardApp(
        audit_path=args.audit_file,
        policy_path=args.policy_file,
        poll_interval=args.poll_interval,
    )
    app.run()


if __name__ == "__main__":
    main()
