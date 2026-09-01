"""Non-mutating C1/C2 verification against a temporary copy of current audit.jsonl."""

import hashlib
import json
import tempfile
from pathlib import Path

from firewall.audit import verify_chain


source = Path("audit.jsonl")
original = source.read_text(encoding="utf-8").splitlines()
print(f"records={len(original)} current={verify_chain(source)}")


def check(name, mutate):
    lines = list(original)
    mutate(lines)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "audit.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"{name}={verify_chain(path)}")


points = sorted({1, len(original) // 2, len(original) - 2})
for point in points:
    def edit(lines, index=point):
        record = json.loads(lines[index])
        record["reason"] = "AUDIT-TAMPER-PROBE"
        lines[index] = json.dumps(record, separators=(",", ":"))
    check(f"edit_{point}", edit)


check("delete_middle", lambda lines: lines.pop(len(lines) // 2))
check("swap_middle", lambda lines: lines.__setitem__(
    slice(len(lines) // 2, len(lines) // 2 + 2),
    list(reversed(lines[len(lines) // 2:len(lines) // 2 + 2])),
))


def edit_last(lines):
    record = json.loads(lines[-1])
    record["reason"] = "LAST-RECORD-TAMPER-PROBE"
    lines[-1] = json.dumps(record, separators=(",", ":"))


check("edit_last_known_limitation", edit_last)


def forge_suffix(lines):
    cut = len(lines) // 2
    prefix = lines[:cut]
    previous = hashlib.sha256(prefix[-1].encode()).hexdigest()
    forged = []
    for index in range(len(lines) - cut):
        record = json.loads(lines[cut + index])
        record["tool_name"] = "forged_tool"
        record["arguments"] = {"qty": 999999}
        record["prev_hash"] = previous
        line = json.dumps(record, separators=(",", ":"))
        forged.append(line)
        previous = hashlib.sha256(line.encode()).hexdigest()
    lines[:] = prefix + forged


check("forged_suffix_known_limitation", forge_suffix)
