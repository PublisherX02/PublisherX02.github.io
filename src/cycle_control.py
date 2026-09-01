"""Single-instance locking, stable IDs, and atomic cycle-state persistence."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_LOCK_FILE = Path(__file__).resolve().parent.parent / "data" / "run_agent.lock"
DEFAULT_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "current_cycle.json"


class CycleAlreadyRunning(RuntimeError):
    pass


def new_cycle_id(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S%fZ")


def client_order_id(cycle_id: str, symbol: str, side: str, sequence: int) -> str:
    """Stable, Alpaca-safe ID (maximum 48 characters)."""
    clean_symbol = re.sub(r"[^A-Za-z0-9]", "", symbol).upper()[-10:]
    clean_side = "b" if side.lower() == "buy" else "s"
    return f"mta-{cycle_id[:20]}-{clean_symbol}-{clean_side}-{sequence:02d}"[:48]


def cycle_id_for_run(prior_state: dict[str, Any] | None, recover: bool) -> tuple[str, bool]:
    """Reuse an interrupted cycle ID only for an explicit recovery pass."""
    interrupted = bool(
        prior_state and prior_state.get("status") in {"starting", "running"}
    )
    if recover and interrupted and prior_state and prior_state.get("cycle_id"):
        return str(prior_state["cycle_id"]), True
    return new_cycle_id(), False


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # ``os.kill(pid, 0)`` is not a reliable existence probe on Windows:
        # some Python/Windows combinations return success for a PID that does
        # not exist.  Ask the kernel for a query handle instead.
        import ctypes

        process_query_limited_information = 0x1000
        error_access_denied = 5
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if handle:
            kernel32.CloseHandle(handle)
            return True
        # A protected process still exists even when we cannot open it.
        return ctypes.get_last_error() == error_access_denied
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


@dataclass
class CycleLock:
    cycle_id: str
    path: Path = DEFAULT_LOCK_FILE
    acquired: bool = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"pid": os.getpid(), "cycle_id": self.cycle_id,
                   "acquired_at": datetime.now(timezone.utc).isoformat()}
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream)
                self.acquired = True
                return
            except FileExistsError:
                try:
                    owner = json.loads(self.path.read_text(encoding="utf-8"))
                    owner_pid = int(owner.get("pid", -1))
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    owner_pid = -1
                if _pid_alive(owner_pid):
                    raise CycleAlreadyRunning(
                        f"cycle {owner.get('cycle_id', 'unknown')} is already running "
                        f"under PID {owner_pid}"
                    )
                # Exact stale lock verified by PID before removal.
                self.path.unlink(missing_ok=True)
        raise CycleAlreadyRunning("could not acquire cycle lock")

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            owner = json.loads(self.path.read_text(encoding="utf-8"))
            if owner.get("cycle_id") == self.cycle_id and owner.get("pid") == os.getpid():
                self.path.unlink(missing_ok=True)
        finally:
            self.acquired = False

    def __enter__(self) -> "CycleLock":
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def write_cycle_state(
    cycle_id: str,
    status: str,
    *,
    state_file: Path | str = DEFAULT_STATE_FILE,
    details: dict[str, Any] | None = None,
) -> None:
    path = Path(state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"cycle_id": cycle_id, "status": status,
               "updated_at": datetime.now(timezone.utc).isoformat(), **(details or {})}
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    temp.replace(path)


def load_cycle_state(state_file: Path | str = DEFAULT_STATE_FILE) -> dict[str, Any] | None:
    try:
        payload = json.loads(Path(state_file).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        return None
