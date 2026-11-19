"""The iteration-budget exit must book against the DISPATCHED breaker budget.

Root cause (t_30a579cb): ``agent/turn_finalizer._record_kanban_budget_exhausted``
called ``_record_task_failure(outcome="timed_out")`` with NO ``failure_limit``,
so the breaker substituted ``DEFAULT_FAILURE_LIMIT = 2`` and parked a card on
its second budget exhaustion even when the dispatching board was configured
``kanban.failure_limit: 4``. Measured on the live DB: 38 ``timed_out``
``gave_up`` rows, all ``failures: 2`` / ``effective_limit: 2``, across 37 cards.

The budget cannot be re-derived from config inside the worker — the dispatcher
resolved it from ITS home (``pm-bangioi``: 4) while the worker's own home
(``developer-bangioi``: no key) resolves the built-in 2. So the dispatcher
stamps the resolved budget on the ``claimed`` event and the worker reads it
back (``hermes_cli.kanban_db_claim_budget``).

Every test here fails on the pre-fix code and passes after it; the
``trips_at_two`` / bare-claim assertions are the negative controls that keep
the change from simply widening the breaker everywhere.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from agent import turn_finalizer as tf
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_claim_budget as kbcb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

LOG = logging.getLogger("test.iteration_budget")
WORKER_PID = 424_242


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated HERMES_HOME with an empty kanban DB and clean process registries."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    kbd._recent_worker_exits.clear()
    saved_procs = dict(kbd._live_worker_procs)
    kbd._live_worker_procs.clear()
    kb.init_db()
    try:
        yield home
    finally:
        kbd._live_worker_procs.clear()
        kbd._live_worker_procs.update(saved_procs)


@pytest.fixture
def conn(kanban_home):
    db = kbc.connect()
    try:
        yield db
    finally:
        db.close()


def _spawn(*_args, **_kwargs):
    return WORKER_PID


def _event_payload(conn, task_id: str, kind: str):
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
        (task_id, kind),
    ).fetchone()
    return json.loads(row["payload"]) if row is not None else None


def _ready_card(conn, title: str = "budget card") -> str:
    """A spawnable card in ``ready`` (``create_task`` lands there with no parents)."""
    return kb.create_task(conn, title=title, assignee="alice")


def _dispatch(conn, failure_limit: int):
    """One dispatcher tick that spawns the seeded ready card under ``failure_limit``."""
    return kbd.dispatch_once(
        conn, spawn_fn=_spawn, failure_limit=failure_limit, max_spawn=4, max_in_progress=4,
    )


def _seed_failures(conn, task_id: str, failures: int) -> None:
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET consecutive_failures = ? WHERE id = ?", (failures, task_id),
        )


def _exhaust_budget(task_id: str, *, used: int = 500, max_iterations: int = 1000) -> None:
    """The exact call ``agent/turn_finalizer`` makes when the loop budget runs out."""
    tf._record_kanban_budget_exhausted(task_id, used, max_iterations, LOG)


# --------------------------------------------------------------------------- #
# The defect: 38 premature blocks through this path
# --------------------------------------------------------------------------- #

def test_dispatched_budget_keeps_the_card_alive_until_it_is_spent(conn, all_assignees_spawnable):
    """2 prior attempts + this exhaustion = 3 < 4 → retry, not park.

    Pre-fix this parked the card (3 >= DEFAULT_FAILURE_LIMIT 2): the exact
    ``failures: 2`` / ``effective_limit: 2`` shape found 38 times on the live DB.
    """
    task_id = _ready_card(conn)
    _dispatch(conn, 4)
    _seed_failures(conn, task_id, 2)

    _exhaust_budget(task_id)

    task = kb.get_task(conn, task_id)
    assert task.status == "ready", "a board configured for 4 must not park at 3"
    assert task.consecutive_failures == 3
    assert _event_payload(conn, task_id, "gave_up") is None


def test_dispatched_budget_parks_the_card_on_the_fourth(conn, all_assignees_spawnable):
    """3 prior attempts + this one = 4 → park, and the payload says which limit decided."""
    task_id = _ready_card(conn)
    _dispatch(conn, 4)
    _seed_failures(conn, task_id, 3)

    _exhaust_budget(task_id, used=612, max_iterations=700)

    task = kb.get_task(conn, task_id)
    assert task.status == "blocked"
    payload = _event_payload(conn, task_id, "gave_up")
    assert payload is not None
    assert payload["failures"] == 4
    assert payload["effective_limit"] == 4
    assert payload["limit_source"] == "dispatcher"
    assert payload["trigger_outcome"] == "timed_out"
    # The budget telemetry the finalizer attaches must survive the trip payload.
    assert payload["budget_used"] == 612
    assert payload["budget_max"] == 700


def test_the_dispatched_budget_still_trips_at_two(conn, all_assignees_spawnable):
    """Negative control: a board configured for 2 still parks on the second exhaustion.

    Guards against "fixing" it by hard-coding 4 or by never tripping at all.
    """
    task_id = _ready_card(conn)
    _dispatch(conn, 2)
    _seed_failures(conn, task_id, 1)

    _exhaust_budget(task_id)

    task = kb.get_task(conn, task_id)
    assert task.status == "blocked"
    payload = _event_payload(conn, task_id, "gave_up")
    assert payload["failures"] == 2
    assert payload["effective_limit"] == 2
    assert payload["limit_source"] == "dispatcher"


# --------------------------------------------------------------------------- #
# A claim with no stamp must not widen anything
# --------------------------------------------------------------------------- #

def test_an_unstamped_claim_falls_back_to_the_built_in_default(conn):
    """A claim taken outside the dispatcher carries no budget → built-in default.

    ``limit_source: "default"`` is PR #21's honest label; before it the payload
    falsely claimed ``dispatcher`` for a limit the dispatcher never passed in.
    """
    task_id = _ready_card(conn)
    assert kb.claim_task(conn, task_id, ttl_seconds=60) is not None
    _seed_failures(conn, task_id, 1)

    _exhaust_budget(task_id)

    task = kb.get_task(conn, task_id)
    assert task.status == "blocked"
    payload = _event_payload(conn, task_id, "gave_up")
    assert payload["effective_limit"] == kb.DEFAULT_FAILURE_LIMIT
    assert payload["limit_source"] == "default"


@pytest.mark.parametrize("stamp", ["", "banana", 0, -3, 2.5, True])
def test_a_junk_stamp_never_widens_the_breaker(conn, stamp):
    """Only a JSON integer >= 1 is a budget; everything else falls back, never up."""
    task_id = _ready_card(conn)
    claimed = kb.claim_task(conn, task_id, ttl_seconds=60, event_extra={"failure_limit": stamp})
    assert claimed is not None
    assert kbcb.dispatched_failure_limit(conn, task_id) is None
    _seed_failures(conn, task_id, 1)

    _exhaust_budget(task_id)

    payload = _event_payload(conn, task_id, "gave_up")
    assert payload["effective_limit"] == kb.DEFAULT_FAILURE_LIMIT
    assert payload["limit_source"] == "default"


# --------------------------------------------------------------------------- #
# The stamp itself: written by the dispatcher, only the newest one counts
# --------------------------------------------------------------------------- #

def test_the_dispatcher_stamps_the_budget_it_resolved(conn, all_assignees_spawnable):
    task_id = _ready_card(conn)
    result = _dispatch(conn, 4)

    assert [spawned[0] for spawned in result.spawned] == [task_id]
    assert kbcb.dispatched_failure_limit(conn, task_id) == 4


def test_a_direct_claim_stamps_nothing_and_keeps_the_event_shape(conn):
    """``claim_task`` without ``event_extra`` stamps nothing (control-plane lanes)."""
    task_id = _ready_card(conn)
    claimed = kb.claim_task(conn, task_id, ttl_seconds=60)

    assert claimed is not None
    assert kbcb.dispatched_failure_limit(conn, task_id) is None
    # The claim event every existing reader parses is otherwise unchanged.
    payload = _event_payload(conn, task_id, "claimed")
    assert "failure_limit" not in payload
    assert payload["run_id"] == claimed.current_run_id


def test_only_the_newest_claim_stamps_the_budget(conn, all_assignees_spawnable):
    """A re-claim opens a new run; the previous run's budget must not leak onto it."""
    task_id = _ready_card(conn)
    _dispatch(conn, 4)
    assert kbcb.dispatched_failure_limit(conn, task_id) == 4

    assert kb.reclaim_task(conn, task_id) is True
    assert kb.claim_task(conn, task_id, ttl_seconds=60) is not None
    assert kbcb.dispatched_failure_limit(conn, task_id) is None


def test_a_review_claim_carries_the_budget_and_keeps_its_source_status(conn):
    """Review runs keep ``source_status: "review"`` while also carrying the budget."""
    task_id = _ready_card(conn)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))

    claimed = kb.claim_review_task(
        conn, task_id, ttl_seconds=60, event_extra={"failure_limit": 4},
    )

    assert claimed is not None
    payload = _event_payload(conn, task_id, "claimed")
    assert payload["source_status"] == "review"
    assert payload["failure_limit"] == 4
    assert kbcb.dispatched_failure_limit(conn, task_id) == 4
    # ...and the retry status still routes the reviewer, not an implementer.
    assert kb._retry_status_for_run(conn, task_id) == "review"
