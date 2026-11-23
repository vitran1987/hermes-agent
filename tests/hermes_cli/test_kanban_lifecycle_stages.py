"""One-card lifecycle (CEO directive 2026-11-23): the dispatcher walks a declared
``implement -> review -> [sec] -> merge`` chain on ONE card.

What these tests pin, stage by stage:

* the card's assignee is moved by the DISPATCHER (``stage_advanced``), never by an
  operator ``assigned`` event;
* no child card is created for any stage -- every stage is the same task id;
* the ``active_pr`` respawn guard accepts the dispatcher's stage handoff instead of
  parking the card (and still holds a card with no handoff at all);
* ``sec_required_if`` decides the security stop from the PR's changed files, and
  fails closed when the diff cannot be read;
* a card that declares no lifecycle is untouched by the phase.

Everything runs on a throwaway board (``HERMES_HOME``) with a fake spawn: no real
worker, no GitHub call.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_lifecycle as kbl
from hermes_cli.kanban_pr_acceptance import parse_lifecycle_contract, validate_contract

PR_URL = "https://github.com/Cong-ty-TNHH-Ban-Gioi/bangioi/pull/19197"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    test_home = tmp_path / ".hermes"
    test_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(test_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return test_home


@pytest.fixture()
def spawned(monkeypatch):
    """Every spawn the dispatcher performs, as ``(task_id, assignee)``."""
    seen: list[tuple[str, str]] = []

    def fake_spawn(task, _workspace, board=None):
        seen.append((task.id, task.assignee or ""))
        return 424242  # not a live PID: the claim is never "live"

    return seen, fake_spawn


@pytest.fixture()
def acceptance_ok(monkeypatch):
    """The exact-head gate, stubbed: these tests are about stage routing."""
    from hermes_cli import kanban_pr_acceptance_store as store

    monkeypatch.setattr(
        store, "collect_acceptance",
        lambda contract, published_pr=None, assignee=None: {
            "ok": True, "classification": "success", "head_sha": "a" * 40,
            "pr_url": PR_URL, "checks": [],
        },
    )


def _contract(**overrides) -> str:
    spec = {
        "lifecycle": "full",
        "stages": ["implement", "review", "sec", "merge"],
        "sec_required_if": [".github/workflows/**", "supabase/migrations/**"],
        "pr": PR_URL,
    }
    spec.update(overrides)
    return json.dumps(spec)


def _events(conn, task_id):
    return [
        (row["kind"], kb._json_dict(row["payload"]))
        for row in conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
        )
    ]


def _advances(conn, task_id):
    return [(p["from"], p["to"]) for k, p in _events(conn, task_id) if k == "stage_advanced"]


def _task(conn, task_id):
    return kb.get_task(conn, task_id)


def test_lifecycle_walks_all_four_stages_without_operator_reassign(
    home, spawned, acceptance_ok, all_assignees_spawnable, monkeypatch,
):
    """implement -> review -> sec -> merge -> done, with no ``assigned`` event.

    The sec gate is fed a workflow path, so the security stop must be inserted.
    """
    seen, fake = spawned
    monkeypatch.setattr(
        kbl, "changed_paths", lambda url, assignee=None: [".github/workflows/ci-pr.yml"],
    )
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="lifecycle card", assignee="developer-bangioi",
            completion_contract=_contract(),
        )

        # --- stage 1: implement ------------------------------------------------
        first = kbd.dispatch_once(conn, spawn_fn=fake)
        assert first.spawned and first.spawned[0][0] == tid
        assert seen == [(tid, "developer-bangioi")]

        # The implementer hands the card to review; it names NO reviewer, so the
        # assignee is still the implementer when the lifecycle takes over.
        assert kb.request_review(conn, tid, summary="implemented, PR open")
        assert _task(conn, tid).assignee == "developer-bangioi"

        # --- stage 2: dispatcher advances to review ----------------------------
        second = kbd.dispatch_once(conn, spawn_fn=fake)
        move = [m for m in second.lifecycle_advanced if m.get("task") == tid]
        assert move and move[0]["from"] == "implement" and move[0]["to"] == "review"
        task = _task(conn, tid)
        assert task.assignee == "code-reviewer-bangioi"
        assert task.status in {"review", "running"}  # claimed by the review lane this tick
        # ... and the review lane spawned THAT profile in the same tick.
        assert seen[-1] == (tid, "code-reviewer-bangioi")

        # Reviewer approves. (The acceptance gate is stubbed.)
        assert kb.complete_task(conn, tid, summary="review: approved")

        # --- stage 3: dispatcher advances to sec -------------------------------
        third = kbd.dispatch_once(conn, spawn_fn=fake)
        move = [m for m in third.lifecycle_advanced if m.get("task") == tid]
        assert move and (move[0]["from"], move[0]["to"]) == ("review", "sec")
        task = _task(conn, tid)
        assert task.assignee == "security-reviewer-bangioi"
        assert task.status in {"review", "running"}
        assert seen[-1] == (tid, "security-reviewer-bangioi")
        sec_event = [p for k, p in _events(conn, tid) if k == "stage_advanced"][-1]
        assert sec_event["sec_decision"]["required"] is True
        assert ".github/workflows/ci-pr.yml" in sec_event["sec_decision"]["matched"]

        # Security approves.
        assert kb.complete_task(conn, tid, summary="sec: approved, verdict bound to head")

        # --- stage 4: dispatcher advances to merge -----------------------------
        fourth = kbd.dispatch_once(conn, spawn_fn=fake)
        move = [m for m in fourth.lifecycle_advanced if m.get("task") == tid]
        assert move and (move[0]["from"], move[0]["to"]) == ("sec", "merge")
        task = _task(conn, tid)
        assert task.assignee == "code-reviewer-bangioi"
        assert task.status in {"ready", "running"}
        assert seen[-1] == (tid, "code-reviewer-bangioi")

        # Merge worker merges and completes: the chain is finished, card stays done.
        assert kb.complete_task(conn, tid, summary="merged --squash --match-head-commit")
        fifth = kbd.dispatch_once(conn, spawn_fn=fake)
        assert any(m.get("task") == tid and m.get("completed") for m in fifth.lifecycle_advanced)
        task = _task(conn, tid)
        assert task.status == "done"
        assert task.completed_at is not None

        # --- the acceptance evidence ------------------------------------------
        assert _advances(conn, tid) == [
            ("implement", "review"), ("review", "sec"), ("sec", "merge"),
        ]
        kinds = [k for k, _ in _events(conn, tid)]
        assert kinds.count("lifecycle_completed") == 1
        # No operator reassignment anywhere: only the lifecycle moved the card.
        assert "assigned" not in kinds
        # Every stage lived on the SAME card.
        assert [r["id"] for r in conn.execute("SELECT id FROM tasks")] == [tid]


def test_active_pr_guard_is_lifted_by_the_stage_handoff(home):
    """A card with a PR comment is guarded -- until the dispatcher's handoff."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="card", assignee="developer-bangioi")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
        kb.add_comment(conn, tid, author="worker", body=f"PR opened: {PR_URL}")
        # Negative control: no handoff after the PR comment -> duplicate-PR risk.
        assert kbd.check_respawn_guard(conn, tid, lane="ready") == "active_pr"
        # The dispatcher's stage handoff is the sanctioned way out (a second
        # later: the guard's strictly-after rule keeps a same-second tie guarded).
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, NULL, 'stage_advanced', ?, ?)",
                (tid, json.dumps({
                    "from": "sec", "to": "merge", "assignee": "code-reviewer-bangioi",
                    "source": "dispatcher",
                }), int(time.time()) + 5),
            )
        assert kbd.check_respawn_guard(conn, tid, lane="ready") is None


