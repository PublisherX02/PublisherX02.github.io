"""Test suite for Textual Dashboard (dashboard/app.py)."""

import asyncio
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
import sys

# Ensure src/ and workspace root are on sys.path
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = WORKSPACE_ROOT / "src"
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from dashboard.app import (
    FirewallDashboardApp,
    AuditLogWatcher,
    AuditRecordItem,
    load_latest_cycle_artifact,
    summarize_cycle_artifact,
    load_lifecycle_summary,
)
from firewall.audit import AuditEvent, AuditLogWriter, GENESIS_HASH


def test_latest_cycle_summary_never_calls_submitted_orders_filled(tmp_path):
    cycle = {
        "cycle_id": "20260901T120000Z",
        "finished_at": "2026-09-01T12:00:00+00:00",
        "budget_usd": 90000,
        "submitted_stock_orders": [
            {"broker": {"status": "accepted", "filled": False}},
            {"broker": {"status": "filled", "filled": True}},
        ],
        "submitted_option_orders": [
            {"broker": {"status": "new", "filled": False}},
        ],
        "blocked_orders": [{"symbol": "AAPL"}],
    }
    path = tmp_path / "20260901T120000Z.json"
    path.write_text(json.dumps(cycle), encoding="utf-8")

    loaded = load_latest_cycle_artifact(tmp_path)
    summary = summarize_cycle_artifact(
        loaded, now=datetime(2026, 9, 1, 12, 5, tzinfo=timezone.utc)
    )
    assert summary["submitted"] == 3
    assert summary["filled"] == 1
    assert summary["blocked"] == 1
    assert summary["statuses"] == {"accepted": 1, "filled": 1, "new": 1}
    assert summary["budget_usd"] == 90000


def test_latest_cycle_loader_skips_malformed_newest_file(tmp_path):
    (tmp_path / "20260901T130000Z.json").write_text("{partial", encoding="utf-8")
    (tmp_path / "20260901T120000Z.json").write_text(
        json.dumps({"cycle_id": "valid"}), encoding="utf-8"
    )
    assert load_latest_cycle_artifact(tmp_path)["cycle_id"] == "valid"


def test_lifecycle_summary_surfaces_nonterminal_and_recovered(tmp_path):
    path = tmp_path / "lifecycle.json"
    path.write_text(json.dumps({
        "client-1": {"status": "partially_filled", "submitted": True, "terminal": False},
        "client-2": {
            "status": "filled", "submitted": True, "terminal": True,
            "recovered_from_call_id": "crashed-call",
        },
        "client-3": {"status": "dry_run", "submitted": False, "terminal": True},
    }), encoding="utf-8")
    summary = load_lifecycle_summary(path)
    assert summary["total"] == 3
    assert summary["unresolved"] == 1
    assert summary["recovered"] == 1
    assert summary["statuses"] == {"partially_filled": 1, "filled": 1, "dry_run": 1}


