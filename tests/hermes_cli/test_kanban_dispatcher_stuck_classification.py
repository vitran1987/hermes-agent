"""Regression tests for t_6a107755 — "dispatcher stuck" conflated guards with faults.

The gateway dispatcher counted every tick with a non-empty ready queue and no
spawn as a "bad" tick and warned ``kanban dispatcher stuck`` after six of them.
A tick that deferred every ready card on a spawn guard or on
``kanban.max_in_progress_per_profile`` is normal operation, so a correctly
throttled board logged hundreds of false "stuck" warnings a day.

Three properties are pinned here:

* ``describe_suppression`` reports the per-profile cap (it was collected and
  then dropped, which is why half the warnings named no hold at all);
* ``classify_hold`` explains a zero-spawn tick only when the hold accounts for
  every row the dispatcher evaluated — a spawn that FAILED stays unexplained so
  the loud warning survives;
* the watcher routes a held tick to a distinct INFO line and keeps the
  ``dispatcher stuck`` WARNING verbatim for the genuine case.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
import types

import pytest


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    """Fresh HERMES_HOME with a kanban DB and alpha/beta/gamma profiles."""
    test_home = tempfile.mkdtemp(prefix="kanban_stuck_classification_")
    for prof in ("alpha", "beta", "gamma", "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
        with open(os.path.join(test_home, "profiles", prof, "config.yaml"), "w") as fh:
            fh.write("{}\n")  # identity marker: a bare dir is not a profile
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    from hermes_cli import kanban_db
    from hermes_cli import kanban_db_connect
    from hermes_cli import kanban_db_dispatch
    # The module purge discards the conftest's memory-sample neutralizer;
    # re-apply it so the host's live memory pressure cannot cap these ticks.
    monkeypatch.setattr(kanban_db_dispatch, "_system_memory_sample", dict)
    yield kanban_db, kanban_db_connect, kanban_db_dispatch


def _fake_spawn(*args, **kwargs):
    return 4242


# --------------------------------------------------------------------------
# describe_suppression — the per-profile cap must reach the log line
# --------------------------------------------------------------------------

def test_describe_suppression_counts_per_profile_cap(isolated_kanban_home):
    """The cap bucket was collected and dropped; it must be counted."""
    _kb, _kbc, kbd = isolated_kanban_home
    res = kbd.DispatchResult()
    res.skipped_per_profile_capped.append(("t1", "developer-bangioi", 3))
    res.skipped_per_profile_capped.append(("t2", "developer-bangioi", 3))

    out = kbd.describe_suppression([res])

    assert "per_profile_capped=2" in out
    assert "profile_at_cap=developer-bangioi(running=3)" in out


def test_describe_suppression_names_guard_and_cap_together(isolated_kanban_home):
    _kb, _kbc, kbd = isolated_kanban_home
    res = kbd.DispatchResult()
    res.respawn_guarded.append(("a", "active_pr"))
    res.rate_limited.append("b")
    res.skipped_per_profile_capped.append(("c", "alpha", 2))
    res.skipped_per_profile_capped.append(("d", "beta", 2))

    out = kbd.describe_suppression([res])

    assert "active_pr=1" in out
    assert "rate_limited=1" in out
    assert "per_profile_capped=2" in out
    assert "profile_at_cap=alpha(running=2),beta(running=2)" in out


def test_describe_suppression_empty_when_nothing_held(isolated_kanban_home):
    _kb, _kbc, kbd = isolated_kanban_home
    assert kbd.describe_suppression([kbd.DispatchResult()]) == ""


# --------------------------------------------------------------------------
# classify_hold — 'hold' vs 'stuck'
# --------------------------------------------------------------------------

def test_classify_hold_guard_tick_is_not_stuck(isolated_kanban_home):
    _kb, _kbc, kbd = isolated_kanban_home
    res = kbd.DispatchResult()
    res.respawn_guarded.append(("t1", "active_pr"))
    res.ready_considered = 1

    reason = kbd.classify_hold([res])

    assert reason is not None
    assert "active_pr=1" in reason
    assert "active_pr" in reason


def test_classify_hold_per_profile_cap_tick_names_profile_and_cap(isolated_kanban_home):
    """Two ready cards behind a profile already at its cap: a hold, not a fault."""
    kb, kbc, kbd = isolated_kanban_home
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        busy = kb.create_task(conn, title="busy", assignee="alpha")
        assert kb.claim_task(conn, busy, ttl_seconds=600) is not None
        for i in range(2):
            kb.create_task(conn, title=f"waiting{i}", assignee="alpha")

    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, max_in_progress_per_profile=1,
        )

    assert res.spawned == []
    assert len(res.skipped_per_profile_capped) == 2
    assert res.ready_considered == 2

    reason = kbd.classify_hold([res], per_profile_cap=1)

    assert reason is not None
    assert "per_profile_capped=2" in reason
    assert "profile_at_cap=alpha(running=1)" in reason
    assert "max_in_progress_per_profile=1" in reason


def test_classify_hold_host_cap_tick_is_not_stuck(isolated_kanban_home):
    """The host-wide ``max_in_progress`` throttle is a hold too."""
    kb, kbc, kbd = isolated_kanban_home
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        busy = kb.create_task(conn, title="busy", assignee="alpha")
        assert kb.claim_task(conn, busy, ttl_seconds=600) is not None
        kb.create_task(conn, title="waiting", assignee="beta")

    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, max_in_progress=1)

    assert res.spawned == []
    assert res.tick_suppressed == "max_in_progress"
    assert "tick_capped=max_in_progress" in kbd.describe_suppression([res])
    assert kbd.classify_hold([res]) is not None


def test_classify_hold_genuinely_stuck_tick_returns_none(isolated_kanban_home):
    """A spawn that raised is not a hold: the loud warning must survive."""
    kb, kbc, kbd = isolated_kanban_home
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="cannot spawn", assignee="alpha")

    def _explode(*args, **kwargs):
        raise RuntimeError("profile venv missing")

    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_explode, failure_limit=5)

    assert res.spawned == []
    assert res.ready_considered == 1
    assert res.skipped_per_profile_capped == []
    assert res.respawn_guarded == []
    # A spawn the host refused is a fault: never classify it as a hold, and name
    # it in the suppression line so the warning carries the reason.
    described = kbd.describe_suppression([res])
    assert "spawn_error=1" in described
    assert "RuntimeError" in described
    assert kbd.classify_hold([res]) is None


def test_classify_hold_does_not_mask_a_failure_behind_a_cap(isolated_kanban_home):
    """A real spawn failure next to a capped card keeps the tick 'stuck'."""
    kb, kbc, kbd = isolated_kanban_home
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        busy = kb.create_task(conn, title="busy", assignee="alpha")
        assert kb.claim_task(conn, busy, ttl_seconds=600) is not None
        kb.create_task(conn, title="capped", assignee="alpha")
        broken = kb.create_task(conn, title="broken", assignee="beta")

    def _explode_for_broken(task, *args, **kwargs):
        if task.assignee == "beta":
            raise RuntimeError("profile venv missing")
        return 4242

    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=_explode_for_broken, max_in_progress_per_profile=1,
            failure_limit=5,
        )

    assert res.spawned == []
    assert len(res.skipped_per_profile_capped) == 1
    assert res.ready_considered == 2  # capped card + the one that failed to spawn
    assert kbd.classify_hold([res], per_profile_cap=1) is None


# --------------------------------------------------------------------------
# Watcher routing — held ticks are INFO, unexplained ticks keep the WARNING
# --------------------------------------------------------------------------

def _watcher_harness(monkeypatch, results, *, ready=True, ticks=6):
    """Drive `_kanban_dispatcher_watcher` for N ticks against fake tick results."""
    import gateway.kanban_watchers as kw

    class _FakeDispatcher:
        def __call__(self, kb, settings):
            return self

        def tick_once(self):
            return results

        def ready_nonempty(self):
            return ready

        def auto_decompose_tick(self, n):
            return 0

    class _AsyncioShim:
        CancelledError = asyncio.CancelledError

        @staticmethod
        async def sleep(_seconds):
            return None

    class _Mixin(kw.GatewayKanbanWatchersMixin):
        def __init__(self):
            self._running = True
            self._kanban_dispatcher_lock_handle = None
            self._ticks = 0
            self._released = False

        def _kanban_dispatcher_boot(self):
            return (lambda: {}, types.SimpleNamespace(), {"dispatch_interval_seconds": 1})

        async def _sleep_between_ticks(self, interval):
            self._ticks += 1
            if self._ticks >= ticks:
                self._running = False

        def _release_kanban_dispatcher_lock(self):
            self._released = True

    async def _inline(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(kw, "asyncio", _AsyncioShim)
    monkeypatch.setattr(kw, "_to_thread_process_service", _inline)
    monkeypatch.setattr(kw, "_kanban_dispatch_allowed", lambda: True)
    monkeypatch.setattr(kw, "_resolve_auto_decompose_settings", lambda load: (False, 0))
    monkeypatch.setattr(
        kw, "_resolve_dispatcher_settings",
        lambda cfg, kb: types.SimpleNamespace(
            interval=1.0, max_in_progress_per_profile=3,
        ),
    )
    monkeypatch.setattr(kw, "_KanbanDispatcher", _FakeDispatcher())
    _real_kbd = kw._kbd()  # capture before patching: the stub delegates to the real module
    monkeypatch.setattr(
        kw, "_kbd", lambda: types.SimpleNamespace(
            reap_worker_zombies=lambda: [],
            classify_hold=_real_kbd.classify_hold,
            describe_suppression=_real_kbd.describe_suppression,
        ),
    )
    return _Mixin


def _dispatch_result(**kwargs):
    from hermes_cli import kanban_db_dispatch as kbd
    return kbd.DispatchResult(**kwargs)


def test_watcher_holds_capped_tick_without_stuck_warning(monkeypatch, caplog, isolated_kanban_home):
    """Six consecutive cap-held ticks: INFO only, no 'dispatcher stuck'."""
    _kb, _kbc, kbd = isolated_kanban_home
    res = kbd.DispatchResult(ready_considered=2)
    res.skipped_per_profile_capped.append(("t1", "alpha", 3))
    res.skipped_per_profile_capped.append(("t2", "alpha", 3))
    results = [("default", res)]

    mixin_cls = _watcher_harness(monkeypatch, results)
    caplog.set_level(logging.DEBUG, logger="gateway.run")
    asyncio.run(mixin_cls()._kanban_dispatcher_watcher())

    messages = [r.getMessage() for r in caplog.records]
    stuck = [m for m in messages if "dispatcher stuck" in m]
    idle = [m for m in messages if "dispatcher idle" in m]

    assert stuck == [], f"cap-held ticks must not warn 'stuck': {stuck}"
    assert len(idle) == 1, f"expected one quieter hold line, got {idle}"
    assert "per_profile_capped=2" in idle[0]
    assert "profile_at_cap=alpha(running=3)" in idle[0]
    assert "max_in_progress_per_profile=3" in idle[0]
    assert "Not a fault" in idle[0]


def test_watcher_warns_verbatim_on_unexplained_tick(monkeypatch, caplog, isolated_kanban_home):
    """A zero-spawn tick nothing explains keeps the existing warning verbatim."""
    _kb, _kbc, kbd = isolated_kanban_home
    results = [("default", kbd.DispatchResult(ready_considered=1))]

    mixin_cls = _watcher_harness(monkeypatch, results)
    caplog.set_level(logging.DEBUG, logger="gateway.run")
    asyncio.run(mixin_cls()._kanban_dispatcher_watcher())

    messages = [r.getMessage() for r in caplog.records]
    stuck = [m for m in messages if "dispatcher stuck" in m]
    idle = [m for m in messages if "dispatcher idle" in m]

    assert len(stuck) == 1, f"expected exactly one stuck warning, got {stuck}"
    assert stuck[0] == (
        "kanban dispatcher stuck: ready queue non-empty for 6 consecutive ticks "
        "but 0 workers spawned. Check profile health (venv, PATH, credentials) "
        "and `hermes kanban list --status ready`."
    )
    assert idle == []


def test_watcher_warns_and_names_the_spawn_failure(monkeypatch, caplog, isolated_kanban_home):
    """Case (c) with a cause: a spawn the host refused stays a loud fault and is named.

    The card's third case ("nothing explainable") must keep the warning verbatim;
    when the dispatcher *does* know why — ``spawn_fn`` raised — the same warning
    has to carry the reason instead of leaving the operator a bare zero count.
    """
    _kb, _kbc, kbd = isolated_kanban_home
    res = kbd.DispatchResult(ready_considered=1)
    res.spawn_errors.append(("t_broken", "RuntimeError: profile venv missing"))
    results = [("default", res)]

    mixin_cls = _watcher_harness(monkeypatch, results)
    caplog.set_level(logging.DEBUG, logger="gateway.run")
    asyncio.run(mixin_cls()._kanban_dispatcher_watcher())

    messages = [r.getMessage() for r in caplog.records]
    stuck = [m for m in messages if "dispatcher stuck" in m]
    idle = [m for m in messages if "dispatcher idle" in m]

    assert idle == [], f"a failed spawn is not a deliberate hold: {idle}"
    assert len(stuck) == 1, f"expected the stuck warning, got {stuck}"
    assert stuck[0].startswith("kanban dispatcher stuck: ready queue non-empty")
    assert "spawn_error=1 (t_broken: RuntimeError: profile venv missing)" in stuck[0]


def test_watcher_never_reports_a_fault_tick_as_a_deliberate_hold(monkeypatch, caplog, isolated_kanban_home):
    """Long fault run: the WARNING repeats, the quieter 'idle' line never fires.

    ``held_ticks`` might otherwise accumulate on fault ticks and slip past the
    ``bad_ticks`` window (the warning is rate-limited to once per 300s), which
    would print "not a fault" over a genuinely stuck dispatcher.
    """
    _kb, _kbc, kbd = isolated_kanban_home
    results = [("default", kbd.DispatchResult(ready_considered=1))]

    mixin_cls = _watcher_harness(monkeypatch, results, ticks=14)
    caplog.set_level(logging.DEBUG, logger="gateway.run")
    asyncio.run(mixin_cls()._kanban_dispatcher_watcher())

    messages = [r.getMessage() for r in caplog.records]
    assert [m for m in messages if "dispatcher stuck" in m], messages
    assert [m for m in messages if "dispatcher idle" in m] == [], messages