def test_active_pr_guard_tie_with_the_pr_comment_still_lifts(home):
    """A stage handoff in the SAME second as the PR comment must not park the card.

    Measured before this rule on a real PR: the review->merge advance landed at the
    comment's second, so the strictly-after comparison never saw it and the merge
    stage stayed guarded on every later tick.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="tie card", assignee="developer-bangioi")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        kb.add_comment(conn, tid, author="worker", body=f"PR opened: {PR_URL}")
        comment_time = conn.execute(
            "SELECT created_at FROM task_comments WHERE task_id = ?", (tid,)
        ).fetchone()[0]
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, NULL, 'stage_advanced', ?, ?)",
                (tid, json.dumps({"from": "sec", "to": "merge",
                                  "assignee": "code-reviewer-bangioi"}), comment_time),
            )
        assert kbd.check_respawn_guard(conn, tid, lane="ready") is None


def test_stage_advanced_without_a_target_is_not_a_handoff(home):
    """A malformed stage_advanced (no ``to``) must not lift the guard."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="junk card", assignee="developer-bangioi")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
        kb.add_comment(conn, tid, author="worker", body=f"PR opened: {PR_URL}")
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, NULL, 'stage_advanced', ?, ?)",
                (tid, json.dumps({"from": "sec"}), int(time.time()) + 5),
            )
        assert kbd.check_respawn_guard(conn, tid, lane="ready") == "active_pr"


