"""A restarted owner must not kill the cards it left running.

Measured defect (2026-11-17, board ``~/.hermes/kanban.db``): 7 cards died in the SAME second
(``ended_at=1794923660``) on 4 lanes, every run booked

    pid <N> exited with code 1 [managed worker lost: managed_worker_failed]
    Worker's last output: 'Kanban owner refused: Kanban execution unknown; retained by owner'

The owner (a profile gateway) lost its managed turn (``worker_receipt_timeout``), noted
``worker_lost`` on the board, the dispatcher's submitter reconnected, read
``receipt.status='unknown'`` and exited 1. Seven identical error fingerprints in one tick made
``_account_crashes`` treat the batch as ``is_systemic`` and trip at ``failure_limit=1``: every
card auto-blocked by ONE infrastructure event.

Two properties are pinned here:

  1. an owner-attributed loss (the owner itself recorded ``worker_lost``) is never a SYSTEMIC
     storm signal — one owner event must not auto-block a whole batch of cards;
  2. a claim is a lease, not a PID: when the process that issued the claim is gone but the
     worker it spawned is still alive, the reaper must HOLD the claim (``reclaim_deferred``)
     instead of killing a live interpreter; only a dead worker is reaped.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

NOW = int(time.time())
HOST = kb._claimer_id().split(":", 1)[0]
FINGERPRINT = "6a232414-2dbf-40ab-a534-d9b71db05658:116|4162240"


def _absent_pid(base: int = 3_700_000) -> int:
    pid = base
    while psutil.pid_exists(pid):
        pid += 1
    return pid


@pytest.fixture
def board(tmp_path, monkeypatch):
    """An isolated HERMES_HOME board plus the owner ledger a receipt points at."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    conn = kbc.connect()
    ledger = tmp_path / "state.db"
    with sqlite3.connect(ledger) as store:
        store.execute(
            "CREATE TABLE session_admissions "
            "(target_session_id TEXT, request_id TEXT, status TEXT, outcome TEXT)"
        )
    yield _Board(conn, ledger)
    conn.close()


