"""A gateway that is not reachable is a HOST condition: the card must not pay for it.

2026-11-22 measured 24 worker exits in one hour, every one of them booked as an ordinary card
crash and charged to that card's ``failure_limit``: the worker client had waited out its
180 s owner-silence budget while the gateway was still booting (a cold start measured at ~4 min),
or was refused by the instance that lost a spawn race, and then exited 1 like any other failure.
The board ended the hour at 2 running / 49 blocked while no card had actually failed.

Two halves are pinned here:

* ``kanban_worker_client`` classifies "the owner is not reachable yet" (``inaccessible`` /
  ``runtime_exited`` / ``profile_parked`` / ``deadline``, and the ``starting``/``draining``
  restart states) as a transient refusal, gives it the revival budget instead of the silence
  budget, and exits with ``KANBAN_TRANSIENT_OWNER_EXIT_CODE`` instead of 1.
* ``kanban_db_dispatch`` decodes that exit code as ``owner_unavailable`` and books the RUN
  ``owner_lost`` — re-admitted without spending the failure budget, bounded by
  ``_OWNER_LOST_FAILURE_LIMIT`` so a permanently broken owner still surfaces a human.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_client as kwc

NOW = int(time.time())


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


# --- worker client: which refusals mean "not yet"? -----------------------------------------------

def _unavailable(state: str, reason: str):
    from hermes_cli.gateway_client import GatewayUnavailableError
    return GatewayUnavailableError(f"Gateway {state}: {reason}", state, reason)


@pytest.mark.parametrize("state, reason", [
    ("starting", "starting"),
    ("draining", "runtime_draining"),
    ("starting", "deadline"),
    ("absent", "runtime_exited"),
])
def test_a_refusal_that_means_not_yet_is_transient(state, reason):
    exc = _unavailable(state, reason)
    assert kwc._is_transient_refusal(exc) is True
    assert kwc._restartable(exc) is True
    # The revival budget, not the 180 s silence budget: a cold boot is ~4 minutes.
    assert kwc._owner_budget(exc) == kwc.OWNER_REVIVAL_WAIT_S


@pytest.mark.parametrize("state, reason", [
    ("inaccessible", "windows_breakaway_unavailable"),
    ("inaccessible", "unsafe_control_permissions"),
    ("absent", "profile_parked"),
])
def test_a_configuration_refusal_is_not_transient(state, reason):
    """Retrying these for 900 s would bury the real fix (elevation guard / permissions / park)."""
    exc = _unavailable(state, reason)
    assert kwc._is_transient_refusal(exc) is False
    assert kwc._owner_budget(exc) == kwc.OWNER_WAIT_S


def test_an_ordinary_transport_loss_keeps_the_silence_budget():
    from hermes_cli.gateway_client import GatewayClientError
    exc = GatewayClientError("Gateway connection failed; no local fallback")
    assert kwc._is_transient_refusal(exc) is False
    assert kwc._owner_budget(exc) == kwc.OWNER_WAIT_S


def test_giving_up_on_the_owner_exits_with_the_transient_sentinel(monkeypatch, capsys):
    """End of the revival budget -> sentinel exit, NOT exit 1 (which books a card crash)."""
    async def _never(_params, _board_db):
        raise kwc.OwnerUnavailable("Kanban owner unavailable for 900s: Gateway starting: deadline")

    monkeypatch.setattr(kwc, "run", _never)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t1")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "1")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "VI-WORKSPACE:1")
    monkeypatch.setenv("HERMES_KANBAN_DB", ":memory:")

    assert kwc._submit() == kb.KANBAN_TRANSIENT_OWNER_EXIT_CODE
    assert "not a card fault" in capsys.readouterr().err


# --- dispatcher: the sentinel is booked as a host-side loss, never a crash ------------------------

def test_the_sentinel_exit_code_decodes_to_owner_unavailable():
    assert kbd._exit_code_kind(kb.KANBAN_TRANSIENT_OWNER_EXIT_CODE) == (
        "owner_unavailable", kb.KANBAN_TRANSIENT_OWNER_EXIT_CODE)


def test_a_sentinel_exit_is_booked_owner_lost_without_reading_the_worker_log(monkeypatch):
    """The exit code is authoritative: no log-text guess is needed (and none is available)."""
    monkeypatch.setattr(kbd, "_worker_final_output", lambda *_a, **_k: "")

    dead = kbd._classify_dead_worker(
        1, "VI-WORKSPACE:1", kb.KANBAN_TRANSIENT_OWNER_EXIT_CODE, task_id="t1", board="default")

    assert dead.owner_lost is True
    assert dead.kind == "owner_unavailable"
    assert dead.run_outcome == "owner_lost"
    assert dead.event_kind == "owner_lost"
    assert dead.event_payload["owner_side_reason"] == "transient_owner_unavailable"
    assert "not a card fault" in dead.error_text


def test_a_sentinel_exit_does_not_spend_the_failure_budget(board):
    board.execute(
        "INSERT INTO tasks (id, title, assignee, status, created_at, priority) "
        "VALUES ('t1', 't1', 'developer-bangioi', 'ready', ?, 5)", (NOW,))
    board.commit()

    blocked = kbd._account_crashes(board, [("t1", 1, "VI-WORKSPACE:1", kbd._classify_dead_worker(
        1, "VI-WORKSPACE:1", kb.KANBAN_TRANSIENT_OWNER_EXIT_CODE, task_id="t1", board="default"))])

    assert blocked == []
    row = board.execute("SELECT consecutive_failures FROM tasks WHERE id = 't1'").fetchone()
    assert row["consecutive_failures"] == 0


# --- "execution unknown": ask the board before calling it a crash --------------------------------

PARAMS = {"board": "default", "task_id": "t1", "run_id": 7, "claim_lock": "VI-WORKSPACE:1"}


@pytest.fixture
def settlement_db(tmp_path):
    """A minimal board: only the two tables the reconciliation reads."""
    import json
    import sqlite3

    path = tmp_path / "board.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, ended_at INTEGER, outcome TEXT)")
    conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, run_id INTEGER, task_id TEXT, kind TEXT, payload TEXT)")
    yield path, conn
    conn.close()


def test_an_unknown_receipt_is_reconciled_from_the_boards_own_result(settlement_db, capsys):
    """2026-11-23 13:03: three workers finished in 16 s and were booked ``crashed``."""
    import json

    path, conn = settlement_db
    conn.execute(
        "INSERT INTO task_events (run_id, task_id, kind, payload) VALUES (?, 't1', 'worker_result', ?)",
        (7, json.dumps({"claim_lock": "VI-WORKSPACE:1", "exit_code": 0, "last_output": "done"})),
    )
    conn.commit()

    code = kwc._exit_code({"status": "unknown"}, dict(PARAMS), str(path))

    assert code == 0, "the work settled; it must not be re-booked as a crash"
    err = capsys.readouterr().err
    assert "RECONCILED" in err
    assert "not a crash" in err


def test_an_unknown_receipt_whose_run_already_ended_exits_zero(settlement_db, capsys):
    path, conn = settlement_db
    conn.execute("INSERT INTO task_runs (id, task_id, ended_at, outcome) VALUES (7, 't1', ?, 'completed')",
                 (NOW,))
    conn.commit()

    assert kwc._exit_code({"status": "unknown"}, dict(PARAMS), str(path)) == 0
    assert "already closed by the board" in capsys.readouterr().err


def test_an_unknown_receipt_retries_the_board_read_before_giving_up(monkeypatch, capsys):
    """The retry is what absorbs the contention that produced ``unknown``."""
    calls = []

    def _flaky(_board_db, _params, _timeout):
        calls.append(_timeout)
        if len(calls) < 2:
            return None
        return ("result", {"exit_code": 0, "claim_lock": "VI-WORKSPACE:1"})

    monkeypatch.setattr(kwc, "_read_settlement", _flaky)

    assert kwc._exit_code({"status": "unknown"}, dict(PARAMS), "unused.db") == 0
    assert calls == [5.0, 15.0], "escalating busy timeouts, not one flat wait"
    assert "after 2 read(s)" in capsys.readouterr().err


def test_an_unknown_receipt_on_a_broken_board_still_fails_closed(tmp_path):
    """A board nobody can read must not be swallowed into a success."""
    from hermes_cli.gateway_client import GatewayClientError

    with pytest.raises(GatewayClientError):
        kwc._exit_code({"status": "unknown"}, dict(PARAMS), str(tmp_path / "missing.db"))


def test_an_unknown_receipt_with_no_settlement_still_fails_closed(settlement_db):
    """The other fail-closed arm: the board IS readable, this attempt simply never settled.

    Reconciliation must not invent an outcome here -- neither a success nor a crash booking made up
    from the submitter's own exit status.
    """
    from hermes_cli.gateway_client import GatewayClientError

    path, _conn = settlement_db
    with pytest.raises(GatewayClientError):
        kwc._exit_code({"status": "unknown"}, dict(PARAMS), str(path))


# --- a reconciled FAILURE keeps its label ------------------------------------------------------
#
# Measured 2026-11-26 on the live board (run 15638 of t_56e39a6d): the managed worker's own turn
# failed, its ``worker_result`` carried ``exit_code=1``, and the dispatcher booked ``crashed`` from
# THAT record. The reconcile line beside it said "not a crash", so a crash-rate reader counted it as
# a misbooking. The marker could not have been right: the board is the authority and it recorded a
# failure, so the line has to say so.


def test_a_reconciled_failure_is_not_reported_as_a_non_crash(settlement_db, capsys):
    import json

    path, conn = settlement_db
    conn.execute(
        "INSERT INTO task_events (run_id, task_id, kind, payload) VALUES (?, 't1', 'worker_result', ?)",
        (7, json.dumps({"claim_lock": "VI-WORKSPACE:1", "exit_code": 1, "last_output": ""})),
    )
    conn.commit()

    code = kwc._exit_code({"status": "unknown"}, dict(PARAMS), str(path))

    assert code == 1, "the board recorded a failure: replaying it is the whole point of the read"
    err = capsys.readouterr().err
    assert "RECONCILED" in err
    assert "not a crash" not in err, (
        "the board recorded this attempt's own failure -- calling it 'not a crash' is the label "
        "that inflated the crash-rate reading in the first place")
    assert "exit code 1" in err and "failure" in err


def test_a_settled_record_without_an_exit_code_is_not_a_success(settlement_db):
    """One absence, one meaning: the settled-receipt path already answered ``1`` here."""
    import json

    path, conn = settlement_db
    conn.execute(
        "INSERT INTO task_events (run_id, task_id, kind, payload) VALUES (?, 't1', 'worker_result', ?)",
        (7, json.dumps({"claim_lock": "VI-WORKSPACE:1", "last_output": "done"})),
    )
    conn.commit()

    assert kwc._exit_code({"status": "unknown"}, dict(PARAMS), str(path)) == 1


# --- the booking is the board's, so "reconcile then exit 0" could not have changed it -----------


def test_the_dispatcher_books_a_reconciled_failure_from_the_board_not_the_submitters_exit(tmp_path):
    """Run 15638's exact shape, replayed through the dispatcher's own helpers.

    ``tasks.worker_pid`` is the spawned submitter (34612); the attempt's authority record is the
    ``worker_result`` the owner-bound interpreter wrote (pid 4436, same claim). The dispatcher reads
    THAT code before its reap registry or the worker-log trailer, so no value the submitter exits
    with can turn this booking into anything else. This is the invariant the card's proposed
    "reconcile -> exit 0" would have left untouched, and it is pinned here so the next reader
    measures the booking instead of re-deriving it from the log line.
    """
    import json
    import sqlite3

    from hermes_cli.kanban_owner_recovery import managed_run_exit_code

    board = sqlite3.connect(tmp_path / "board.db")
    board.row_factory = sqlite3.Row
    board.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, current_run_id INTEGER)")
    board.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, run_id INTEGER, task_id TEXT, "
                  "kind TEXT, payload TEXT)")
    board.execute("INSERT INTO tasks VALUES ('t1', 7)")
    for kind, payload in (
        ("worker_bound", {"pid": 4436, "claim_lock": "VI-WORKSPACE:1"}),
        ("worker_result", {"pid": 4436, "claim_lock": "VI-WORKSPACE:1", "exit_code": 1,
                           "last_output": ""}),
    ):
        board.execute("INSERT INTO task_events (run_id, task_id, kind, payload) VALUES (7, 't1', ?, ?)",
                      (kind, json.dumps(payload)))
    board.commit()

    exit_code = managed_run_exit_code(board, "t1", 34612, "VI-WORKSPACE:1")
    assert exit_code == 1, "the board's own verdict for this attempt"

    dead = kbd._classify_dead_worker_exit(34612, "VI-WORKSPACE:1", exit_code, task_id="t1",
                                          board="default")
    assert (dead.kind, dead.run_outcome) == ("nonzero_exit", "crashed")

