"""Tests for the audit log's append-only hash chain."""

import json
import subprocess
import sys
from pathlib import Path

from firewall.audit import GENESIS_HASH, AuditLogWriter, verify_chain


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
