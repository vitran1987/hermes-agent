"""Regression tests for the dispatcher cap visibility + staleness fix (t_7bea7966).

Incident (2026-10-22): ``kanban.max_in_progress`` edits in config.yaml were
silently ignored by the running gateway until a restart — the booted value
(7) stayed a secret, three edits (cap 5 -> 3) never took effect, and
stakeholders concluded the cap was "broken".

Covers the three acceptance criteria:

1. Settings resolved once at boot (default): a config mismatch must emit a
   staleness WARNING naming both values and the restart remedy, and the
   in-memory settings must stay authoritative (no reload).
2. Opt-in hot reload (``kanban.reload_dispatcher_settings_on_change: true``):
   a changed cap is re-resolved and the new settings are returned for the
   caller to swap in.
3. The reload path never touches live workers: lowering the cap below the
   current population stops new spawns, sets ``DispatchResult.over_cap``,
   and leaves every running task in ``running`` (no evict / park /
   reclaim / kill).
"""
from __future__ import annotations

import importlib
import os
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from gateway.kanban_watchers_dispatcher import (
    _DispatcherSettings,
    _KanbanDispatcher,
    _resolve_dispatcher_settings,
)

RELOAD_KEY = "reload_dispatcher_settings_on_change"


def _make_settings(**overrides) -> _DispatcherSettings:
    base = dict(
        interval=60.0,
        max_spawn=None,
        max_in_progress=7,
        failure_limit=2,
        stale_timeout_seconds=0,
        reconcile_orphans=True,
        default_assignee=None,
        max_in_progress_per_profile=3,
    )
    base.update(overrides)
    return _DispatcherSettings(**base)  # type: ignore[arg-type]


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with the default board and a spawnable profile."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (home / "profiles" / "pm").mkdir(parents=True)
    (home / "profiles" / "pm" / "config.yaml").write_text("{}\n")
    with kbc.connect_closing():
        try:
            kb.create_board(slug="default", name="Test")
        except Exception:
            pass
    return home


@pytest.fixture
def neutralize_memory_guard(monkeypatch):
    """No memory-pressure interference for cap tests."""
    monkeypatch.setattr(kbd, "_system_memory_sample", dict)
    return monkeypatch


# ---------------------------------------------------------------------------
# 1. Settings resolved once at boot
# ---------------------------------------------------------------------------


def test_boot_resolution_logs_caps_and_stores_them(caplog):
    """Boot resolution parses + logs the caps and stores them in the dataclass."""
    with caplog.at_level("INFO"):
        settings = _resolve_dispatcher_settings(
            {"max_in_progress": 7, "max_in_progress_per_profile": 3}, kb)
    assert settings.max_in_progress == 7
    assert settings.max_in_progress_per_profile == 3
    assert settings.interval == 60.0
    assert settings.failure_limit == kb.DEFAULT_FAILURE_LIMIT
    # The resolved caps are part of the startup log line.
    assert any("max_in_progress=7" in rec.message for rec in caplog.records)


