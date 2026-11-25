"""A host-caused worker death must never be buried as an untyped, never-retried park.

Regression for the 2026-11-25 incident: one authorised gateway restart killed 7 workers in a single
second. Each died with exit 1/130 (a worker killed with its gateway never reaches its exit epilogue),
so every one was booked a plain ``crashed``, charged to its card's own failure budget, and — seven
identical error fingerprints in one tick — tripped the systemic breaker at ``failure_limit=1``.
Seven cards parked ``blocked`` with ``block_kind IS NULL``: no reaper query, no cron and no operator
filter selects a NULL kind, so the cards were invisible while still filling the blocked column.

Two halves are pinned here:

  A1  a death whose surviving evidence says the HOST ended the attempt (the owner's own admission
      note, or the worker client's restart chrome) is booked ``owner_lost``: re-admitted to ``ready``
      WITHOUT spending the card's failure budget, so no ``kanban unblock`` is needed;
  A2  when that class does park (past the bounded owner-loss streak) it parks TYPED
      ``block_kind='transient'``, never NULL;
  B   a genuine content failure keeps its old behaviour: charged to the budget, and a park at the
      limit stays UNTYPED — an unhealable card failure must not be relabelled ``transient`` and
      retried forever.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import psutil
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_seed import seed_admission, seed_session

NOW = int(time.time())
HOST = kb._claimer_id().split(":", 1)[0]
FINGERPRINT = "6a232414-2dbf-40ab-a534-d9b71db05658:116|4162240"


def _absent_pid(base: int = 3_600_000) -> int:
    pid = base
    while psutil.pid_exists(pid):
        pid += 1
    return pid


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    conn = kbc.connect()
    ledger = tmp_path / "state.db"
    with open_persistence(profile_scope(home=tmp_path), eager=True) as store:
        seed_session(store, "sess-1", "cli")
        yield _Board(conn, ledger, store)
    conn.close()


class _Board:
    def __init__(self, conn, ledger: Path, store):
        self.conn, self.ledger, self.store = conn, ledger, store

    # --- seeding ----------------------------------------------------------------------
    def running(self, tid: str = "t_lost", *, admission=("terminal", "interrupted"),
                owner: bool = True) -> int:
        """A ``running`` card whose worker died; its owner receipt is in *admission*."""
        worker_pid = _absent_pid()
        lock = f"{HOST}:{_absent_pid(base=3_700_000)}"
        run_id = int(self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'fan', 'running', ?, ?, ?, ?)",
            (tid, lock, NOW + 900, worker_pid, NOW - 120),
        ).lastrowid)
        self.conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, created_at, claim_lock, claim_expires, "
            "worker_pid, worker_started_at, last_heartbeat_at, current_run_id, started_at, priority) "
            "VALUES (?, ?, 'fan', 'running', ?, ?, ?, ?, ?, ?, ?, ?, 5)",
            (tid, tid, NOW - 86400, lock, NOW + 900, worker_pid, FINGERPRINT, NOW - 30, run_id,
             NOW - 120),
        )
        self.event(tid, run_id, "claimed", {"lock": lock, "expires": NOW + 900}, NOW - 120)
        self.event(tid, run_id, "spawned", {"pid": worker_pid, "started_at": FINGERPRINT}, NOW - 120)
        if owner:
            request_id = f'kanban:["default","{tid}",{run_id}]'
            self.event(tid, run_id, "owner_admitted",
                       {"db": str(self.ledger), "session_id": "sess-1",
                        "pid": _absent_pid(base=3_800_000), "request_id": request_id}, NOW - 117)
            if admission:
                seed_admission(self.store, "sess-1", request_id, *admission)
        self.conn.commit()
        return run_id

    def worker_log(self, tid: str, text: str) -> None:
        log = kb.worker_log_path(tid)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(text, encoding="utf-8")

    def closed_owner_lost_runs(self, tid: str, count: int) -> None:
        """Trailing host-caused losses already on the card — the bounded streak A2 trips at."""
        for _ in range(count):
            self.conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, metadata, started_at, "
                "ended_at) VALUES (?, 'fan', 'owner_lost', 'owner_lost', ?, ?, ?)",
                (tid, json.dumps({"owner_lost": True}), NOW - 600, NOW - 540),
            )
        self.conn.commit()

    def event(self, tid: str, run: int, kind: str, payload: dict, ts: int) -> None:
        self.conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)", (tid, run, kind, json.dumps(payload), ts))

    # --- reading ----------------------------------------------------------------------
    def state(self, tid: str) -> dict:
        return dict(self.conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())

    def run(self, run_id: int) -> dict:
        return dict(self.conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone())

    def event_kinds(self, tid: str) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,))]

    def tick(self, *, failure_limit: int | None = None) -> kbd.DispatchResult:
        return kbd.dispatch_once(self.conn, max_spawn=0,
                                 failure_limit=kbd.DEFAULT_FAILURE_LIMIT if failure_limit is None else failure_limit,
                                 stale_timeout_seconds=0)


# --- A1: a host-caused death is re-admitted, not charged ---------------------------------

def test_an_interrupted_owner_admission_is_booked_host_side_and_the_card_returns_to_ready(board):
    """The owner's own ledger says it interrupted the attempt: the card never failed."""
    run_id = board.running("t_restart", admission=("terminal", "interrupted"))
    board.tick(failure_limit=1)  # the incident's threshold: one strike parked 7 cards

    row, run = board.state("t_restart"), board.run(run_id)
    assert row["status"] == "ready"                     # back in the pool, no unblock needed
    assert row["consecutive_failures"] == 0             # the host did not spend the card's budget
    assert (row["claim_lock"], row["worker_pid"], row["current_run_id"]) == (None, None, None)
    assert run["outcome"] == "owner_lost"
    assert "owner admission settled interrupted" in run["error"]
    assert "gave_up" not in board.event_kinds("t_restart")


