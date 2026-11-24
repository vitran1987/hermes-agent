"""``worker_result`` / ``worker_exit_code`` must survive their own default arguments.

Regression carried by delta #36 (fork PR #36, commit ``a640ce4fe05``): ``worker_result`` gained
``busy_timeout`` for the reconciling re-read and passed it straight into
``sqlite3.connect(timeout=...)``. sqlite3 rejects ``None`` with
``TypeError: must be real number, not NoneType``, and BOTH managed-worker success paths call it
without the argument — ``kanban_worker_client._exit_code`` (a settled receipt) and
``worker_exit_code`` — so every worker that finished cleanly raised on its way out, exited 1, and
the dispatcher booked the run ``crashed`` and spent the card's failure budget.

Measured 2026-11-24 (deploy 20:38 ICT): 3 runs with that traceback, 0 before it.

The board here is a throwaway sqlite file with just the ``task_events`` table this function reads,
so the proof does not need a live board or a Hermes home.
"""
import json
import sqlite3

import pytest

from gateway import session_kanban

PARAMS = {"run_id": 7, "task_id": "t_worker_result_default", "claim_lock": "host:owner"}


def _board(tmp_path, exit_code=None, claim_lock=None, name="board"):
    path = tmp_path / f"{name}.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER,"
            " kind TEXT, payload TEXT)"
        )
        if exit_code is not None:
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload) VALUES (?, ?, 'worker_result', ?)",
                (
                    PARAMS["task_id"],
                    PARAMS["run_id"],
                    json.dumps(
                        {
                            "pid": 4242,
                            "claim_lock": claim_lock or PARAMS["claim_lock"],
                            "exit_code": exit_code,
                            "last_output": "done",
                        }
                    ),
                ),
            )
        conn.commit()
    return str(path)


def test_worker_result_without_busy_timeout_reads_the_attempt(tmp_path):
    """The default call (busy_timeout omitted) must return the settled attempt, not raise."""
    result = session_kanban.worker_result(_board(tmp_path, exit_code=0), PARAMS)

    assert result["exit_code"] == 0
    assert result["claim_lock"] == PARAMS["claim_lock"]


def test_worker_result_default_keeps_the_sqlite_five_second_wait(tmp_path, monkeypatch):
    """``None`` resolves to sqlite3's own 5 s default — not to ``timeout=None`` (a TypeError)."""
    seen = {}
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(session_kanban.sqlite3, "connect", spy)
    path = _board(tmp_path, exit_code=0)

    session_kanban.worker_result(path, PARAMS)
    assert seen["timeout"] == 5.0

    session_kanban.worker_result(path, PARAMS, busy_timeout=12.5)
    assert seen["timeout"] == 12.5


@pytest.mark.parametrize("busy_timeout", [None, 5.0])
def test_worker_exit_code_reads_and_forwards_the_timeout(tmp_path, busy_timeout):
    """``worker_exit_code`` is the second success-path caller; it forwards the wait explicitly."""
    path = _board(tmp_path, exit_code=0)

    assert session_kanban.worker_exit_code(path, PARAMS, busy_timeout=busy_timeout) == 0
    assert session_kanban.worker_exit_code(path, PARAMS) == 0


def test_worker_exit_code_still_fails_closed_without_an_attempt(tmp_path):
    """No attempt on the board stays a failure exit (1) — the fix must not widen the contract."""
    assert session_kanban.worker_exit_code(_board(tmp_path), PARAMS) == 1
    assert session_kanban.worker_result(_board(tmp_path, claim_lock="foreign"), PARAMS) == {}
