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
UpstreamStatus = Literal["ok", "error", "not_forwarded"]


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
    forwarded: bool
    upstream_status: UpstreamStatus
    prev_hash: str


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
        forwarded: bool,
        upstream_status: UpstreamStatus,
        rule_id: str | None = None,
        regulation_ref: str | None = None,
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
        )
        line = event.model_dump_json()
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        self._prev_hash = hashlib.sha256(line.encode("utf-8")).hexdigest()
        return event


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


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m firewall.audit")
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify_parser = subparsers.add_parser(
        "verify", help="verify a JSONL audit log's hash chain"
    )
    verify_parser.add_argument("path", type=Path)
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

    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
