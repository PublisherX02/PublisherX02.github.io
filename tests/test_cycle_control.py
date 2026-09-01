import json
import os
from datetime import datetime, timezone

import pytest

from cycle_control import (
    CycleAlreadyRunning,
    CycleLock,
    client_order_id,
    cycle_id_for_run,
    load_cycle_state,
    new_cycle_id,
    write_cycle_state,
)


def test_cycle_id_and_client_order_id_are_stable_and_bounded():
    cycle = new_cycle_id(datetime(2026, 9, 1, 12, 30, tzinfo=timezone.utc))
    first = client_order_id(cycle, "AAPL", "buy", 1)
    assert first == client_order_id(cycle, "AAPL", "buy", 1)
    assert first != client_order_id(cycle, "AAPL", "buy", 2)
    assert first != client_order_id(cycle, "AAPL", "sell", 1)
    assert len(first) <= 48


def test_lock_refuses_live_owner_and_releases_only_its_own_file(tmp_path):
    path = tmp_path / "cycle.lock"
    first = CycleLock("cycle-1", path)
    first.acquire()
    try:
        with pytest.raises(CycleAlreadyRunning):
            CycleLock("cycle-2", path).acquire()
        assert json.loads(path.read_text())["pid"] == os.getpid()
    finally:
        first.release()
    assert not path.exists()


def test_stale_lock_is_recovered(tmp_path):
    path = tmp_path / "cycle.lock"
    path.write_text(json.dumps({"pid": 999999999, "cycle_id": "dead"}))
    lock = CycleLock("new", path)
    lock.acquire()
    try:
        assert json.loads(path.read_text())["cycle_id"] == "new"
    finally:
        lock.release()


def test_cycle_state_write_is_loadable(tmp_path):
    state = tmp_path / "state.json"
    write_cycle_state("cycle-1", "completed", state_file=state, details={"orders": 2})
    loaded = load_cycle_state(state)
    assert loaded["cycle_id"] == "cycle-1"
    assert loaded["status"] == "completed"
    assert loaded["orders"] == 2


def test_explicit_recovery_reuses_interrupted_cycle_id():
    cycle_id, recovering = cycle_id_for_run(
        {"cycle_id": "interrupted-cycle", "status": "running"}, True
    )
    assert cycle_id == "interrupted-cycle"
    assert recovering is True


def test_normal_run_never_reuses_prior_cycle_id():
    cycle_id, recovering = cycle_id_for_run(
        {"cycle_id": "old-cycle", "status": "completed"}, False
    )
    assert cycle_id != "old-cycle"
    assert recovering is False
