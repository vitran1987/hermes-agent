"""``_kanban_busy`` is scoped to the profiles a gateway serves, not to the whole board (#t_8e160544).

A board-global probe made one due card anywhere hold every ``--idle-exit`` gateway on the host, so no
unmanaged lane daemon ever retired while the board was busy for somebody else. Real SQLite boards with
real rows; only ``list_boards``/``kanban_db_path`` are pointed at the temp ones, and the runner is a
stand-in exposing just what the idle predicate reads.
"""
from __future__ import annotations

import itertools
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import run_idle_exit
from gateway.config import Platform
from gateway.runtime_bootstrap import TicketStore
from gateway.session_authorities import SessionAuthorities
from hermes_cli import kanban_db
from hermes_constants import get_default_hermes_root
from hermes_state import SessionDB

_IDS = itertools.count(1)


@pytest.fixture
def boards(tmp_path, monkeypatch):
    """``slug -> path`` for the two real boards the probe enumerates (``default`` + one named)."""
    paths, metas = {}, []
    for slug in ("default", "lane"):
        path = tmp_path / f"{slug}.db"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL, "
                         "assignee TEXT, claim_lock TEXT)")
        paths[slug] = path
        metas.append({"slug": slug})
    monkeypatch.setattr(kanban_db, "list_boards", lambda **_kw: metas)
    monkeypatch.setattr(kanban_db, "kanban_db_path", lambda board=None: paths[board or "default"])
    return paths


def _card(path, *, status="ready", assignee=None, claim_lock=None):
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO tasks VALUES (?,?,?,?)",
                     (f"t_{next(_IDS):04d}", status, assignee, claim_lock))


def _profile_home(tmp_path, name):
    home = tmp_path / "profiles" / name
    home.mkdir(parents=True)
    return home


def _runner(tmp_path, monkeypatch, home):
    db = SessionDB(db_path=tmp_path / "state.db")
    registry = SessionAuthorities(home)
    registry.add(home, SimpleNamespace(profile_id=str(home), db=db))

    async def stop():
        runner._running = False
    runner = SimpleNamespace(
        _running=True, _draining=False, session_authorities=registry,
        adapters={Platform.LOCAL: object()}, _profile_adapters={}, _profile_failed_platforms={},
        _failed_platforms={}, config=SimpleNamespace(platforms={}),
        session_ticket_store=TicketStore("i", frozenset({str(home)})),
        session_runtime_descriptor={"state": "ready", "capabilities": ["x"]}, _exit_reason=None,
        _idle_exit_clients=0, _active_work_count=lambda: 0,
        _scale_to_zero_has_live_background_work=lambda: False, stop=stop)
    run_idle_exit.note_client_activity(runner)
    runner._idle_exit_last_activity -= 3600
    monkeypatch.setattr("gateway.run._cron_tick_profile_homes", lambda config: [])
    return runner, db


def test_a_card_for_another_profile_leaves_this_gateway_idle(boards, tmp_path, monkeypatch):
    """The defect: a card owed by ``lane-x`` used to keep ``lane-y``'s daemon up forever."""
    _card(boards["default"], status="ready", assignee="lane-x")
    _card(boards["lane"], status="running", assignee="lane-x")
    home = _profile_home(tmp_path, "lane-y")
    runner, db = _runner(tmp_path, monkeypatch, home)
    try:
        assert run_idle_exit._kanban_busy([home]) is None
        assert run_idle_exit.busy_reason(runner, window=0.2) is None
    finally:
        db.close()


def test_a_card_for_a_served_profile_keeps_its_gateway_up(boards, tmp_path, monkeypatch):
    _card(boards["default"], status="ready", assignee="lane-x")
    _card(boards["lane"], status="review", assignee="lane-y")
    home = _profile_home(tmp_path, "lane-y")
    runner, db = _runner(tmp_path, monkeypatch, home)
    try:
        assert run_idle_exit._kanban_busy([home]) == "kanban work for lane-y on board lane"
        assert "kanban work for lane-y" in run_idle_exit.busy_reason(runner, window=0.2)
    finally:
        db.close()


@pytest.mark.parametrize("status", run_idle_exit._KANBAN_DUE_STATUSES)
def test_every_due_status_holds_a_served_profile(boards, tmp_path, status):
    _card(boards["default"], status=status, assignee="lane-y")
    home = _profile_home(tmp_path, "lane-y")
    assert run_idle_exit._kanban_busy([home]) == "kanban work for lane-y on board default"


@pytest.mark.parametrize("status", ("done", "todo", "blocked", "archived"))
def test_a_resting_status_holds_nothing(boards, tmp_path, status):
    _card(boards["default"], status=status, assignee="lane-y")
    home = _profile_home(tmp_path, "lane-y")
    assert run_idle_exit._kanban_busy([home]) is None


def test_the_assignee_is_matched_through_profile_normalization(boards, tmp_path):
    """Dashboard/CLI writes may carry a title-cased label; the row is still lane-y's card."""
    _card(boards["default"], status="ready", assignee=" Lane-Y ")
    home = _profile_home(tmp_path, "lane-y")
    assert run_idle_exit._kanban_busy([home]) == "kanban work for lane-y on board default"


def test_a_card_this_gateway_claimed_holds_it_whoever_owns_it(boards, tmp_path, monkeypatch):
    """A spawn this process's dispatcher started is not lost by exiting, so it still holds."""
    _card(boards["default"], status="running", assignee="lane-x",
          claim_lock=kanban_db._claimer_id())
    _card(boards["lane"], status="running", assignee="lane-x", claim_lock="otherhost:4242")
    home = _profile_home(tmp_path, "lane-y")
    monkeypatch.setattr(run_idle_exit, "_own_claim_lock", kanban_db._claimer_id)
    assert run_idle_exit._kanban_busy([home]) == "kanban work this gateway claimed on board default"


def test_a_foreign_claim_on_an_unserved_profile_holds_nothing(boards, tmp_path):
    _card(boards["default"], status="running", assignee="lane-x", claim_lock="otherhost:4242")
    home = _profile_home(tmp_path, "lane-y")
    assert run_idle_exit._kanban_busy([home]) is None


def test_an_unassigned_card_holds_nothing(boards, tmp_path):
    """No profile can own it: the dispatcher parks a non-spawnable row, nothing acts on it from here."""
    _card(boards["default"], status="ready", assignee=None)
    _card(boards["lane"], status="ready", assignee="CEO_HITL")
    home = _profile_home(tmp_path, "lane-y")
    assert run_idle_exit._kanban_busy([home]) is None


def test_a_throwaway_home_owns_no_card(boards, tmp_path):
    """A script/test/one-shot ``--idle-exit`` home is no profile home: it holds nothing and retires."""
    _card(boards["default"], status="ready", assignee="default")
    scratch = tmp_path / "scratch-home"
    scratch.mkdir()
    assert run_idle_exit._served_profile_names([scratch]) == frozenset()
    assert run_idle_exit._kanban_busy([scratch]) is None


def test_the_default_home_serves_the_default_profile(boards, tmp_path):
    """The default home's basename is an install detail, not the profile id ``default``."""
    root = get_default_hermes_root()
    assert run_idle_exit._served_profile_names([Path(root)]) == frozenset({"default"})
    _card(boards["default"], status="scheduled", assignee="default")
    assert run_idle_exit._kanban_busy([Path(root)]) == "kanban work for default on board default"