def test_boot_resolution_derives_memory_default_when_unset(caplog, monkeypatch):
    """Unset cap -> memory-derived default, logged so the booted value is not a secret."""
    monkeypatch.setattr(kbd, "derive_default_max_in_progress", lambda: 5)
    with caplog.at_level("INFO"):
        settings = _resolve_dispatcher_settings({}, kb)
    assert settings.max_in_progress == 5
    assert any("memory-derived default" in rec.message
               and "max_in_progress=5" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# 2. Staleness warning (default mode: no reload, in-memory stays authoritative)
# ---------------------------------------------------------------------------


def test_staleness_warning_fires_on_cap_mismatch(caplog):
    """config.yaml cap 3 vs in-memory 7 (the incident shape): WARNING names
    both values and the restart remedy; no reload happens."""
    cfg = {"kanban": {"max_in_progress": 3, "max_in_progress_per_profile": 2}}
    dispatcher = _KanbanDispatcher(kb, _make_settings())
    with caplog.at_level("WARNING"):
        result = dispatcher.check_config_staleness(lambda: cfg, kb)
    assert result is None, "default mode must never swap settings"
    assert dispatcher.settings.max_in_progress == 7, "in-memory settings stay authoritative"
    warnings = [r for r in caplog.records if r.levelno >= 30 and "config.yaml disagrees" in r.message]
    assert len(warnings) == 1, [r.message for r in caplog.records]
    assert "config=3" in warnings[0].message and "running=7" in warnings[0].message
    assert "restart the gateway" in warnings[0].message
    assert "reload_dispatcher_settings_on_change" in warnings[0].message


def test_no_staleness_warning_when_config_matches(caplog):
    cfg = {"kanban": {"max_in_progress": 7, "max_in_progress_per_profile": 3}}
    dispatcher = _KanbanDispatcher(kb, _make_settings())
    with caplog.at_level("WARNING"):
        assert dispatcher.check_config_staleness(lambda: cfg, kb) is None
    assert not any("config.yaml disagrees" in r.message for r in caplog.records)


def test_staleness_warning_survives_config_load_failure(caplog, monkeypatch):
    """A transient load error is swallowed: no crash, no false warning."""
    def _boom():
        raise RuntimeError("config unreadable")
    dispatcher = _KanbanDispatcher(kb, _make_settings())
    with caplog.at_level("WARNING"):
        assert dispatcher.check_config_staleness(_boom, kb) is None
    assert not any("config.yaml disagrees" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 3. Opt-in hot reload
# ---------------------------------------------------------------------------


def test_optin_reload_swaps_settings_on_cap_change(caplog):
    """With the opt-in flag, a changed cap is re-resolved and returned for
    the watcher to swap in; no warning-about-staleness, an apply notice."""
    cfg = {"kanban": {
        "max_in_progress": 3,
        "max_in_progress_per_profile": 2,
        RELOAD_KEY: True,
    }}
    dispatcher = _KanbanDispatcher(kb, _make_settings())
    with caplog.at_level("WARNING"):
        new_settings = dispatcher.check_config_staleness(lambda: cfg, kb)
    assert new_settings is not None, "opt-in mode must return the new settings"
    assert new_settings.max_in_progress == 3
    assert new_settings.max_in_progress_per_profile == 2
    assert new_settings.interval == 60.0
    # The watcher side: swap + next tick uses the new caps.
    dispatcher.settings = new_settings
    kwargs = dispatcher.settings.as_tick_kwargs()
    assert kwargs["max_in_progress"] == 3
    assert kwargs["max_in_progress_per_profile"] == 2
    assert "interval" not in kwargs and "config_signature" not in kwargs
    # Re-check with no change: stable (no repeated swaps).
    assert dispatcher.check_config_staleness(lambda: cfg, kb) is None


def test_optin_reload_disabled_flag_is_false_by_default():
    """The hot reload is OFF unless the operator explicitly opts in."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["kanban"][RELOAD_KEY] is False


def test_optin_reload_keeps_old_settings_when_config_is_broken(caplog):
    """A resolution failure (e.g. memory read breaks) keeps the in-memory
    values: no partial swap, and the failure is logged.

    The checker imports the dispatch module fresh per call, so the patch
    must outlive that import (module re-created inside the try) and the
    original is restored in finally.
    """
    def _boom(configured):
        raise RuntimeError("resolve failure")

    # Restore the pristine module in finally (a plain re-import after the
    # pop gives a clean, unpatched module — the previous test's patch, if any,
    # must NOT survive this test).
    import importlib

    def _restore_pristine():
        """Re-import the module from disk so the patched name does not leak
        to later tests in this file (sys.modules cache is the only import
        state; a pop + fresh import is a clean slate)."""
        sys.modules.pop("hermes_cli.kanban_db_dispatch", None)
        importlib.import_module("hermes_cli.kanban_db_dispatch")
        # Re-bind the module-level name `kbd` to the pristine module object
        # so sibling tests in this file do not see the patched function.
        test_mod = sys.modules[__name__]
        test_mod.kbd = sys.modules["hermes_cli.kanban_db_dispatch"]

    sys.modules.pop("hermes_cli.kanban_db_dispatch", None)
    # Import once, patch that object, and let the checker's fresh import
    # reuse it from sys.modules (import never re-executes a cached module).
    import hermes_cli.kanban_db_dispatch as _kbd_fresh
    _kbd_fresh.resolve_max_in_progress = _boom  # type: ignore[assignment]
    try:
        kbd.resolve_max_in_progress = _boom  # type: ignore[assignment]
        cfg = {"kanban": {"max_in_progress": 3, RELOAD_KEY: True}}
        dispatcher = _KanbanDispatcher(kb, _make_settings(max_in_progress=5))
        with caplog.at_level("ERROR"):
            new_settings = dispatcher.check_config_staleness(lambda: cfg, kb)
    finally:
        _restore_pristine()
    assert new_settings is None
    assert dispatcher.settings.max_in_progress == 5
    assert any("compare config.yaml" in r.message or "re-resolve" in r.message
               for r in caplog.records)


# ---------------------------------------------------------------------------
# 4. Reload path never evicts live workers (AC 4 + 5)
# ---------------------------------------------------------------------------

_FAKE_PID = 424242


def _fake_spawn(*args, **kwargs):
    return _FAKE_PID


def _seed_running_tasks(n, assignee="pm"):
    """Seed n tasks in a realistic 'running' state via the dispatcher's own
    machinery (claim_task + PID), so the rows are normal claimed tasks —
    with a live claim_lock/claim_expires — not orphan bookkeeping."""
    import socket
    import time as _time

    lock = f"test-host:{os.getpid()}:seed"
    ttl = 86400
    with kbc.connect_closing() as conn:
        ids = []
        for i in range(n):
            tid = kb.create_task(conn, title=f"live-{i}", assignee=assignee)
            ids.append(tid)
            claimed = kb.claim_task(conn, tid, ttl_seconds=ttl, claimer=lock)
            assert claimed is not None, f"seed task {tid} must claim"
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET worker_pid = ?, worker_started_at = ? "
                    "WHERE id = ?",
                    (_FAKE_PID, int(_time.time()), tid))
        rows = conn.execute(
            "SELECT id, status, claim_lock, claim_expires FROM tasks "
            "WHERE status = 'running'").fetchall()
    assert len(rows) == n, "seeded tasks must all be running"
    assert all(r["claim_lock"] == lock and r["claim_expires"] > 0 for r in rows)
    return [r["id"] for r in rows]


def test_lowered_cap_below_population_never_evicts(
        kanban_home, neutralize_memory_guard, monkeypatch):
    """Population 7, cap lowered to 3 (the incident numbers): the next tick
    spawns nothing, flags over_cap, and every running task STAYS running —
    no evict, park, reclaim or kill from the reload/spawn-budget path."""
    running = _seed_running_tasks(7)
    assert len(running) == 7
    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, max_in_progress=3)
    assert res.over_cap == 3
    assert res.spawned == []
    assert res.crashed == []
    assert res.timed_out == []
    assert res.stale == []
    assert res.reconciled_orphans == []
    assert res.reaped_terminal_workers == []
    assert res.skipped_nonspawnable == []
    assert res.rate_limited == []
    with kbc.connect_closing() as conn:
        rows = conn.execute(
            "SELECT id, status, worker_pid FROM tasks").fetchall()
    assert {r["id"] for r in rows} == set(running)
    assert all(r["status"] == "running" and r["worker_pid"] == _FAKE_PID
               for r in rows), "lowering the cap must not move live workers"


def test_population_at_exact_cap_stops_new_spawns(kanban_home, neutralize_memory_guard):
    """At the cap (population == cap, no ready tasks) the budget is 0 and the
    tick reports it as over-cap; nothing spawns."""
    _seed_running_tasks(3)
    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, max_in_progress=3)
    assert res.over_cap == 3
    assert res.spawned == []
    with kbc.connect_closing() as conn:
        assert all(r["status"] == "running" for r in conn.execute(
            "SELECT id, status FROM tasks").fetchall())


def test_under_cap_still_spawns_and_no_over_cap(kanban_home, neutralize_memory_guard):
    """Population 2 of cap 3: one more ready task spawns; no over_cap flag."""
    _seed_running_tasks(2)
    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="ready-one", assignee="pm")
    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, max_in_progress=3)
    assert res.over_cap is None
    assert len(res.spawned) == 1
    with kbc.connect_closing() as conn:
        assert conn.execute(
            "SELECT COUNT(*) n FROM tasks WHERE status = 'running'").fetchone()["n"] == 3


def test_staleness_check_itself_performs_no_board_writes(kanban_home, monkeypatch):
    """The staleness check reads config only: it must not open any board DB
    or claim/spawn anything, even when the reload flag is on."""
    calls = []
    # Patch the function on the module the checker imports fresh.
    import hermes_cli.kanban_db_connect as kbc_fresh
    monkeypatch.setattr(
        kbc_fresh, "connect",
        lambda *a, **k: calls.append(("connect", a, k)) or None)
    cfg = {"kanban": {"max_in_progress": 3, RELOAD_KEY: True}}
    dispatcher = _KanbanDispatcher(kb, _make_settings())
    new_settings = dispatcher.check_config_staleness(lambda: cfg, kb)
    assert new_settings is not None
    assert calls == [], "staleness check must not touch board DBs"
    assert dispatcher.settings.max_in_progress == 7  # swap is caller-driven
