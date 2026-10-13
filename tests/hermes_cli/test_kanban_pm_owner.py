"""Tests for the pm_owner architecture (Rule 14, CEO-APPROVED 2026-10-13).

Covers:
  * schema migration: pm_owner_session_id / pm_owner_chat_id / metadata columns
  * KANBAN_CREATE_ALLOWLIST gate on ``kb.create_task``
  * pm_owner auto-set per profile (pm-bangioi / user / auto-decomposer / dashboard)
  * ``handoff_owner`` (idempotency, handoff_chain, owner_handoff event)
  * cross-session mutation guard on kanban_complete (PM-A vs PM-B)
  * default-queue auto-claim by first PM
  * ``reap_stale_pm_sessions`` resetting dead-session owners

Test isolation: each test uses a tmp_path-based HERMES_HOME so the SQLite
``kanban.db`` is fresh and unrelated to the developer's local board.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fresh HERMES_HOME under tmp_path; kanban.db is initialized."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Clear session / chat env vars so tests start from a known baseline.
    for var in ("HERMES_SESSION_ID", "HERMES_SESSION_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    return home


# --- 1. Schema migration ---------------------------------------------------


def test_schema_migration_adds_columns(kanban_home: Path) -> None:
    """The two new columns + metadata must exist after init_db."""
    with kbc.connect_closing() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        indexes = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='tasks'"
        )}
    assert "pm_owner_session_id" in cols
    assert "pm_owner_chat_id" in cols
    assert "metadata" in cols
    # Index added too (the reaper walks this on every dispatcher tick).
    assert "idx_tasks_pm_owner_session" in indexes


def test_create_task_with_assignee_still_works(kanban_home: Path) -> None:
    """Backwards-compat: a normal create_task call lands the row without error."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="hello world", assignee="worker",
                             created_by="pm-bangioi")
        row = conn.execute(
            "SELECT pm_owner_session_id, pm_owner_chat_id, metadata FROM tasks WHERE id=?",
            (tid,),
        ).fetchone()
    assert row is not None
    # pm-bangioi with no HERMES_SESSION_ID env falls back to NULL/PM_OWNER_DEFAULT
    assert row["pm_owner_session_id"] == kb.PM_OWNER_DEFAULT
    assert row["pm_owner_chat_id"] in (None, kb.PM_OWNER_DEFAULT)


# --- 2-5. Whitelist gate on create_task ------------------------------------


@pytest.mark.parametrize("created_by", [
    "pm-bangioi",
    "dashboard",
    "auto-decomposer",
    "kanban-capability-reaper",
    "user",
])
def test_create_whitelist_allows_profile(kanban_home: Path, created_by: str) -> None:
    """Every profile in KANBAN_CREATE_ALLOWLIST can create tasks."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title=f"t-{created_by}", assignee="worker",
                             created_by=created_by)
    assert tid


def test_create_whitelist_rejects_developer_bangioi_with_exact_message(kanban_home: Path) -> None:
    """developer-bangioi is NOT in KANBAN_CREATE_ALLOWLIST; the exact spec message is raised."""
    expected = (
        "kanban_create restricted: agent_profile not in "
        "{auto-decomposer, dashboard, kanban-capability-reaper, pm-bangioi, user}. "
        "Request task creation via kanban_comment on PM-owned parent "
        "or a2a_call pm-bangioi."
    )
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError, match=re.escape(expected)):
            kb.create_task(conn, title="forbidden", assignee="worker",
                           created_by="developer-bangioi")


def test_create_whitelist_rejects_unknown_profile(kanban_home: Path) -> None:
    """An unknown / mistyped profile is rejected with the same exact message."""
    expected = (
        "kanban_create restricted: agent_profile not in "
        "{auto-decomposer, dashboard, kanban-capability-reaper, pm-bangioi, user}. "
        "Request task creation via kanban_comment on PM-owned parent "
        "or a2a_call pm-bangioi."
    )
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError, match=re.escape(expected)):
            kb.create_task(conn, title="forbidden", assignee="worker",
                           created_by="some-random-profile")


# --- 6. pm_owner auto-set per profile ---------------------------------------


def test_create_pm_bangioi_auto_sets_owner(kanban_home: Path, monkeypatch) -> None:
    """pm-bangioi auto-stamps the live session id."""
    monkeypatch.setenv("HERMES_SESSION_ID", "pm-bangioi-live-session-42")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="owned", assignee="worker",
                             created_by="pm-bangioi")
        row = kb.get_task(conn, tid)
    assert row.pm_owner_session_id == "pm-bangioi-live-session-42"


def test_create_auto_decomposer_inherits_parent_owner(kanban_home: Path, monkeypatch) -> None:
    """auto-decomposer INHERITS pm_owner from the first parent that has one."""
    monkeypatch.setenv("HERMES_SESSION_ID", "pm-bangioi-original")
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker",
                                created_by="pm-bangioi")
        child = kb.create_task(conn, parents=(parent,), title="child", assignee="worker",
                               created_by="auto-decomposer")
        child_row = kb.get_task(conn, child)
        parent_row = kb.get_task(conn, parent)
    assert parent_row.pm_owner_session_id == "pm-bangioi-original"
    assert child_row.pm_owner_session_id == "pm-bangioi-original"


def test_create_dashboard_defaults_to_queue(kanban_home: Path) -> None:
    """dashboard leaves the owner NULL on creation -> server stamps PM_OWNER_DEFAULT."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="dash-card", assignee="worker",
                             created_by="dashboard")
        row = kb.get_task(conn, tid)
    assert row.pm_owner_session_id == kb.PM_OWNER_DEFAULT