@pytest.fixture
def live_worker():
    """A real live process standing in for a worker that outlived its owner."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(180)"])
    try:
        assert kbd._pid_alive(proc.pid)
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=30)


class _Board:
    def __init__(self, conn, ledger: Path):
        self.conn = conn
        self.ledger = ledger

    def running(
        self, tid: str, *, worker_pid, claim_issuer: int, record_worker_pid: bool = True,
        claim_expires_in: int = 900, heartbeat_age=None, owner: bool = False,
        admission=("terminal", "completed"), worker_lost=None, age_seconds: int = 1200,
    ) -> int:
        """A ``running`` card whose claim was issued by ``claim_issuer``."""
        lock = f"{HOST}:{int(claim_issuer)}"
        started = NOW - age_seconds
        # A live pid must carry its REAL spawn fingerprint: liveness is fingerprint-compared, and a
        # fake one reads as a recycled pid (i.e. the worker is gone).
        fingerprint = (kbd._process_fingerprint(worker_pid) if kbd._pid_alive(worker_pid)
                       else FINGERPRINT)
        run_id = int(self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'fan', 'running', ?, ?, ?, ?)",
            (tid, lock, NOW + claim_expires_in, worker_pid if record_worker_pid else None, started),
        ).lastrowid)
        self.conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, created_at, claim_lock, claim_expires, "
            "worker_pid, worker_started_at, last_heartbeat_at, current_run_id, started_at, priority) "
            "VALUES (?, ?, 'fan', 'running', ?, ?, ?, ?, ?, ?, ?, ?, 5)",
            (tid, tid, NOW - 86400, lock, NOW + claim_expires_in,
             worker_pid if record_worker_pid else None,
             fingerprint,
             (NOW - heartbeat_age) if heartbeat_age is not None else None, run_id, started),
        )
        self.event(tid, run_id, "claimed", {"lock": lock, "expires": NOW + claim_expires_in}, started)
        self.event(tid, run_id, "spawned",
                   {"pid": worker_pid, "started_at": fingerprint} if record_worker_pid else {}, started)
        if owner:
            request_id = f'kanban:["default","{tid}",{run_id}]'
            self.event(tid, run_id, "owner_admitted",
                       {"db": str(self.ledger), "session_id": "sess-1",
                        "pid": _absent_pid(base=3_950_000), "request_id": request_id}, started + 3)
            if admission:
                with sqlite3.connect(self.ledger) as store:
                    store.execute("INSERT INTO session_admissions VALUES ('sess-1', ?, ?, ?)",
                                  (request_id, admission[0], admission[1]))
        if worker_lost:
            self.event(tid, run_id, "worker_lost",
                       {"reason": worker_lost, "claim_lock": lock, "run_id": run_id,
                        "pid": worker_pid, "bound": True}, started + 24)
        self.conn.commit()
        return run_id

    def event(self, tid: str, run: int, kind: str, payload: dict, ts: int) -> None:
        self.conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)", (tid, run, kind, json.dumps(payload), ts))

    def state(self, tid: str) -> dict:
        return dict(self.conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())

    def run(self, run_id: int) -> dict:
        return dict(self.conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone())

    def kinds(self, tid: str) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,))]


# --- 1. claim is a lease, not a PID: a live worker outlives the process that claimed it ------

def test_a_live_worker_survives_the_death_of_its_claim_issuer(board, live_worker):
    """Owner restarted (its pid in ``claim_lock`` is gone) while the worker still runs.

    The reaper must HOLD the claim and must not signal anything: killing a live interpreter
    because its former owner stopped heartbeating is the defect this card fixes.
    """
    signalled: list[int] = []

    def recording_kill(pid, sig):
        signalled.append(int(pid))
        live_worker.kill()

    board.running("t_hold", worker_pid=live_worker.pid, claim_issuer=_absent_pid(base=3_910_000))
    reclaimed = kbd.detect_stale_running(board.conn, stale_timeout_seconds=900,
                                         signal_fn=recording_kill)
    row = board.state("t_hold")
    assert signalled == [], "the reaper signalled a worker whose owner had merely restarted"
    assert reclaimed == []
    assert row["status"] == "running" and row["worker_pid"] == live_worker.pid
    assert live_worker.poll() is None, "the live worker was killed by the owner's restart"
    assert "reclaim_deferred" in board.kinds("t_hold")


def test_a_dead_worker_under_a_dead_owner_is_still_reaped(board):
    """The inverse: owner gone AND worker gone must still be cleaned up (never adopt a corpse)."""
    signalled: list[int] = []
    run_id = board.running("t_reap", worker_pid=_absent_pid(base=3_920_000),
                           claim_issuer=_absent_pid(base=3_930_000))
    reclaimed = kbd.detect_stale_running(board.conn, stale_timeout_seconds=900,
                                         signal_fn=lambda pid, sig: signalled.append(int(pid)))
    row, run = board.state("t_reap"), board.run(run_id)
    assert reclaimed == ["t_reap"]
    assert row["status"] == "ready" and row["claim_lock"] is None
    assert run["outcome"] == "stale"


def test_a_live_claim_issuer_keeps_the_existing_reap(board, live_worker):
    """No behaviour change while the owner is alive: the stale path still reclaims."""
    run_id = board.running("t_owner_alive", worker_pid=live_worker.pid,
                           claim_issuer=os.getpid())
    reclaimed = kbd.detect_stale_running(
        board.conn, stale_timeout_seconds=900,
        signal_fn=lambda pid, sig: live_worker.kill())
    row, run = board.state("t_owner_alive"), board.run(run_id)
    assert reclaimed == ["t_owner_alive"]
    assert row["status"] == "ready" and row["claim_lock"] is None
    assert run["outcome"] == "stale"


# --- 2. one owner-side loss must not auto-block a batch of cards -----------------------------

def test_owner_attributed_losses_are_not_a_systemic_storm(board):
    """Seven identical ``worker_lost`` crashes in one tick are one infrastructure event.

    Each card still pays ONE failure toward its own ``failure_limit`` (bounded retries), but
    the identical-fingerprint rule must not trip the breaker at 1 for the whole batch.
    """
    for n in range(3):
        board.running(f"t_storm_{n}", worker_pid=_absent_pid(base=3_940_000 + n),
                      claim_issuer=_absent_pid(base=3_960_000), heartbeat_age=30,
                      owner=True, worker_lost="managed_worker_failed")
    auto_blocked = kbd.detect_crashed_workers(board.conn)
    rows = [board.state(f"t_storm_{n}") for n in range(3)]
    tripped = getattr(kbd.detect_crashed_workers, "_last_auto_blocked", [])
    assert tripped == [], f"one owner event auto-blocked {len(tripped)} cards"
    assert auto_blocked == [f"t_storm_{n}" for n in range(3)]  # booked as crashes, not skipped
    assert [r["status"] for r in rows] == ["ready"] * 3
    assert [r["consecutive_failures"] for r in rows] == [1] * 3
    assert all("gave_up" not in board.kinds(f"t_storm_{n}") for n in range(3))
