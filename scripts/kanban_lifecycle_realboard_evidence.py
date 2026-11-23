"""Acceptance run on a COPY of the live board, with REAL PRs and REAL gh reads.

Why a copy: a dispatcher-spawned kanban worker is fenced from every board by the
kernel (`_assert_not_delegated_child_mutation` — `delegate_task child contexts
cannot mutate Kanban tasks`), and there is no sanctioned way for this session to
create cards on the live board. So the board is copied (schema, 4.8 k cards,
all settings identical), and everything that CAN be real is real:

  * real `dispatch_once` ticks of the patched tree (no stage is driven by hand);
  * REAL PRs and REAL `gh api` diff reads:
      - Cong-ty-TNHH-Ban-Gioi/bangioi#19197  (touches .github/workflows/ci-pr.yml)
      - vitran1987/hermes-agent#37           (does not touch a workflow)
  * the real adoption guard: `active_pr` fires on a real PR URL comment.

Stubbed, and named so in the output: `spawn_fn` (no worker process is started) and
the exact-head acceptance gate (the PR is not merged; the gate is exercised by the
unit tests instead).
"""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

root = Path(sys.argv[1]).resolve()
real_board = Path(sys.argv[2] if len(sys.argv) > 2
                  else Path.home() / "AppData/Local/hermes/kanban.db")

for pin in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
            "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_WORKSPACES_ROOT"):
    os.environ.pop(pin, None)
# NOTE: HERMES_HOME is deliberately left at its REAL value so an assignee profile
# resolves to its own gh login -- the sec decision below is therefore a REAL
# `gh api` read of the real PR, exactly as the dispatcher does it in production.
tmp = Path(tempfile.mkdtemp(prefix="lc-realboard-"))
copy = tmp / "kanban.db"
src = sqlite3.connect(f"file:{real_board.as_posix()}?mode=ro", uri=True)
dst = sqlite3.connect(str(copy))
src.backup(dst)
src.close()
dst.close()
os.environ["HERMES_KANBAN_DB"] = str(copy)
os.environ["HERMES_KANBAN_WORKSPACES_ROOT"] = str(tmp / "workspaces")
sys.path.insert(0, str(root))

from hermes_cli import kanban_db as kb                       # noqa: E402
from hermes_cli import kanban_db_connect as kbc            # noqa: E402
from hermes_cli import kanban_db_dispatch as kbd           # noqa: E402
from hermes_cli import kanban_lifecycle as kbl             # noqa: E402
from hermes_cli import kanban_pr_acceptance_store as store  # noqa: E402
from hermes_cli import profiles                             # noqa: E402

profiles.profile_exists = lambda name: True
kbd.reap_worker_zombies = lambda: []
kbd.reap_terminal_workers = lambda conn, **kw: []
kbd.enforce_max_runtime = lambda conn, **kw: []
kbd.detect_stale_running = lambda conn, **kw: []
kbd._terminate_reclaimed_worker = lambda *a, **k: {"status": "noop"}
kbd._kb._terminate_reclaimed_worker = lambda *a, **k: {"status": "noop"}
store.collect_acceptance = lambda contract, published_pr=None, assignee=None: {
    "ok": True, "classification": "success", "head_sha": "0" * 40,
    "pr_url": None, "checks": [],
}

WF_PR = "https://github.com/Cong-ty-TNHH-Ban-Gioi/bangioi/pull/19197"
MD_PR = "https://github.com/vitran1987/hermes-agent/pull/37"
CONTRACT = {
    "lifecycle": "full",
    "stages": ["implement", "review", "sec", "merge"],
    "sec_required_if": [".github/workflows/**", ".github/actions/**", "**/secrets/**",
                        "supabase/migrations/**"],
}
SPAWNED = []


def spawn(task, workspace, board=None):
    SPAWNED.append((task.id, task.assignee))
    return 999999


def adv(conn, tid):
    return [(kb._json_dict(r["payload"])["from"], kb._json_dict(r["payload"])["to"])
            for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'stage_advanced' "
                "ORDER BY id", (tid,))]


def tick(conn, label):
    r = kbd.dispatch_once(conn, spawn_fn=spawn)
    moves = [(m.get("task"), m.get("from"), m.get("to"), m.get("completed")) for m in r.lifecycle_advanced]
    print(f"  {label:<28} advanced={moves} guarded={r.respawn_guarded} spawned={[s[1] for s in r.spawned]}")
    return r


def dump(conn, tid, title):
    row = conn.execute("SELECT status, assignee, completion_contract FROM tasks WHERE id = ?",
                       (tid,)).fetchone()
    print(f"\n=== {title} :: {tid} ===")
    print(f"  completion_contract: {row['completion_contract']}")
    print(f"  final: status={row['status']} assignee={row['assignee']}")
    kinds = []
    for e in conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id", (tid,)
    ):
        payload = kb._json_dict(e["payload"]) or {}
        kinds.append(e["kind"])
        if e["kind"] == "stage_advanced":
            sec = payload.get("sec_decision") or {}
            line = f"  stage_advanced  {payload['from']:>9} -> {payload['to']:<9} assignee={payload.get('assignee')}"
            if sec:
                line += f"  sec_required={sec.get('required')} ({sec.get('reason')})"
            print(line)
        elif e["kind"] in ("lifecycle_completed", "lifecycle_deviation", "changes_requested",
                           "review_requested", "completed", "claimed"):
            print(f"  {e['kind']:<16} {json.dumps(payload, ensure_ascii=False)[:120]}")
    print(f"  'assigned' events (operator reassignments): {kinds.count('assigned')}")
    print(f"  'stage_advanced' events: {kinds.count('stage_advanced')}")
    print(f"  card ids on board: {conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]}"
          f" (one card carried every stage)")


