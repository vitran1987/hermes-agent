"""The crash/timeout reclaim paths must honour ``kanban.failure_limit``.

Root cause (t_a8484158): ``_run_reclaim_phase`` had the configured
``failure_limit`` and passed it to ``release_stale_claims`` and
``recompute_ready`` — but NOT to ``detect_crashed_workers`` /
``enforce_max_runtime``, and ``_account_crashes`` forwarded ``None`` into
``_record_task_failure``. That call substituted the hardcoded
``DEFAULT_FAILURE_LIMIT = 2`` while still stamping
``limit_source: "dispatcher"``, so a board configured for 4 tripped the breaker
on ordinary crashes and timeouts at 2 (150 premature trips / 75 cards measured
between 2026-11-16 and 2026-11-18).

Every test here is written so that it FAILS on the pre-fix code and passes
after the fix (the ``_trips_at_two`` / missed-``gave_up`` assertions are the
negative control).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


# A PID that is provably not this process' worker; ``kb._pid_alive`` is patched
# to False for the whole module so no host process is ever consulted.
DEAD_PID = 812_345
OLD = int(time.time()) - 3_600


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated HERMES_HOME with an empty kanban DB and clean process registries."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # The launch-window grace exists to protect a freshly spawned worker; these
    # tests seed an already-dead one.
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    kbd._recent_worker_exits.clear()
    # Other test modules park fake ``Popen`` stand-ins in this module-global;
    # ``reap_worker_zombies`` polls them and a stand-in without ``poll`` breaks
    # every reclaim tick. Start from a clean registry.
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


def _event_payload(conn, tid: str, kind: str):
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
        (tid, kind),
    ).fetchone()
    return json.loads(row["payload"]) if row is not None else None


def _arm_crashed_worker(
    conn,
    title: str,
    *,
    failures: int | None = None,
    pid: int = DEAD_PID,
    max_runtime_seconds: int | None = None,
    task_id: str | None = None,
) -> str:
    """Put one card in ``running`` with a dead host-local worker attached.

    ``failures=None`` leaves the card's existing counter alone (used when
    re-arming the same card across ticks).
    """
    if task_id is None:
        task_id = kb.create_task(
            conn, title=title, assignee="builder", max_runtime_seconds=max_runtime_seconds,
        )
    host = kb._claimer_id().split(":", 1)[0]
    claimed = kb.claim_task(conn, task_id, claimer=f"{host}:t{pid}")
    assert claimed is not None, f"could not claim {task_id} for the seeded worker"
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid = ?, started_at = ? WHERE id = ?", (pid, OLD, task_id),
        )
        if failures is not None:
            conn.execute(
                "UPDATE tasks SET consecutive_failures = ? WHERE id = ?", (failures, task_id),
            )
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE task_id = ?", (OLD, task_id),
        )
    return task_id


def _reclaim(conn, failure_limit: int) -> kbd.DispatchResult:
    result = kbd.DispatchResult()
    kbd._run_reclaim_phase(
        conn,
        result,
        stale_timeout_seconds=0,
        failure_limit=failure_limit,
        reconcile_orphans=False,
    )
    return result


# ---------------------------------------------------------------------------
# Crash path (detect_crashed_workers <- _account_crashes)
# ---------------------------------------------------------------------------


def test_reclaim_phase_crash_honours_configured_limit(conn):
    """consecutive_failures=2 + limit 4 ⇒ next crash is NOT a trip (pre-fix: blocked at 2)."""
    tid = _arm_crashed_worker(conn, "crash under a configured budget of 4", failures=2)

    result = _reclaim(conn, failure_limit=4)

    task = kb.get_task(conn, tid)
    assert tid in result.crashed
    assert task.consecutive_failures == 3
    assert task.status == "ready", (
        f"breaker tripped at the built-in default 2 instead of the configured 4 "
        f"(status={task.status}, failures={task.consecutive_failures})"
    )
    assert _event_payload(conn, tid, "gave_up") is None


def test_reclaim_phase_crash_negative_control_trips_at_limit_two(conn):
    """Same shape at limit 2 must still trip — the breaker is not disabled."""
    tid = _arm_crashed_worker(conn, "crash at a configured budget of 2", failures=1)

    result = _reclaim(conn, failure_limit=2)

    task = kb.get_task(conn, tid)
    assert tid in result.crashed
    assert task.status == "blocked"
    payload = _event_payload(conn, tid, "gave_up")
    assert payload is not None
    assert payload["failures"] == 2
    assert payload["effective_limit"] == 2
    assert payload["limit_source"] == "dispatcher"
    assert payload["trigger_outcome"] == "crashed"


def test_crash_trip_reports_the_configured_limit_not_the_default(conn):
    """At limit 4 the trip must be stamped effective_limit=4 / dispatcher, not 2."""
    tid = _arm_crashed_worker(conn, "crash reaching a configured budget of 4", failures=3)

    _reclaim(conn, failure_limit=4)

    task = kb.get_task(conn, tid)
    assert task.status == "blocked"
    payload = _event_payload(conn, tid, "gave_up")
    assert payload["failures"] == 4
    assert payload["effective_limit"] == 4
    assert payload["limit_source"] == "dispatcher"


# ---------------------------------------------------------------------------
# Timeout path (enforce_max_runtime)
# ---------------------------------------------------------------------------


def test_enforce_max_runtime_honours_configured_limit(conn):
    """Elapsed max_runtime + 2 prior failures + limit 4 ⇒ timed_out, NOT blocked."""
    tid = _arm_crashed_worker(
        conn, "timeout under a configured budget of 4", failures=2, max_runtime_seconds=60,
    )

    timed_out = kbd.enforce_max_runtime(conn, signal_fn=lambda *_: None, failure_limit=4)

    task = kb.get_task(conn, tid)
    assert tid in timed_out, "the worker was not reaped even though max_runtime_seconds elapsed"
    assert task.consecutive_failures == 3
    assert task.status == "ready", (
        f"timeout tripped at the built-in default 2 instead of the configured 4 "
        f"(status={task.status}, failures={task.consecutive_failures})"
    )
    assert _event_payload(conn, tid, "gave_up") is None


def test_enforce_max_runtime_negative_control_trips_at_limit_two(conn):
    tid = _arm_crashed_worker(
        conn, "timeout at a configured budget of 2", failures=1, max_runtime_seconds=60,
    )

    timed_out = kbd.enforce_max_runtime(conn, signal_fn=lambda *_: None, failure_limit=2)

    task = kb.get_task(conn, tid)
    assert tid in timed_out
    assert task.status == "blocked"
    payload = _event_payload(conn, tid, "gave_up")
    assert payload["failures"] == 2
    assert payload["effective_limit"] == 2
    assert payload["trigger_outcome"] == "timed_out"


def test_enforce_max_runtime_without_a_limit_keeps_the_default(conn):
    """Direct callers that pass no limit keep the documented default (no silent change)."""
    tid = _arm_crashed_worker(
        conn, "timeout without a supplied limit", failures=1, max_runtime_seconds=60,
    )

    assert tid in kbd.enforce_max_runtime(conn, signal_fn=lambda *_: None)

    task = kb.get_task(conn, tid)
    assert task.status == "blocked"
    payload = _event_payload(conn, tid, "gave_up")
    assert payload["effective_limit"] == kbd.DEFAULT_FAILURE_LIMIT
    assert payload["limit_source"] == "default"


# ---------------------------------------------------------------------------
# Rules that must NOT change
# ---------------------------------------------------------------------------


def test_systemic_same_error_wave_still_trips_at_one_with_limit_four(conn):
    """3 identical crash fingerprints in one tick still trip at 1, sticky, limit or not."""
    tids = [
        _arm_crashed_worker(conn, f"systemic wave {i}", failures=0)
        for i in range(3)
    ]

    result = _reclaim(conn, failure_limit=4)

    for tid in tids:
        assert tid in result.crashed
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.consecutive_failures == 1  # tripped well below the configured 4
        payload = _event_payload(conn, tid, "gave_up")
        assert payload["effective_limit"] == 1
        assert payload["sticky"] is True


def test_per_task_max_retries_still_wins_over_the_dispatcher_limit(conn):
    """``max_retries`` > ``failure_limit`` > default: the task override decides."""
    tid = kb.create_task(conn, title="per-task override", assignee="builder", max_retries=6)
    _arm_crashed_worker(conn, "", task_id=tid, failures=3)

    # failures 3 -> 4 -> 5 stay below the task's own budget of 6 even though the
    # dispatcher limit is 2.
    for expected in (4, 5):
        _reclaim(conn, failure_limit=2)
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.consecutive_failures == expected
        assert task.status == "ready", (
            f"dispatcher limit 2 overrode max_retries=6 (failures={task.consecutive_failures})"
        )
        _arm_crashed_worker(conn, "", task_id=tid)

    _reclaim(conn, failure_limit=2)

    task = kb.get_task(conn, tid)
    assert task is not None
    assert task.status == "blocked"
    assert task.consecutive_failures == 6
    payload = _event_payload(conn, tid, "gave_up")
    assert payload is not None
    assert payload["effective_limit"] == 6
    assert payload["limit_source"] == "task"


# ---------------------------------------------------------------------------
# Honest ``limit_source``
# ---------------------------------------------------------------------------


def test_limit_source_default_when_no_limit_was_supplied(conn):
    """A trip at the built-in default must not claim the dispatcher configured it."""
    tid = kb.create_task(conn, title="no limit supplied", assignee="builder")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET consecutive_failures = 1 WHERE id = ?", (tid,))

    assert kbd._record_task_failure(conn, tid, error="boom", outcome="crashed") is True

    payload = _event_payload(conn, tid, "gave_up")
    assert payload["effective_limit"] == kbd.DEFAULT_FAILURE_LIMIT
    assert payload["limit_source"] == "default"


def test_limit_source_dispatcher_when_a_limit_is_supplied(conn):
    tid = kb.create_task(conn, title="dispatcher limit supplied", assignee="builder")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET consecutive_failures = 2 WHERE id = ?", (tid,))

    # 2 -> 3 is below the supplied 4: no trip, and no event to mislabel.
    assert kbd._record_task_failure(
        conn, tid, error="boom", outcome="crashed", failure_limit=4,
    ) is False
    assert _event_payload(conn, tid, "gave_up") is None

    # 3 -> 4 reaches the supplied limit and trips, labelled with its real source.
    assert kbd._record_task_failure(
        conn, tid, error="boom again", outcome="crashed", failure_limit=4,
    ) is True
    payload = _event_payload(conn, tid, "gave_up")
    assert payload is not None
    assert payload["effective_limit"] == 4
    assert payload["limit_source"] == "dispatcher"
