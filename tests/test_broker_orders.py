import asyncio
import json
from types import SimpleNamespace

from broker_orders import (
    BrokerOrderReceipt,
    LifecycleJournal,
    parse_broker_order_result,
    poll_broker_order_terminal,
    recover_pending_order_events,
    reconcile_broker_order,
)


def _result(payload, *, is_error=False):
    return SimpleNamespace(
        is_error=is_error,
        content=[SimpleNamespace(text=json.dumps(payload))],
    )


def test_parses_direct_filled_order():
    receipt = parse_broker_order_result(_result({
        "id": "order-1",
        "client_order_id": "cycle-aapl-1",
        "status": "filled",
        "filled_qty": "10",
        "filled_avg_price": "201.25",
    }))
    assert receipt.order_id == "order-1"
    assert receipt.filled is True
    assert receipt.filled_qty == 10
    assert receipt.filled_avg_price == 201.25


def test_parses_realistic_mcp_security_wrapper_without_claiming_fill():
    receipt = parse_broker_order_result(_result({
        "_alpaca_mcp_security": {"paper": True},
        "data": {"result": {"order_id": "order-2", "status": "accepted"}},
    }))
    assert receipt.order_id == "order-2"
    assert receipt.status == "accepted"
    assert receipt.submitted is True
    assert receipt.filled is False


def test_unparseable_success_is_submitted_unconfirmed_not_filled():
    result = SimpleNamespace(is_error=False, content=[SimpleNamespace(text="order accepted")])
    receipt = parse_broker_order_result(result)
    assert receipt.status == "submitted_unconfirmed"
    assert receipt.submitted is True
    assert receipt.filled is False


def test_dry_run_receipt_is_never_marked_submitted():
    receipt = parse_broker_order_result(_result({
        "id": "dry-run-1", "client_order_id": "cycle-aapl-1", "status": "dry_run"
    }))
    assert receipt.status == "dry_run"
    assert receipt.submitted is False
    assert receipt.filled is False


def test_reconciliation_refreshes_to_filled():
    class Client:
        async def call_tool(self, name, arguments, raise_on_error=False):
            assert name == "get_order_by_id"
            assert arguments == {"order_id": "order-3"}
            return _result({
                "id": "order-3", "status": "filled",
                "filled_qty": "1", "filled_avg_price": "2.40",
            })

    initial = BrokerOrderReceipt("order-3", None, "accepted", True, False, raw_parseable=True)
    refreshed = asyncio.run(reconcile_broker_order(Client(), initial))
    assert refreshed.filled is True
    assert refreshed.filled_avg_price == 2.40


def test_reconciliation_failure_preserves_original_receipt():
    class Client:
        async def call_tool(self, *args, **kwargs):
            raise RuntimeError("network")

    initial = BrokerOrderReceipt("order-4", None, "accepted", True, False, raw_parseable=True)
    assert asyncio.run(reconcile_broker_order(Client(), initial)) == initial


def test_reconciliation_falls_back_to_idempotent_client_order_id():
    class Client:
        async def call_tool(self, name, arguments, raise_on_error=False):
            assert name == "get_order_by_client_id"
            assert arguments == {"client_order_id": "cycle-aapl-1"}
            return _result({
                "id": "resolved-order", "client_order_id": "cycle-aapl-1",
                "status": "accepted",
            })

    initial = BrokerOrderReceipt(
        None, "cycle-aapl-1", "submitted_unconfirmed", True, False
    )
    refreshed = asyncio.run(reconcile_broker_order(Client(), initial))
    assert refreshed.order_id == "resolved-order"
    assert refreshed.status == "accepted"


def test_terminal_poll_records_partial_fill_then_fill(tmp_path):
    responses = iter([
        {"id": "order-5", "client_order_id": "client-5", "status": "partially_filled", "filled_qty": "2"},
        {"id": "order-5", "client_order_id": "client-5", "status": "filled", "filled_qty": "5"},
    ])

    class Client:
        async def call_tool(self, *args, **kwargs):
            return _result(next(responses))

    journal = LifecycleJournal(tmp_path / "lifecycle.json")
    initial = BrokerOrderReceipt("order-5", "client-5", "accepted", True, False)
    final = asyncio.run(poll_broker_order_terminal(
        Client(), initial, max_attempts=3, poll_interval_seconds=0, journal=journal,
        context={"symbol": "AAPL"},
    ))
    assert final.status == "filled"
    entry = journal.load()["client-5"]
    assert entry["terminal"] is True
    assert [item["status"] for item in entry["history"]] == [
        "accepted", "partially_filled", "filled"
    ]


def test_terminal_poll_timeout_stays_unresolved(tmp_path):
    class Client:
        async def call_tool(self, *args, **kwargs):
            return _result({"id": "order-6", "client_order_id": "client-6", "status": "accepted"})

    journal = LifecycleJournal(tmp_path / "lifecycle.json")
    initial = BrokerOrderReceipt("order-6", "client-6", "accepted", True, False)
    final = asyncio.run(poll_broker_order_terminal(
        Client(), initial, max_attempts=2, poll_interval_seconds=0, journal=journal,
    ))
    assert final.status == "accepted"
    assert journal.unresolved("client-6") is True


def test_lifecycle_journal_dry_run_is_terminal_but_not_submitted(tmp_path):
    journal = LifecycleJournal(tmp_path / "lifecycle.json")
    journal.record(BrokerOrderReceipt(
        "dry-1", "client-dry", "dry_run", False, False, raw_parseable=True
    ))
    entry = journal.load()["client-dry"]
    assert entry["terminal"] is True
    assert entry["submitted"] is False
    assert journal.unresolved("client-dry") is False


def test_restart_recovery_queries_orphan_by_client_id_without_placing_order(tmp_path):
    calls = []

    class Client:
        async def call_tool(self, name, arguments, raise_on_error=False):
            calls.append((name, arguments))
            return _result({
                "id": "broker-7", "client_order_id": "client-7", "status": "filled",
                "filled_qty": "3",
            })

    event = SimpleNamespace(
        tool_name="place_stock_order",
        call_id="crashed-call",
        arguments={"symbol": "AAPL", "side": "buy", "qty": "3", "client_order_id": "client-7"},
    )
    journal = LifecycleJournal(tmp_path / "lifecycle.json")
    receipts = asyncio.run(recover_pending_order_events(
        Client(), [event], journal=journal, max_attempts=1, poll_interval_seconds=0,
    ))
    assert calls == [("get_order_by_client_id", {"client_order_id": "client-7"})]
    assert receipts[0].status == "filled"
    assert journal.load()["client-7"]["recovered_from_call_id"] == "crashed-call"