def test_dashboard_full_lifecycle():
    async def _runner():
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "audit.jsonl"
            writer = AuditLogWriter(log_file)

            # 1. Normal order with pending -> outcome
            e_pend = writer.append(
                tool_name="place_stock_order",
                arguments={"symbol": "AAPL", "qty": "10", "side": "buy"},
                verdict="allow",
                reason="no rule triggered",
                forwarded=None,
                upstream_status="pending",
                call_id="call-1",
            )
            writer.append(
                tool_name="place_stock_order",
                arguments={"symbol": "AAPL", "qty": "10", "side": "buy"},
                verdict="allow",
                reason="no rule triggered",
                forwarded=True,
                upstream_status="ok",
                call_id="call-1",
                pending_hash=e_pend.prev_hash,
            )

            # 2. Standing options overlay
            writer.append(
                tool_name="scheduled_overlay:proposed",
                arguments={
                    "symbol": "SPY",
                    "occ_symbol": "SPY260925P00731000",
                    "contracts": 1,
                    "strike": 731.0,
                    "target_expiry": "2026-09-25",
                },
                verdict="info",
                reason="SCHEDULED OPTIONS OVERLAY standing portfolio insurance",
                forwarded=None,
                upstream_status="not_forwarded",
                rule_id="scheduled-options-overlay",
            )

            # 3. Reactive hedge proposal
            writer.append(
                tool_name="hedge_proposal:detected",
                arguments={"symbol": "SPY", "trigger": "cvar_early_warning"},
                verdict="soft_block",
                reason="HEDGE PROPOSAL: tail loss crossed threshold",
                forwarded=None,
                upstream_status="not_forwarded",
                rule_id="hedge-proposal",
            )

            # 4. Weight clipped
            writer.append(
                tool_name="basket_rebalance:weight_clipped",
                arguments={
                    "symbol": "SPY",
                    "raw_target_weight": 0.44,
                    "capped_target_weight": 0.278,
                    "ceiling_usd": 25000.0,
                    "total_budget_usd": 90000.0,
                },
                verdict="info",
                reason="SPY raw weight 44.0% clipped to 27.8%",
                forwarded=None,
                upstream_status="not_forwarded",
                rule_id="basket-rebalance-position-cap-clip",
            )

            # 5. Order chunked
            writer.append(
                tool_name="basket_rebalance:order_chunked",
                arguments={
                    "symbol": "MSFT",
                    "side": "buy",
                    "total_qty": 24,
                    "chunk_count": 2,
                    "chunk_sizes": [12, 12],
                },
                verdict="info",
                reason="MSFT 24 shares split into 2 chunks",
                forwarded=None,
                upstream_status="not_forwarded",
                rule_id="basket-rebalance-chunking",
            )

            # 6. Unresolved pending record (simulating process crash)
            old_pending = AuditEvent(
                event_id="unresolved-event-id",
                timestamp="2026-08-01T00:00:00+00:00",
                session_id="test-session",
                tool_name="place_stock_order",
                arguments={"symbol": "QQQ", "qty": "50", "side": "buy"},
                verdict="allow",
                rule_id=None,
                regulation_ref=None,
                reason="no rule triggered",
                forwarded=None,
                upstream_status="pending",
                prev_hash=writer._prev_hash,
                call_id="crashed-call-99",
                pending_hash=None,
            )
            with log_file.open("a", encoding="utf-8") as f:
                f.write(old_pending.model_dump_json() + "\n")

            app = FirewallDashboardApp(audit_path=log_file, poll_interval=0.1)
            async with app.run_test() as pilot:
                await pilot.pause()
                table = app.query_one("#audit-table")

                # Check categorized items
                categories = [item.category for item in app._displayed_records]
                assert "reactive_hedge" in categories
                assert "standing_overlay" in categories
                assert "weight_clipped" in categories
                assert "order_chunked" in categories
                assert "stock_order" in categories

                # Check that pending record was NOT shown as raw pending outcome
                raw_pending_shown = any(
                    item.upstream_status == "pending" and not item.is_unresolved
                    for item in app._displayed_records
                )
                assert not raw_pending_shown, "Raw pending records should not be displayed as final outcomes"

                # Check that unresolved pending call was surfaced
                unresolved_surfaced = any(item.is_unresolved for item in app._displayed_records)
                assert unresolved_surfaced, "Unresolved pending call should be surfaced in UI"

                # Test live appending new record
                writer.append(
                    tool_name="place_stock_order",
                    arguments={"symbol": "MSFT", "qty": "5", "side": "buy"},
                    verdict="hard_block",
                    reason="order-rate-throttle limit exceeded",
                    forwarded=False,
                    upstream_status="not_forwarded",
                    rule_id="order-rate-throttle",
                    regulation_ref="SEC Rule 15c3-5(c)(1)(ii)",
                )

                # Wait for poll
                await pilot.pause(0.3)
                assert any(
                    item.rule_id == "order-rate-throttle" for item in app._displayed_records
                ), "Live appended record was not found after poll"

    asyncio.run(_runner())