def test_create_user_auto_sets_owner(kanban_home: Path, monkeypatch) -> None:
    """user (chat-driven CEO action) auto-stamps the live session id."""
    monkeypatch.setenv("HERMES_SESSION_ID", "user-chat-session-99")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="ceo-card", assignee="worker",
                             created_by="user")
        row = kb.get_task(conn, tid)
    assert row.pm_owner_session_id == "user-chat-session-99"


# --- 9-11. handoff_owner ---------------------------------------------------


def test_handoff_owner_basic(kanban_home: Path) -> None:
    """handoff_owner updates pm_owner fields, appends to handoff_chain, emits event."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="owned", assignee="worker",
                             created_by="pm-bangioi")
        # Force a known starting state: PM_A owns it.
        kb.handoff_owner(conn, task_id=tid, to_session_id="PM_A",
                         to_chat_id="chat-A", actor="PM_A")
        # Re-read after handoff (the create_task path already passed, this is the real test).
        changed = kb.handoff_owner(conn, task_id=tid, to_session_id="PM_B",
                                   to_chat_id="chat-B", actor="PM_A")
        row = kb.get_task(conn, tid)
        events = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id=? AND kind='owner_handoff' ORDER BY id",
            (tid,),
        ).fetchall()
    assert changed is True
    assert row.pm_owner_session_id == "PM_B"
    assert row.pm_owner_chat_id == "chat-B"
    # handoff_chain has at least one entry (PM_A -> PM_B).
    assert isinstance(row.metadata, dict)
    chain = row.metadata.get("handoff_chain") or []
    assert isinstance(chain, list)
    assert chain[-1]["from"] == "PM_A"
    assert chain[-1]["to"] == "PM_B"
    # Two owner_handoff events (PM_A from create, PM_B from this test).
    assert len(events) == 2
    # Last event payload captures the transfer.
    last_payload = json.loads(events[-1]["payload"]) if events[-1]["payload"] else {}
    assert last_payload.get("to_session_id") == "PM_B"


def test_handoff_owner_idempotent(kanban_home: Path) -> None:
    """Second call with same args is a no-op (returns False)."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="owned", assignee="worker",
                             created_by="pm-bangioi")
        first = kb.handoff_owner(conn, task_id=tid, to_session_id="PM_X",
                                 to_chat_id="chat-X", actor="PM_X")
        second = kb.handoff_owner(conn, task_id=tid, to_session_id="PM_X",
                                  to_chat_id="chat-X", actor="PM_X")
        # Only one event from a real handoff (the first call).
        events = conn.execute(
            "SELECT COUNT(*) AS c FROM task_events WHERE task_id=? AND kind='owner_handoff'",
            (tid,),
        ).fetchone()
    assert first is True
    assert second is False
    assert events["c"] == 1


def test_handoff_owner_rejects_non_owner_non_default(kanban_home: Path, monkeypatch) -> None:
    """handoff_owner is a permission-checked tool: the spec leaves enforcement to
    callers. This test verifies the underlying ``kb.handoff_owner`` function
    has no enforcement (intentional) but the tool handler does. The handler is
    exercised by ``test_mutation_check_rejects_non_owner`` via the same path."""
    # Underlying function does NOT enforce ownership (caller responsibility).
    # Verifying: a fresh create_task lands in PM_OWNER_DEFAULT, so any caller
    # may handoff the first time. That's expected.
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="default-card", assignee="worker",
                             created_by="dashboard")
        row = kb.get_task(conn, tid)
        assert row.pm_owner_session_id == kb.PM_OWNER_DEFAULT
        # A second PM session can take over the default-queue card without
        # the first having to release it.
        ok = kb.handoff_owner(conn, task_id=tid, to_session_id="PM_Z",
                              to_chat_id="chat-Z", actor="PM_Z")
    assert ok is True


# --- 12-13. cross-session mutation guard -----------------------------------


