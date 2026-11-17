"""One gateway death must not spend every served card's failure budget.

2026-11-17 12:00: nine cards of five different profiles crashed within seven minutes, all with
``claimer: VI-WORKSPACE:110912`` (the pm-bangioi dispatcher), all carrying the same owner-side line
(``Kanban owner refused: Kanban owner silent for 190s: Gateway starting: deadline``). From the card
side a gateway death and a worker failure were indistinguishable, so each one was booked as an
ordinary crash against that card's ``failure_limit`` — one host fault, nine cards blocked.

The booking now separates the two: an owner-side death is recorded as ``owner_lost`` with a payload
saying which gateway answered what, the card is re-admitted WITHOUT spending its failure budget, and
the budget is bounded so a genuinely broken owner still surfaces a human instead of requeueing for
ever.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

NOW = int(time.time())

OWNER_SIDE_OUTPUT = (
    "Kanban owner refused: Kanban owner silent for 190s: Gateway starting: deadline "
    "Kanban owner refused: Kanban owner silent for 190s: InvalidStatus (HTTP 500)"
)


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    conn = kbc.connect()
    yield conn
    conn.close()


def _dead(*, owner_lost: bool = True, output: str = OWNER_SIDE_OUTPUT) -> kbd._DeadWorker:
    return kbd._DeadWorker(
        "owner_lost" if owner_lost else "nonzero_exit", 1,
        "pid 1 exited with code 1" + (f"{kbd.WORKER_OUTPUT_SEP}{output!r}" if output else ""),
        "owner_lost" if owner_lost else "crashed",
        {"pid": 1, "exit_code": 1, "worker_output": output},
        owner_lost=owner_lost,
    )


def _task(conn, tid: str = "t1", *, status: str = "running") -> None:
    conn.execute(
        "INSERT INTO tasks (id, title, assignee, status, created_at, priority) "
        "VALUES (?, ?, 'developer-bangioi', ?, ?, 5)", (tid, tid, status, NOW))
    conn.commit()


def _owner_lost_run(conn, tid: str, n: int = 1) -> None:
    for _ in range(n):
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, metadata) "
            "VALUES (?, 'developer-bangioi', 'done', ?, ?, 'owner_lost', ?)",
            (tid, NOW, NOW, json.dumps({"owner_lost": True})))
    conn.commit()


# --- the classification: the card's own words vs the host's -------------------------------------

def test_a_dead_serving_gateway_is_booked_owner_lost_with_its_own_outcome(monkeypatch):
    monkeypatch.setattr(kbd, "_classify_worker_exit", lambda _pid: ("nonzero_exit", 1))
    monkeypatch.setattr(kbd, "_worker_final_output", lambda *_a, **_k: OWNER_SIDE_OUTPUT)

    dead = kbd._classify_dead_worker(1, "VI-WORKSPACE:110912", 1, task_id="t1", board="default")

    assert dead.owner_lost is True
    assert dead.run_outcome == "owner_lost"
    assert dead.event_kind == "owner_lost"
    assert dead.event_payload["owner_lost"] is True
    assert dead.event_payload["owner_side_reason"] == "Kanban owner silent"
    assert "the gateway serving this profile died" in dead.error_text
    assert "not a worker fault" in dead.error_text
    # The worker's own words are kept: the diagnosis needs both halves.
    assert dead.event_payload["worker_output"] == OWNER_SIDE_OUTPUT


def test_a_worker_that_failed_on_its_own_stays_an_ordinary_crash(monkeypatch):
    monkeypatch.setattr(kbd, "_classify_worker_exit", lambda _pid: ("nonzero_exit", 1))
    monkeypatch.setattr(kbd, "_worker_final_output",
                        lambda *_a, **_k: "pytest failed: 3 tests in nobody's gateway")

    dead = kbd._classify_dead_worker(1, "VI-WORKSPACE:110912", 1, task_id="t1", board="default")

    assert dead.owner_lost is False
    assert dead.run_outcome == "crashed"
    assert dead.event_kind == "crashed"


@pytest.mark.parametrize("state, reason", [
    ("starting", "deadline"),
    ("draining", "update_paused"),
])
def test_a_worker_that_gave_up_waiting_for_its_owner_names_the_state(state, reason):
    output = f"Kanban owner silent for 190s: Gateway {state}: {reason}"
    assert kbd._owner_side_failure(output) == "Kanban owner silent", output


# --- the booking: re-admission without the failure budget, bounded ------------------------------

def test_an_owner_lost_run_is_readmitted_without_spending_the_failure_budget(board):
    _task(board)
    _owner_lost_run(board, "t1", 1)  # the run being accounted for

    blocked = kbd._account_crashes(board, [("t1", 1, "VI-WORKSPACE:110912", _dead())])

    assert blocked == []
    row = board.execute("SELECT consecutive_failures, status FROM tasks WHERE id = 't1'").fetchone()
    assert row["consecutive_failures"] == 0, "the card did not fail; the host did"
    assert row["status"] == "running"  # the sweep already re-admitted it; nothing blocked it


def test_a_permanently_broken_owner_still_surfaces_a_human_after_the_bound(board):
    _task(board)
    _owner_lost_run(board, "t1", kbd._OWNER_LOST_FAILURE_LIMIT)  # six consecutive host losses

    blocked = kbd._account_crashes(board, [("t1", 1, "VI-WORKSPACE:110912", _dead())])

    assert blocked == ["t1"]
    row = board.execute("SELECT status, consecutive_failures FROM tasks WHERE id = 't1'").fetchone()
    assert row["status"] == "blocked"
    kinds = [r["kind"] for r in board.execute(
        "SELECT kind FROM task_events WHERE task_id = 't1' ORDER BY id")]
    assert "gave_up" in kinds


def test_a_real_card_failure_breaks_the_owner_lost_streak(board):
    """A card that fails on its own spends its own budget; the two are not pooled."""
    _task(board)
    _owner_lost_run(board, "t1", 2)
    board.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome) "
        "VALUES ('t1', 'developer-bangioi', 'done', ?, ?, 'crashed')", (NOW, NOW))
    board.commit()

    assert kbd._owner_lost_streak(board, "t1") == 0
