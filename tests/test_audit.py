"""Tests for the audit log's append-only hash chain."""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from firewall.audit import (
    GENESIS_HASH,
    AuditLogWriter,
    compute_record_hash,
    find_unresolved_pending,
    verify_chain,
)


def _write_records(path: Path, count: int) -> None:
    writer = AuditLogWriter(path)
    for i in range(count):
        writer.append(
            tool_name=f"tool_{i}",
            arguments={"i": i},
            verdict="allow",
            reason="ok",
            forwarded=True,
            upstream_status="ok",
        )


def test_first_record_uses_genesis_hash(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write_records(path, 1)

    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])

    assert record["prev_hash"] == GENESIS_HASH


def test_verify_chain_passes_for_untampered_log(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write_records(path, 100)

    ok, bad_index = verify_chain(path)

    assert ok is True
    assert bad_index is None


def test_verify_chain_detects_mutation_at_the_right_index(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write_records(path, 100)

    lines = path.read_text(encoding="utf-8").splitlines()
    tampered_index = 50
    record = json.loads(lines[tampered_index])
    record["tool_name"] = "tampered"
    lines[tampered_index] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok, bad_index = verify_chain(path)

    assert ok is False
    # Tampering with record i changes its serialized bytes, which breaks
    # the hash that record i + 1 stores of its predecessor -- that's where
    # the break is first detectable.
    assert bad_index == tampered_index + 1


def test_cli_verify_exits_zero_for_untampered_log(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write_records(path, 10)

    result = subprocess.run(
        [sys.executable, "-m", "firewall.audit", "verify", str(path)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0


def test_cli_verify_exits_nonzero_for_tampered_log(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write_records(path, 10)

    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[3])
    record["tool_name"] = "tampered"
    lines[3] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "firewall.audit", "verify", str(path)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1


# --- pending/outcome record pairs (crash-safe audit logging) --------------


def test_append_accepts_a_pending_record_with_null_forwarded(tmp_path):
    """A 'pending' record is written before a call is forwarded, so
    forwarded/upstream_status can't be known yet -- forwarded must accept
    None (not just True/False), and upstream_status must accept the new
    "pending" value."""
    path = tmp_path / "audit.jsonl"
    writer = AuditLogWriter(path)

    event = writer.append(
        tool_name="place_stock_order",
        arguments={"symbol": "AAPL"},
        verdict="allow",
        reason="no rule triggered",
        forwarded=None,
        upstream_status="pending",
        call_id="call-1",
    )

    assert event.forwarded is None
    assert event.upstream_status == "pending"
    assert event.call_id == "call-1"

    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["forwarded"] is None
    assert record["upstream_status"] == "pending"
    assert record["call_id"] == "call-1"


def test_compute_record_hash_matches_the_real_chain_hash(tmp_path):
    """compute_record_hash must recompute the exact same hash the writer
    uses internally to link the *next* record's prev_hash -- otherwise a
    caller using it to reference an earlier record (e.g. an outcome record
    pointing back at its pending record) would store a value that doesn't
    actually correspond to anything in the chain."""
    path = tmp_path / "audit.jsonl"
    writer = AuditLogWriter(path)

    pending_event = writer.append(
        tool_name="place_stock_order",
        arguments={"symbol": "AAPL"},
        verdict="allow",
        reason="no rule triggered",
        forwarded=None,
        upstream_status="pending",
        call_id="call-1",
    )
    outcome_event = writer.append(
        tool_name="place_stock_order",
        arguments={"symbol": "AAPL"},
        verdict="allow",
        reason="no rule triggered",
        forwarded=True,
        upstream_status="ok",
        call_id="call-1",
        pending_hash=compute_record_hash(pending_event),
    )

    lines = path.read_text(encoding="utf-8").splitlines()
    outcome_record_on_disk = json.loads(lines[1])

    assert compute_record_hash(pending_event) == outcome_record_on_disk["prev_hash"]
    assert outcome_event.pending_hash == outcome_record_on_disk["prev_hash"]


def test_pending_outcome_pair_does_not_mutate_the_pending_record(tmp_path):
    """Two linked records, not one edited record: the pending record's
    bytes on disk must be untouched once the outcome record is appended --
    an append-only hash chain that ever rewrote an earlier line would
    defeat the tamper-evidence the chain exists for."""
    path = tmp_path / "audit.jsonl"
    writer = AuditLogWriter(path)

    writer.append(
        tool_name="place_stock_order",
        arguments={"symbol": "AAPL"},
        verdict="allow",
        reason="no rule triggered",
        forwarded=None,
        upstream_status="pending",
        call_id="call-1",
    )
    pending_line_before = path.read_text(encoding="utf-8").splitlines()[0]

    writer.append(
        tool_name="place_stock_order",
        arguments={"symbol": "AAPL"},
        verdict="allow",
        reason="no rule triggered",
        forwarded=True,
        upstream_status="ok",
        call_id="call-1",
    )
    lines_after = path.read_text(encoding="utf-8").splitlines()

    assert len(lines_after) == 2
    assert lines_after[0] == pending_line_before

    ok, bad_index = verify_chain(path)
    assert ok is True
    assert bad_index is None


def test_find_unresolved_pending_flags_a_pending_record_with_no_outcome(tmp_path):
    path = tmp_path / "audit.jsonl"
    writer = AuditLogWriter(path)
    old_timestamp = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    writer.append(
        tool_name="place_stock_order",
        arguments={"symbol": "AAPL"},
        verdict="allow",
        reason="no rule triggered",
        forwarded=None,
        upstream_status="pending",
        call_id="orphaned-call",
    )
    # Backdate the record directly on disk -- simulates real elapsed time
    # without needing to sleep in the test.
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["timestamp"] = old_timestamp
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    stale = find_unresolved_pending(path, stale_after_seconds=60.0)

    assert len(stale) == 1
    assert stale[0].call_id == "orphaned-call"


def test_find_unresolved_pending_does_not_flag_a_resolved_pair(tmp_path):
    path = tmp_path / "audit.jsonl"
    writer = AuditLogWriter(path)
    old_timestamp = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    writer.append(
        tool_name="place_stock_order",
        arguments={"symbol": "AAPL"},
        verdict="allow",
        reason="no rule triggered",
        forwarded=None,
        upstream_status="pending",
        call_id="resolved-call",
    )
    writer.append(
        tool_name="place_stock_order",
        arguments={"symbol": "AAPL"},
        verdict="allow",
        reason="no rule triggered",
        forwarded=True,
        upstream_status="ok",
        call_id="resolved-call",
    )
    lines = path.read_text(encoding="utf-8").splitlines()
    pending_record = json.loads(lines[0])
    pending_record["timestamp"] = old_timestamp
    lines[0] = json.dumps(pending_record)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    stale = find_unresolved_pending(path, stale_after_seconds=60.0)

    assert stale == []


def test_find_unresolved_pending_does_not_flag_a_recent_pending_record(tmp_path):
    """A pending record that's only milliseconds old is a call in flight,
    not a crash -- it must not be flagged until it's actually stale."""
    path = tmp_path / "audit.jsonl"
    writer = AuditLogWriter(path)

    writer.append(
        tool_name="place_stock_order",
        arguments={"symbol": "AAPL"},
        verdict="allow",
        reason="no rule triggered",
        forwarded=None,
        upstream_status="pending",
        call_id="in-flight-call",
    )

    stale = find_unresolved_pending(path, stale_after_seconds=60.0)

    assert stale == []