def test_the_worker_clients_restart_chrome_is_booked_host_side(board):
    """A worker killed with its gateway prints the client's own restart line and exits 1."""
    run_id = board.running("t_chrome", owner=False)
    board.worker_log(
        "t_chrome",
        "Kanban owner restarting (Gateway starting: deadline); waiting up to 900s for the "
        "replacement to answer (the worker is not at fault)\n"
        "Kanban owner unavailable (host condition, not a card fault): Kanban owner unavailable "
        "for 900s: Gateway starting: deadline\n",
    )
    board.tick(failure_limit=1)

    assert board.run(run_id)["outcome"] == "owner_lost"
    row = board.state("t_chrome")
    assert row["status"] == "ready" and row["consecutive_failures"] == 0


# --- A2: past the bounded streak the park is TYPED, never NULL ---------------------------

def test_a_host_caused_park_past_the_streak_is_typed_transient_never_null(board):
    """Six trailing host-side losses mean the host is broken: park, but typed so it stays visible."""
    run_id = board.running("t_buried", admission=("terminal", "interrupted"))
    board.closed_owner_lost_runs("t_buried", kbd._OWNER_LOST_FAILURE_LIMIT - 1)
    board.tick(failure_limit=1)

    row, run = board.state("t_buried"), board.run(run_id)
    assert row["status"] == "blocked"
    assert row["block_kind"] == "transient"             # NOT NULL: the D10 burial
    assert row["block_recurrences"] == 1
    assert row["consecutive_failures"] >= 1
    assert run["outcome"] == "owner_lost"               # how the death itself was booked
    kinds = board.event_kinds("t_buried")
    assert "gave_up" in kinds                           # ... and the breaker's trip on top of it


def test_the_park_kind_is_derived_from_the_failure_not_invented_by_the_caller(board):
    """The classifier is a pure function of the evidence the board keeps."""
    assert kbd._park_kind_for_failure(
        "pid 1 exited with code 1 [owner admission settled interrupted]") == "transient"
    assert kbd._park_kind_for_failure(
        "pid 1 exited with code 1 [managed worker lost: managed_worker_failed]") == "transient"
    assert kbd._park_kind_for_failure(
        "pid 1 exited with code 1", {"worker_output": "Gateway draining"}) == "transient"
    assert kbd._park_kind_for_failure("pid 1 exited with code 1") is None