def test_sec_is_skipped_for_a_docs_only_pr_and_the_reason_is_recorded(
    home, spawned, acceptance_ok, all_assignees_spawnable, monkeypatch,
):
    """``sec_required_if`` does not match a .md-only diff: sec is skipped, logged."""
    seen, fake = spawned
    monkeypatch.setattr(
        kbl, "changed_paths", lambda url, assignee=None: ["docs/hermes-downstream-delta.md"],
    )
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="docs card", assignee="developer-bangioi",
            completion_contract=_contract(),
        )
        kbd.dispatch_once(conn, spawn_fn=fake)
        assert kb.request_review(conn, tid, summary="docs")
        kbd.dispatch_once(conn, spawn_fn=fake)  # implement -> review
        assert kb.complete_task(conn, tid, summary="review: approved")

        move = [m for m in kbd.dispatch_once(conn, spawn_fn=fake).lifecycle_advanced
                if m.get("task") == tid]
        assert move and (move[0]["from"], move[0]["to"]) == ("review", "merge")
        payload = [p for k, p in _events(conn, tid) if k == "stage_advanced"][-1]
        decision = payload["sec_decision"]
        assert decision["required"] is False
        assert "none of 1 changed path(s) match sec_required_if" in decision["reason"]
        assert decision["changed"] == 1
        assert "sec" not in [p["to"] for k, p in _events(conn, tid) if k == "stage_advanced"]


def test_sec_stays_required_when_the_diff_cannot_be_read(home, monkeypatch):
    """Unreadable PR file list must never be read as 'clean diff'."""
    monkeypatch.setattr(kbl, "changed_paths", lambda url, assignee=None: None)
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="unknown diff", assignee="developer-bangioi",
            completion_contract=_contract(),
        )
        row = conn.execute(
            "SELECT id, status, assignee, completion_contract FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        spec = kbl.lifecycle_spec(row["completion_contract"])
        decision = kbl.sec_decision(conn, row, spec)
        assert decision["required"] is True
        assert "unreadable" in decision["reason"]


def test_sec_decision_defaults_to_the_sensitive_paths(home, monkeypatch):
    """A lifecycle that declares ``sec`` but no patterns keeps the security stop."""
    monkeypatch.setattr(kbl, "changed_paths", lambda url, assignee=None: ["README.md"])
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="no patterns", assignee="developer-bangioi",
            completion_contract=_contract(sec_required_if=None),
        )
        row = conn.execute(
            "SELECT id, status, assignee, completion_contract FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        decision = kbl.sec_decision(conn, row, kbl.lifecycle_spec(row["completion_contract"]))
        assert decision["required"] is False
        assert decision["reason"].startswith("none of 1 changed path(s)")

        monkeypatch.setattr(kbl, "changed_paths", lambda url, assignee=None: ["supabase/migrations/1.sql"])
        decision = kbl.sec_decision(conn, row, kbl.lifecycle_spec(row["completion_contract"]))
        assert decision["required"] is True


def test_ordinary_card_is_untouched_by_the_lifecycle_phase(home, spawned, all_assignees_spawnable):
    """No lifecycle declaration -> the phase skips the card and writes nothing."""
    seen, fake = spawned
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="plain card", assignee="developer-bangioi")
        kb.add_comment(conn, tid, author="worker", body=f"PR opened: {PR_URL}")
        assert kbl.run_phase(conn) == []
        res = kbd.dispatch_once(conn, spawn_fn=fake)
        assert res.lifecycle_advanced == []
        kinds = [k for k, _ in _events(conn, tid)]
        assert kinds == ["created", "commented", "respawn_guarded"]  # the pre-existing guard
        assert "stage_advanced" not in kinds and "lifecycle_completed" not in kinds
        assert _task(conn, tid).assignee == "developer-bangioi"


def test_dry_run_plans_the_advance_without_writing(home, acceptance_ok, all_assignees_spawnable):
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="dry", assignee="developer-bangioi", completion_contract=_contract(),
        )
        kbd.dispatch_once(conn, spawn_fn=lambda task, ws: 424242)
        assert kb.request_review(conn, tid, summary="implemented")
        planned = kbl.run_phase(conn, dry_run=True)
        assert planned and planned[0]["task"] == tid and planned[0]["dry_run"] is True
        # Nothing moved.
        assert _task(conn, tid).assignee == "developer-bangioi"
        assert _advances(conn, tid) == []


