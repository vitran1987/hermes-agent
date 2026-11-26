"""An owner that stops answering must PARK the card, never book a crash (kanban t_56e39a6d).

2026-11-26, measured on the live board: one gateway stall between 10:29:42 and 10:45:27, then
6-7 workers died inside 5 seconds with ``WorkerPersistenceError: worker_receipt_timeout`` --
each after three in-place model retries that could not possibly help -- and the dispatcher booked
``spawned=7 crashed=7 auto_blocked=4``. Two cards were re-run on work that was already merged.

The acceptance properties pinned here:

* AC1 -- owner silent past the receipt budget: the worker exits 0, the card ends ``blocked`` with
  ``block_kind='transient'`` and a reason naming the owner, and NO run is booked ``crashed``;
* AC2 -- seven workers under one stalled owner: still zero crashes;
* AC4 -- the log line names the OWNER, not ``provider failure``;
* the park is fail-closed on run ownership, and its fallback is the board's ``owner_unavailable``
  sentinel (booked ``owner_lost``), never a plain exit 1.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.turn_exit import OWNER_UNAVAILABLE_REASON, turn_exit_code
from agent import kanban_turn_recovery as ktr
from agent.runtime_session_store import WorkerPersistenceError, WorkerReceiptTimeout

NOW = int(time.time())
LOCK = "VI-WORKSPACE:4242"


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # A dispatcher worker writes its own board; only a delegated descendant is fenced.
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    kb.init_db()
    conn = kbc.connect()
    yield conn
    conn.close()


def _running_task(conn, task_id: str, run_id: int) -> None:
    conn.execute(
        "INSERT INTO tasks (id, title, assignee, status, created_at, priority, "
        "current_run_id, claim_lock) VALUES (?, ?, 'senior-techlead-bangioi', 'running', ?, 5, ?, ?)",
        (task_id, task_id, NOW, run_id, LOCK),
    )
    conn.execute(
        "INSERT INTO task_runs (id, task_id, status, claim_lock, started_at) "
        "VALUES (?, ?, 'running', ?, ?)",
        (run_id, task_id, LOCK, NOW),
    )
    conn.commit()


def _owner_silent_result(detail: str = "worker_receipt_timeout") -> dict:
    return {"failed": True, "completed": False, "failure_reason": OWNER_UNAVAILABLE_REASON,
            "failure_retryable": False, "error": f"WorkerReceiptTimeout: {detail}"}


def _park(monkeypatch, task_id: str, run_id: int, emitted: list | None = None) -> bool:
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", LOCK)
    emit = (emitted.append if emitted is not None else None)
    return ktr.park_if_owner_unavailable(_owner_silent_result(), emit=emit)


# ── AC1: parked, typed, not crashed ──────────────────────────────────────────────────────────────

def test_owner_silence_parks_the_card_transient_and_exits_zero(board, monkeypatch):
    _running_task(board, "t1", 101)
    result = _owner_silent_result()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t1")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "101")
    assert ktr.park_if_owner_unavailable(result) is True
    assert result["owner_parked"] is True

    task = board.execute("SELECT * FROM tasks WHERE id = 't1'").fetchone()
    assert task["status"] == "blocked"
    assert task["block_kind"] == "transient"
    assert task["consecutive_failures"] == 0, "a host condition must not spend the card's budget"
    assert task["claim_lock"] is None and task["current_run_id"] is None
    assert task["last_failure_error"] is None, "nothing failed on the card, so no failure is stamped"
    # The reason a human needs lives on the blocked event and the closed run, not in a failure column.
    blocked_event = board.execute(
        "SELECT payload FROM task_events WHERE task_id = 't1' AND kind = 'blocked' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert blocked_event is not None and "owner" in blocked_event["payload"].lower()

    run = board.execute("SELECT * FROM task_runs WHERE id = 101").fetchone()
    assert run["outcome"] == "blocked" and run["outcome"] != "crashed"
    assert "owner" in (run["summary"] or "").lower()

    # (a) the worker exits 0 and the dispatcher has nothing to book as a crash
    assert turn_exit_code(result, kanban_worker=True) == 0
    assert kbd._exit_code_kind(0) == ("clean_exit", 0)


def test_the_park_leaves_a_comment_naming_the_owner(board, monkeypatch):
    _running_task(board, "t1", 101)
    assert _park(monkeypatch, "t1", 101) is True
    bodies = [row["body"] for row in board.execute(
        "SELECT body FROM task_comments WHERE task_id = 't1' ORDER BY id").fetchall()]
    assert bodies, "the card must explain itself to the human who finds it blocked"
    assert "Kanban owner silent" in bodies[-1]
    assert "not" in bodies[-1] and "failure_limit" in bodies[-1]


# ── AC2: the 26-11 shape -- seven workers, one stalled owner ────────────────────────────────────

def test_seven_workers_under_one_stalled_owner_book_zero_crashes(board, monkeypatch):
    for index in range(7):
        _running_task(board, f"t{index}", 200 + index)
    parked = [_park(monkeypatch, f"t{index}", 200 + index) for index in range(7)]

    assert parked == [True] * 7
    statuses = board.execute(
        "SELECT id, status, block_kind, consecutive_failures FROM tasks ORDER BY id").fetchall()
    assert [(row["status"], row["block_kind"]) for row in statuses] == [("blocked", "transient")] * 7
    assert [row["consecutive_failures"] for row in statuses] == [0] * 7
    assert board.execute(
        "SELECT COUNT(*) AS n FROM task_runs WHERE outcome = 'crashed'").fetchone()["n"] == 0


# ── AC4: the log names the owner ────────────────────────────────────────────────────────────────

def test_the_log_line_names_the_owner_and_never_says_provider_failure(board, monkeypatch):
    _running_task(board, "t1", 101)
    emitted: list[str] = []
    assert _park(monkeypatch, "t1", 101, emitted) is True

    assert emitted, "the worker log must carry the owner-silent clause"
    line = emitted[0]
    # Before this fix the same failure printed:
    #   [kanban] provider failure on t1 — retrying the turn in place (attempt 1/3) after 15s
    assert "provider failure" not in line.lower()
    assert "Kanban owner silent" in line


# ── fail-closed fallbacks ───────────────────────────────────────────────────────────────────────

def test_without_run_ownership_proof_the_park_refuses_and_the_sentinel_books_owner_lost(
        board, monkeypatch):
    """No pinned run id => no board write; the exit policy still keeps the card off the crash path."""
    _running_task(board, "t1", 101)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t1")
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    result = _owner_silent_result()

    assert ktr.park_if_owner_unavailable(result) is False
    assert "owner_parked" not in result
    assert board.execute("SELECT status FROM tasks WHERE id = 't1'").fetchone()["status"] == "running"

    code = turn_exit_code(result, kanban_worker=True)
    assert code == kb.KANBAN_TRANSIENT_OWNER_EXIT_CODE
    assert kbd._exit_code_kind(code) == ("owner_unavailable", code)
    dead = kbd._classify_dead_worker(1, LOCK, code, task_id="t1", board="default")
    assert dead.owner_lost is True and dead.run_outcome == "owner_lost"


def test_a_provider_timeout_result_is_not_parked_as_owner_silence(board, monkeypatch):
    """False positives are as harmful: a real provider failure must keep its own recovery path."""
    _running_task(board, "t1", 101)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t1")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "101")
    provider = {"failed": True, "failure_reason": "timeout", "failure_retryable": True,
                "error": "Request timed out"}
    assert ktr.park_if_owner_unavailable(provider) is False
    assert board.execute("SELECT status FROM tasks WHERE id = 't1'").fetchone()["status"] == "running"
    assert turn_exit_code(provider, kanban_worker=True) == kb.KANBAN_RATE_LIMIT_EXIT_CODE


# ── the arm absorbs the RECEIPT timeout only, never the rest of the store family ────────────────
#
# Measured on the previous head: catching the whole ``WorkerPersistenceError`` family made every one
# of these render "The persistence owner stopped acknowledging this run's receipts (<code>)", carry
# ``failure_reason=store_unavailable`` and park the card blocked/transient/exit 0. That is the same
# false-cause class this card exists to remove -- ``outbox_full`` is not a silent owner.

NON_RECEIPT_STORE_CODES = (
    "outbox_full",
    "permission_denied",
    "unsupported_operation",
    "synchronous_self_rpc",
    "pending_receipt",
)


@pytest.mark.parametrize("code", NON_RECEIPT_STORE_CODES)
def test_a_non_receipt_store_failure_is_never_owner_silence(board, monkeypatch, code):
    from hermes_cli.cli_single_query import _owner_silent_or_reraise, _owner_silent_result

    exc = WorkerPersistenceError(code)
    assert _owner_silent_result(exc) is None, (
        f"{code} is a real store failure: reporting it as a silent owner parks the card on a false "
        "cause and hides the actual failure from whoever picks it up")

    # The one line BOTH single-query arms call must hand the identical exception back to the caller,
    # not a typed owner-silence result.
    with pytest.raises(WorkerPersistenceError) as raised:
        _owner_silent_or_reraise(exc)
    assert raised.value is exc
    assert raised.value.args == (code,)

    # ...and nothing downstream can mistake its own route for the owner-silence one.
    assert ktr.park_if_owner_unavailable({"failed": True, "error": str(exc)}) is False
    assert ktr.owner_unavailable_failure({"failed": True, "failure_reason": "unknown",
                                          "error": str(exc)}) is False


def test_a_receipt_timeout_is_still_typed_owner_silence(board, monkeypatch):
    from hermes_cli.cli_single_query import _owner_silent_or_reraise, _owner_silent_result

    receipt = WorkerReceiptTimeout("worker_receipt_timeout")
    result = _owner_silent_result(receipt)
    assert result is not None
    assert result["failure_reason"] == OWNER_UNAVAILABLE_REASON
    assert result["failure_retryable"] is False
    assert result["failed"] is True
    # The cause text names the type that actually arrived -- it is not a hard-coded label.
    assert result["error"] == "WorkerReceiptTimeout: worker_receipt_timeout"
    assert ktr.owner_unavailable_failure(result) is True
    # The arm helper returns the same dict instead of re-raising.
    assert _owner_silent_or_reraise(receipt) == result