# --- B: a content failure keeps its budget and parks untyped -----------------------------

def test_a_content_failure_is_still_charged_and_parks_untyped(board):
    """Exit 1 with no host-side evidence: a real card failure. Not transient, not retried for free."""
    board.running("t_content", owner=False)
    board.worker_log("t_content", "Traceback (most recent call last):\n  ValueError: bad card body\n")
    # ``_account_crashes`` books a plain crash against DEFAULT_FAILURE_LIMIT (it resolves its own
    # threshold rather than the dispatcher's), so seed one failure short of it.
    board.conn.execute("UPDATE tasks SET consecutive_failures = ? WHERE id = 't_content'",
                       (kbd.DEFAULT_FAILURE_LIMIT - 1,))
    board.conn.commit()
    board.tick(failure_limit=1)

    row = board.state("t_content")
    assert row["status"] == "blocked"
    assert row["consecutive_failures"] == kbd.DEFAULT_FAILURE_LIMIT
    assert row["block_kind"] is None, "a content failure must not be relabelled transient"
    assert "gave_up" in board.event_kinds("t_content")


def test_a_systemic_wave_of_content_failures_parks_untyped(board):
    """Three identical content errors in one tick trip the breaker — still not a host condition."""
    for tid in ("t_w1", "t_w2", "t_w3"):
        board.running(tid, owner=False)
        board.worker_log(tid, "Traceback (most recent call last):\n  RuntimeError: provider said no\n")
    board.tick()

    for tid in ("t_w1", "t_w2", "t_w3"):
        row = board.state(tid)
        assert row["status"] == "blocked", tid
        assert row["block_kind"] is None, tid


# --- C: a quota text left behind by an OLD failure cannot hold a recovered card ----------

def _closed_run(board, tid: str, outcome: str, *, ended_at: int, metadata: dict | None = None) -> None:
    board.conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, outcome, metadata, started_at, ended_at) "
        "VALUES (?, 'fan', ?, ?, ?, ?, ?)",
        (tid, outcome, outcome, json.dumps(metadata or {}), ended_at - 600, ended_at),
    )
    board.conn.commit()


def test_a_quota_text_from_an_old_failure_does_not_park_a_recovered_card(board):
    """2026-11-25: 5 review cards stood still 112 min on a quota text from a run 3-4 h older.

    Their newest run was a SUCCESS (``review_requested``); ``last_failure_error`` is only written by
    a failure, so it still described an attempt the card had already recovered from. Reading it as a
    live blocker is the second silent-park failure mode of the same class.
    """
    board.conn.execute(
        "INSERT INTO tasks (id, title, assignee, status, created_at, priority, last_failure_error) "
        "VALUES ('t_recovered', 't_recovered', 'fan', 'review', ?, 5, ?)",
        (NOW - 86400, "pid 27672 exited rate-limited (quota wall) — requeued without counting a failure"),
    )
    board.conn.commit()
    # Newest run is the successful handoff...
    _closed_run(board, "t_recovered", "review_requested", ended_at=NOW - 300)
    # ... and the stale quota text came from an older, already-recovered attempt.
    _closed_run(board, "t_recovered", "rate_limited", ended_at=NOW - 4 * 3600)

    assert kbd.check_respawn_guard(board.conn, "t_recovered", lane="review") is None


def test_a_quota_text_whose_newest_run_failed_still_parks(board):
    """The guard's real job survives: a live quota/auth blocker still holds the card."""
    board.conn.execute(
        "INSERT INTO tasks (id, title, assignee, status, created_at, priority, last_failure_error) "
        "VALUES ('t_quota', 't_quota', 'fan', 'review', ?, 5, ?)",
        (NOW - 86400, "pid 27672 exited rate-limited (quota wall) — requeued without counting a failure"),
    )
    board.conn.commit()
    _closed_run(board, "t_quota", "gave_up", ended_at=NOW - 60)

    assert kbd.check_respawn_guard(board.conn, "t_quota", lane="review") == "blocker_auth"
