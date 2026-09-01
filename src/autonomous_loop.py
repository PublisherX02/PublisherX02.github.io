"""Bounded scheduler for repeated trading-agent cycles."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from firewall.account_data import AccountPnLResult, MarketClockResult, fetch_market_clock, fetch_session_pnl
from cycle_control import load_cycle_state

DEFAULT_HEARTBEAT_FILE = Path(__file__).resolve().parent.parent / "data" / "runtime_heartbeat.json"


def write_heartbeat(payload: dict[str, Any], path: Path = DEFAULT_HEARTBEAT_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temp.replace(path)


@dataclass(frozen=True)
class LoopConfig:
    interval_seconds: float = 60.0
    closed_market_poll_seconds: float = 300.0
    failure_backoff_seconds: float = 30.0
    max_cycles: int | None = None
    dry_run: bool = True
    budget: float | None = None
    expected_account_id: str | None = None
    include_options: bool = False
    heartbeat_file: Path = DEFAULT_HEARTBEAT_FILE


def cycle_command(config: LoopConfig) -> list[str]:
    command = [sys.executable, "-m", "run_agent"]
    if config.dry_run:
        command.append("--dry-run")
    else:
        command.append("--execute")
    if config.budget is not None:
        command.extend(["--budget", str(config.budget)])
    if config.expected_account_id:
        command.extend(["--expected-account-id", config.expected_account_id])
    if not config.include_options:
        command.append("--no-overlay")
    return command


def run_loop(
    config: LoopConfig,
    *,
    clock_fetcher: Callable[[], MarketClockResult] = fetch_market_clock,
    account_fetcher: Callable[[], AccountPnLResult] = fetch_session_pnl,
    cycle_runner: Callable[[list[str]], int] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run bounded cycles only while Alpaca reports the market open."""
    if not config.dry_run and not config.expected_account_id:
        raise ValueError("live paper loops require --expected-account-id")
    runner = cycle_runner or (lambda command: subprocess.run(command, check=False).returncode)
    stop = False

    def request_stop(*_: object) -> None:
        nonlocal stop
        stop = True

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, request_stop)
        except (ValueError, OSError):
            pass

    completed = 0
    started_at = datetime.now(timezone.utc)
    account_state = account_fetcher()
    heartbeat: dict[str, Any] = {
        "pid": os.getpid(),
        "started_at": started_at.isoformat(),
        "mode": "dry_run" if config.dry_run else "execute",
        "account_id": config.expected_account_id or account_state.account_id,
        "last_cycle": None,
        "next_cycle": None,
        "reconciliation_status": "not_yet_run",
    }
    write_heartbeat(heartbeat, config.heartbeat_file)
    while not stop and (config.max_cycles is None or completed < config.max_cycles):
        clock = clock_fetcher()
        if not clock.ok or not clock.is_open:
            delay = config.closed_market_poll_seconds if clock.ok else config.failure_backoff_seconds
            heartbeat["next_cycle"] = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            heartbeat["reconciliation_status"] = "waiting_market" if clock.ok else "clock_unavailable"
            write_heartbeat(heartbeat, config.heartbeat_file)
            sleep(delay)
            continue
        heartbeat["next_cycle"] = None
        heartbeat["reconciliation_status"] = "cycle_starting"
        write_heartbeat(heartbeat, config.heartbeat_file)
        code = runner(cycle_command(config))
        completed += 1
        state = load_cycle_state() or {}
        result = state.get("result") if isinstance(state.get("result"), dict) else {}
        heartbeat["last_cycle"] = {
            "cycle_id": state.get("cycle_id"),
            "status": state.get("status") or ("failed" if code else "completed"),
            "completed_at": state.get("updated_at"),
            "exit_code": code,
            "failure_reason": result.get("reason") if code else None,
        }
        heartbeat["status"] = "failed" if code else "healthy"
        heartbeat["reconciliation_status"] = (
            result.get("reconciliation_status")
            or ("failed" if code else "verified")
        )
        if stop or (config.max_cycles is not None and completed >= config.max_cycles):
            heartbeat["next_cycle"] = None
            write_heartbeat(heartbeat, config.heartbeat_file)
            break
        delay = config.interval_seconds if code == 0 else config.failure_backoff_seconds
        heartbeat["next_cycle"] = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        write_heartbeat(heartbeat, config.heartbeat_file)
        sleep(delay)
    heartbeat["next_cycle"] = None
    heartbeat["stopped_at"] = datetime.now(timezone.utc).isoformat()
    if heartbeat["reconciliation_status"] in {
        "not_yet_run", "waiting_market", "clock_unavailable", "cycle_starting"
    }:
        heartbeat["reconciliation_status"] = "stopped"
    write_heartbeat(heartbeat, config.heartbeat_file)
    return completed


def main() -> None:
    parser = argparse.ArgumentParser(description="Run bounded autonomous paper cycles")
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--closed-market-poll-seconds", type=float, default=300.0)
    parser.add_argument("--failure-backoff-seconds", type=float, default=30.0)
    parser.add_argument("--max-cycles", type=int)
    parser.add_argument("--budget", type=float)
    parser.add_argument("--expected-account-id", default=os.getenv("ALPACA_EXPECTED_ACCOUNT_ID"))
    parser.add_argument("--execute", action="store_true", help="enable paper submissions; default is dry-run")
    parser.add_argument("--include-options", action="store_true")
    args = parser.parse_args()
    config = LoopConfig(
        interval_seconds=max(1.0, args.interval_seconds),
        closed_market_poll_seconds=max(1.0, args.closed_market_poll_seconds),
        failure_backoff_seconds=max(1.0, args.failure_backoff_seconds),
        max_cycles=args.max_cycles,
        dry_run=not args.execute,
        budget=args.budget,
        expected_account_id=args.expected_account_id,
        include_options=args.include_options,
    )
    run_loop(config)


if __name__ == "__main__":
    main()