def test_implement_completed_without_a_review_handoff_is_a_deviation(
    home, acceptance_ok, all_assignees_spawnable,
):
    """A lifecycle card cannot skip its verdict: report, do not invent a stage."""
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="skipped review", assignee="developer-bangioi",
            completion_contract=_contract(),
        )
        kbd.dispatch_once(conn, spawn_fn=lambda task, ws: 424242)
        assert kb.complete_task(conn, tid, summary="done, forgot the review handoff")
        moved = kbl.run_phase(conn)
        assert moved and "deviation" in moved[0]
        kinds = [k for k, _ in _events(conn, tid)]
        assert "lifecycle_deviation" in kinds and "stage_advanced" not in kinds
        assert _task(conn, tid).status == "done"
        # Idempotent: a second tick does not repeat the report.
        assert all("deviation" not in m for m in kbl.run_phase(conn))


def test_changes_requested_sends_the_card_back_to_implement(
    home, spawned, acceptance_ok, all_assignees_spawnable, monkeypatch,
):
    """A reviewer's rejection is a stage move back, on the SAME card."""
    seen, fake = spawned
    monkeypatch.setattr(kbl, "changed_paths", lambda url, assignee=None: [".github/workflows/x.yml"])
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="rejected", assignee="developer-bangioi", completion_contract=_contract(),
        )
        kbd.dispatch_once(conn, spawn_fn=fake)
        assert kb.request_review(conn, tid, summary="implemented")
        kbd.dispatch_once(conn, spawn_fn=fake)  # implement -> review
        run_id = _task(conn, tid).current_run_id
        ok, implementer = kb.request_changes(conn, tid, reason="needs tests", expected_run_id=run_id)
        assert ok and implementer == "developer-bangioi"
        # The dispatcher records the stage hop back (once), so the audit trail is
        # not silent about review -> implement.
        moved = kbl.run_phase(conn)
        assert moved and (moved[0]["from"], moved[0]["to"]) == ("review", "implement")
        assert kbl.run_phase(conn) == []  # idempotent: one record per rejection
        task = _task(conn, tid)
        assert (task.status, task.assignee) == ("ready", "developer-bangioi")
        assert _advances(conn, tid)[-1] == ("review", "implement")
        # ... and the card can be re-reviewed: the second handoff still advances.
        assert kb.request_review(conn, tid, summary="v2")
        kbd.dispatch_once(conn, spawn_fn=fake)
        assert _advances(conn, tid)[-1] == ("implement", "review")


def test_lifecycle_contract_validation():
    assert validate_contract(_contract()).startswith("{")
    assert parse_lifecycle_contract(_contract())["stages"] == ["implement", "review", "sec", "merge"]
    assert parse_lifecycle_contract("local-only") is None
    assert parse_lifecycle_contract(PR_URL) is None
    for bad in (
        '{"lifecycle": "partial", "stages": ["implement"]}',
        '{"lifecycle": "full", "stages": ["review", "merge"]}',
        '{"lifecycle": "full", "stages": ["implement", "test"]}',
        '{"lifecycle": "full", "stages": ["implement", "implement"]}',
        '{"lifecycle": "full", "stages": ["implement"], "sec_required_if": "x"}',
    ):
        with pytest.raises(ValueError):
            validate_contract(bad)


def test_sec_required_glob_matching():
    required, hits = kbl.sec_required(
        [".github/workflows/ci-pr.yml", "src/app.ts"], [".github/workflows/**"],
    )
    assert required is True and hits == [".github/workflows/ci-pr.yml"]
    required, hits = kbl.sec_required(["docs/a.md"], [".github/workflows/**"])
    assert required is False and hits == []


def test_lifecycle_contract_without_a_pr_fails_closed_at_acceptance():
    """The exact-head gate is not loosened for a lifecycle card."""
    from hermes_cli.kanban_pr_acceptance import collect_acceptance

    receipt = collect_acceptance(json.dumps({"lifecycle": "full", "stages": ["implement"]}), None)
    assert receipt["ok"] is False and receipt["classification"] == "missing"
    # ... and a published PR that disagrees with the declared one is refused.
    other = "https://github.com/Cong-ty-TNHH-Ban-Gioi/bangioi/pull/2"
    receipt = collect_acceptance(_contract(), published_pr=other)
    assert receipt["ok"] is False and receipt["classification"] == "missing"