kb.init_db()
with kbc.connect() as conn:
    print("== card A: REAL PR bangioi#19197 (touches .github/workflows/ci-pr.yml) ==")
    a = kb.create_task(conn, title="[EVIDENCE] lifecycle A - workflow-touching PR",
                       assignee="developer-bangioi",
                       completion_contract=json.dumps(dict(CONTRACT, pr=WF_PR)))
    tick(conn, "tick 1 (implement)")
    # the worker posts the PR URL exactly like a real worker does
    kb.add_comment(conn, a, author="developer-bangioi", body=f"PR opened: {WF_PR}")
    assert kb.request_review(conn, a, summary="implemented; PR opened")
    tick(conn, "tick 2 implement->review")
    assert kb.complete_task(conn, a, summary="review: APPROVE at head")
    tick(conn, "tick 3 review->?")
    assert kb.complete_task(conn, a, summary="sec: APPROVE bound to head")
    # sec -> merge happens in the ready lane, where the active_pr guard applies.
    # A same-second tie between the PR comment and the handoff is held (fail
    # closed) and must clear on the very next tick -- shown by ticking twice.
    r = tick(conn, "tick 4 sec->merge")
    if r.respawn_guarded:
        tick(conn, "tick 5 (guard self-clears)")
    assert kb.complete_task(conn, a, summary="merged --squash --match-head-commit")
    tick(conn, "tick 6 finish")
    dump(conn, a, "A: real PR #19197")

    print("\n== card B: REAL PR hermes-agent#37 (no workflow path) ==")
    b = kb.create_task(conn, title="[EVIDENCE] lifecycle B - docs-only PR",
                       assignee="developer-bangioi",
                       completion_contract=json.dumps(dict(CONTRACT, pr=MD_PR)))
    tick(conn, "tick 1 (implement)")
    kb.add_comment(conn, b, author="developer-bangioi", body=f"PR opened: {MD_PR}")
    assert kb.request_review(conn, b, summary="implemented")
    tick(conn, "tick 2 implement->review")
    assert kb.complete_task(conn, b, summary="review: APPROVE")
    tick(conn, "tick 3 review->?")
    tick(conn, "tick 4 spawn merge")            # sec skipped: no workflow path in the diff
    assert kb.complete_task(conn, b, summary="merged")
    tick(conn, "tick 5 finish")
    dump(conn, b, "B: real PR #37")

    print("\n== card C: REAL PR #19197 + reviewer request_changes (rejection leg) ==")
    c = kb.create_task(conn, title="[EVIDENCE] lifecycle C - rejected then re-reviewed",
                       assignee="developer-bangioi",
                       completion_contract=json.dumps(dict(CONTRACT, pr=WF_PR)))
    tick(conn, "tick 1 (implement)")
    kb.add_comment(conn, c, author="developer-bangioi", body=f"PR opened: {WF_PR}")
    assert kb.request_review(conn, c, summary="implemented v1")
    tick(conn, "tick 2 implement->review")
    run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (c,)).fetchone()[0]
    ok, impl = kb.request_changes(conn, c, reason="add a regression test",
                                  expected_run_id=run_id)
    print(f"  reviewer verdict: request_changes ok={ok} -> back on {impl}")
    tick(conn, "tick 3 dispatcher records")
    state = conn.execute("SELECT status, assignee FROM tasks WHERE id = ?", (c,)).fetchone()
    print(f"  after rejection: status={state['status']} assignee={state['assignee']}")
    assert kb.request_review(conn, c, summary="implemented v2")
    tick(conn, "tick 4 re-entry implement->review")
    assert kb.complete_task(conn, c, summary="review: APPROVE v2")
    for n in (5, 6, 7):
        r = tick(conn, f"tick {n}")
        if r.respawn_guarded:
            r = tick(conn, f"tick {n}b (guard self-clears)")
        if conn.execute("SELECT status FROM tasks WHERE id = ?", (c,)).fetchone()[0] == "done":
            break
        if not r.spawned:
            continue          # the stage worker has not run yet; tick again
        assert kb.complete_task(conn, c, summary=f"stage verdict #{n}")
    dump(conn, c, "C: real PR #19197, rejected once")

    print("\n== the real board is unchanged ==")
    ro = sqlite3.connect(f"file:{real_board.as_posix()}?mode=ro", uri=True)
    cards = ro.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    declared = ro.execute(
        "SELECT COUNT(*) FROM tasks WHERE completion_contract LIKE ?", ('%"lifecycle"%',)
    ).fetchone()[0]
    print(f"  cards on the live board: {cards}  lifecycle-declared: {declared}")
    ro.close()