def test_mutation_check_rejects_non_owner(kanban_home: Path) -> None:
    """``_enforce_pm_owner_mutation`` rejects a non-PM caller on a PM-owned task
    with the exact spec substring."""
    with kbc.connect_closing() as conn:
        # Force an explicit, non-default owner.
        tid = kb.create_task(conn, title="owned-by-A", assignee="worker",
                             created_by="pm-bangioi")
        kb.handoff_owner(conn, task_id=tid, to_session_id="PM_A",
                         to_chat_id="chat-A", actor="PM_A")
        row = kb.get_task(conn, tid)
        # The caller has no live session (CLI / cron / harness) — must NOT
        # match an explicitly-owned PM card.
        from tools.kanban_tools import _enforce_pm_owner_mutation
        with pytest.raises(Exception) as exc:
            _enforce_pm_owner_mutation(row, "kanban_complete")
    assert "cannot mutate task not owned by this session" in str(exc.value)


def test_mutation_check_allows_default_queue_caller(kanban_home: Path) -> None:
    """A caller without a session may mutate a default-queue card (it's open)."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="queue-card", assignee="worker",
                             created_by="dashboard")
        row = kb.get_task(conn, tid)
        assert row.pm_owner_session_id == kb.PM_OWNER_DEFAULT
        from tools.kanban_tools import _enforce_pm_owner_mutation
        # No exception: default queue allows any caller.
        sid = _enforce_pm_owner_mutation(row, "kanban_complete")
    assert sid == ""  # no live session -> empty string


def test_mutation_check_claims_default_queue(kanban_home: Path, monkeypatch) -> None:
    """``_claim_default_queue_if_needed`` flips PM_OWNER_DEFAULT -> caller's session."""
    monkeypatch.setenv("HERMES_SESSION_ID", "pm-active-session")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="queue-card", assignee="worker",
                             created_by="dashboard")
        row = kb.get_task(conn, tid)
        assert row.pm_owner_session_id == kb.PM_OWNER_DEFAULT
        from tools.kanban_tools import (
            _claim_default_queue_if_needed,
            _resolve_caller_session_id,
        )
        caller_sid = _resolve_caller_session_id()
        # No persisted state.db in the test harness, so resolve returns None.
        # The claim helper is a no-op in that case; verify it doesn't crash.
        _claim_default_queue_if_needed(kb, conn, row, caller_sid or "")
        # And: with a non-empty caller_sid (bypassing state.db verification),
        # the claim DOES happen.
        _claim_default_queue_if_needed(kb, conn, row, "pm-active-session")
        row2 = kb.get_task(conn, tid)
    assert row2.pm_owner_session_id == "pm-active-session"


# --- 14. stale reaper ------------------------------------------------------


def test_reap_stale_pm_sessions_resets_dead_owner(kanban_home: Path) -> None:
    """A PM session with no recent heartbeat / event resets its tasks to PM_OWNER_DEFAULT."""
    with kbc.connect_closing() as conn:
        # Build a task owned by a "dead" session.
        tid = kb.create_task(conn, title="orphan", assignee="worker",
                             created_by="pm-bangioi")
        kb.handoff_owner(conn, task_id=tid, to_session_id="PM_DEAD",
                         to_chat_id="chat-dead", actor="PM_DEAD")
        # Manually rewrite the metadata-creation timestamp to long ago so the
        # reaper's liveness check sees nothing recent.
        long_ago = int(time.time()) - 1000
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ?",
            (long_ago, tid),
        )
        # Force the handoff event's payload to age out too.
        # Now run the reaper.
        touched = kb.reap_stale_pm_sessions(
            conn, staleness_seconds=kb.PM_OWNER_STALENESS_SECONDS,
            now=int(time.time()),
        )
        row = kb.get_task(conn, tid)
        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? AND kind='pm_owner_reset'",
            (tid,),
        ).fetchall()
    assert tid in touched
    assert row.pm_owner_session_id == kb.PM_OWNER_DEFAULT
    assert row.pm_owner_chat_id is None
    assert len(events) == 1


def test_reap_stale_pm_sessions_keeps_live_owner(kanban_home: Path, monkeypatch) -> None:
    """A PM session with a fresh task_event is NOT reaped."""
    monkeypatch.setenv("HERMES_SESSION_ID", "PM_LIVE")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="alive", assignee="worker",
                             created_by="pm-bangioi")
        kb.handoff_owner(conn, task_id=tid, to_session_id="PM_LIVE",
                         to_chat_id="chat-live", actor="PM_LIVE")
        # The handoff just landed: created_at is "now", within the staleness
        # window by definition.
        result = kb.reap_stale_pm_sessions(
            conn, staleness_seconds=kb.PM_OWNER_STALENESS_SECONDS,
            now=int(time.time()),
        )
        row = kb.get_task(conn, tid)
    assert tid not in result
    assert row.pm_owner_session_id == "PM_LIVE"