"""Audit log writer.

Append-only JSONL audit log for every tool call the firewall intercepts.
Each record links to the previous one via a SHA-256 hash chain (prev_hash),
making after-the-fact tampering of the log file detectable by
`verify_chain`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

GENESIS_HASH = "0" * 64

Verdict = Literal[
    "allow", "soft_block", "hard_block", "state_entered", "state_exited"
]
# "pending" marks a record written before a call was actually forwarded --
# forwarded/upstream_status aren't knowable yet at that point. See
# `find_unresolved_pending` and `PolicyEngine.record_call_pending`.
UpstreamStatus = Literal["ok", "error", "not_forwarded", "pending"]


class AuditEvent(BaseModel):
    """A single recorded event: a tool call, the decision made on it, and
    its link to the previous record in the chain.

    `state_entered`/`state_exited` are a different kind of record from the
    other three verdicts: they mark a stateful rule (e.g.
    cooldown_after_loss) crossing a state boundary, not a decision on the
    call in `tool_name`/`arguments` itself -- that call is just what was
    being evaluated at the moment the transition was noticed. `rule_id`
    identifies which rule's state changed; a dashboard groups
    state_entered/state_exited pairs by `rule_id` to render one continuous
    state span instead of reconstructing it from per-call blocks.

    `call_id`/`pending_hash` implement crash-safe logging for allow/
    soft_block calls specifically: `PolicyEngine.record_call_pending`
    writes a "pending" record (forwarded=None, upstream_status="pending")
    before the caller attempts to forward the call, and
    `PolicyEngine.record_call_outcome` later writes a second, separate
    "outcome" record carrying the real forwarded/upstream_status values,
    the same `call_id`, and `pending_hash` set to
    `compute_record_hash(pending_event)` -- an explicit reference back to
    the pending record, independent of the two being adjacent in the file.
    The pending record itself is never mutated; if the process dies
    between the two writes, the pending record alone remains as proof a
    call was attempted (see `find_unresolved_pending`). Both fields are
    None on every other record (hard_block, state_entered/exited, or any
    record written without this pattern).
    """

    event_id: str
    timestamp: str
    session_id: str
    tool_name: str
    arguments: dict[str, Any]
    verdict: Verdict
    rule_id: str | None
    regulation_ref: str | None
    reason: str
    forwarded: bool | None
    upstream_status: UpstreamStatus
    prev_hash: str
    call_id: str | None = None
    pending_hash: str | None = None


class AuditLogWriter:
    """Appends AuditEvent records to a JSONL file, chaining each record's
    hash to the previous one.

    If `log_path` already contains records, the chain resumes from the hash
    of its last line rather than restarting at the genesis hash.
    """

    def __init__(self, log_path: Path | str, session_id: str | None = None) -> None:
        self.log_path = Path(log_path)
        self.session_id = session_id or str(uuid.uuid4())
        self._prev_hash = self._read_last_hash()

    def _read_last_hash(self) -> str:
        if not self.log_path.exists():
            return GENESIS_HASH
        last_line = ""
        with self.log_path.open("r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.rstrip("\n")
                if line:
                    last_line = line
        if not last_line:
            return GENESIS_HASH
        return hashlib.sha256(last_line.encode("utf-8")).hexdigest()

    def append(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        verdict: Verdict,
        reason: str,
        forwarded: bool | None,
        upstream_status: UpstreamStatus,
        rule_id: str | None = None,
        regulation_ref: str | None = None,
        call_id: str | None = None,
        pending_hash: str | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            event_id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc).isoformat(),
            session_id=self.session_id,
            tool_name=tool_name,
            arguments=arguments,
            verdict=verdict,
            rule_id=rule_id,
            regulation_ref=regulation_ref,
            reason=reason,
            forwarded=forwarded,
            upstream_status=upstream_status,
            prev_hash=self._prev_hash,
            call_id=call_id,
            pending_hash=pending_hash,
        )
        line = event.model_dump_json()
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        self._prev_hash = hashlib.sha256(line.encode("utf-8")).hexdigest()
        return event


def compute_record_hash(event: AuditEvent) -> str:
    """Hash of `event`'s serialized line, exactly as it would appear (or
    already appears) in the JSONL file -- i.e. the same value the *next*
    record's `prev_hash` carries if `event` is the immediately preceding
    line. Lets a caller reference an earlier record it already holds (e.g.
    an outcome record linking back to its pending record via
    `pending_hash`) without depending on the writer's internal
    `_prev_hash` state, which may have moved on if another record was
    appended in between."""
    return hashlib.sha256(event.model_dump_json().encode("utf-8")).hexdigest()


def verify_chain(path: Path | str) -> tuple[bool, int | None]:
    """Verify the prev_hash chain of a JSONL audit log.

    Returns (True, None) if every record's prev_hash matches the SHA-256 of
    the previous record's raw serialized line, and the first record's
    prev_hash is the genesis hash of 64 zeros.

    Otherwise returns (False, i), where i is the 0-based index of the first
    record whose stated prev_hash does not match. A hash chain only proves
    a record's link to its *predecessor*, so tampering with record i's
    content is detected at record i + 1 -- whose stored hash no longer
    matches the mutated predecessor. Tampering with the last record in the
    file is therefore undetectable from the file alone.
    """
    path = Path(path)
    expected_prev = GENESIS_HASH
    with path.open("r", encoding="utf-8") as f:
        index = 0
        for raw_line in f:
            line = raw_line.rstrip("\n")
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                return False, index
            if record.get("prev_hash") != expected_prev:
                return False, index
            expected_prev = hashlib.sha256(line.encode("utf-8")).hexdigest()
            index += 1
    return True, None


def find_unresolved_pending(
    path: Path | str,
    *,
    now: datetime | None = None,
    stale_after_seconds: float = 60.0,
) -> list[AuditEvent]:
    """Scan a JSONL audit log for "pending" records (written by
    `PolicyEngine.record_call_pending` before a call was forwarded) that
    have no matching "outcome" record -- a later record sharing the same
    `call_id` with a non-"pending" `upstream_status`, written by
    `PolicyEngine.record_call_outcome` once forwarding actually completed
    -- and are older than `stale_after_seconds`.

    An unresolved pending record almost always means the process died
    between the two writes (see `FirewallMiddleware.on_call_tool`): the
    call was attempted and policy-evaluated, but whether it ever reached
    upstream, and what happened if it did, is genuinely unknown. A
    dashboard or CLI should surface these as "call attempted, outcome
    unknown" rather than silently treating a missing outcome as either
    forwarded or not.

    A pending record younger than `stale_after_seconds` is not flagged --
    it may simply be a call still in flight. `now` defaults to the current
    time; pass it explicitly for deterministic tests.
    """
    now = now if now is not None else datetime.now(timezone.utc)
    path = Path(path)
    if not path.exists():
        return []

    pending_by_call_id: dict[str, AuditEvent] = {}
    resolved_call_ids: set[str] = set()

    with path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")
            if not line:
                continue
            event = AuditEvent.model_validate_json(line)
            if event.call_id is None:
                continue
            if event.upstream_status == "pending":
                pending_by_call_id[event.call_id] = event
            else:
                resolved_call_ids.add(event.call_id)

    stale: list[AuditEvent] = []
    for call_id, event in pending_by_call_id.items():
        if call_id in resolved_call_ids:
            continue
        age_seconds = (now - datetime.fromisoformat(event.timestamp)).total_seconds()
        if age_seconds >= stale_after_seconds:
            stale.append(event)
    return stale


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m firewall.audit")
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify_parser = subparsers.add_parser(
        "verify", help="verify a JSONL audit log's hash chain"
    )
    verify_parser.add_argument("path", type=Path)
    pending_parser = subparsers.add_parser(
        "pending", help="list stale 'pending' records with no matching outcome"
    )
    pending_parser.add_argument("path", type=Path)
    pending_parser.add_argument(
        "--stale-after-seconds",
        type=float,
        default=60.0,
        help="minimum age before an unresolved pending record is flagged (default: 60)",
    )
    args = parser.parse_args(argv)

    if args.command == "verify":
        ok, bad_index = verify_chain(args.path)
        if ok:
            print(f"OK: chain verified ({args.path})")
            return 0
        print(
            f"TAMPERED: chain broken at record index {bad_index} ({args.path})",
            file=sys.stderr,
        )
        return 1

    if args.command == "pending":
        stale = find_unresolved_pending(
            args.path, stale_after_seconds=args.stale_after_seconds
        )
        if not stale:
            print(
                f"OK: no unresolved pending records older than "
                f"{args.stale_after_seconds}s ({args.path})"
            )
            return 0
        for event in stale:
            print(
                f"UNRESOLVED: call_id={event.call_id} tool_name={event.tool_name!r} "
                f"timestamp={event.timestamp} verdict={event.verdict} -- "
                "call attempted, outcome unknown",
                file=sys.stderr,
            )
        return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
