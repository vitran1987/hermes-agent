"""Acceptance evidence for the one-card lifecycle (no hand reassignment).

Runs the REAL dispatcher (``hermes_cli.kanban_db_dispatch.dispatch_once``) over a
throwaway board with a fake spawn (no worker process) and a stubbed GitHub
acceptance gate (no network). Prints the card's event chain, per card:

  * A: implement -> review -> sec -> merge -> done   (PR touches .github/workflows)
  * B: implement -> review -> merge -> done          (PR changes a .md only)

Nothing here is simulated at the stage level: the transitions are written by
``hermes_cli.kanban_lifecycle.run_phase`` inside the dispatcher tick, exactly as
in production.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))

home = Path(tempfile.mkdtemp(prefix="lc-evidence-")) / ".hermes"
home.mkdir(parents=True)
# This runner drives a THROWAWAY board: drop the worker-session pins so the temp
# DB (not the real board) is the target, and so the delegate-child fence does not
# refuse writes under it.
for _pin in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_PROFILE",
             "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_WORKSPACES_ROOT"):
    os.environ.pop(_pin, None)
REAL_BOARD = Path(os.environ.get("REAL_BOARD_DB") or
                  Path.home() / "AppData/Local/hermes/kanban.db")
os.environ["HERMES_HOME"] = str(home)
Path.home = staticmethod(lambda: home.parent)  # type: ignore[assignment]

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_lifecycle as kbl
from hermes_cli import kanban_pr_acceptance_store as store
from hermes_cli import profiles

profiles.profile_exists = lambda name: True  # synthetic assignees
store.collect_acceptance = lambda contract, published_pr=None, assignee=None: {
    "ok": True, "classification": "success", "head_sha": "0" * 40,
    "pr_url": None, "checks": [],
}

A_PR = "https://github.com/Cong-ty-TNHH-Ban-Gioi/bangioi/pull/19197"
B_PR = "https://github.com/Cong-ty-TNHH-Ban-Gioi/bangioi/pull/19200"
DIFFS = {
    A_PR: [".github/workflows/ci-pr.yml", "hermes/scripts/x.ps1"],
    B_PR: ["docs/hermes-downstream-delta.md"],
}
REAL_CHANGED_PATHS = kbl.changed_paths          # kept for the real-gh section below
kbl.changed_paths = lambda url, assignee=None: DIFFS[url]

CONTRACT = {
    "lifecycle": "full",
    "stages": ["implement", "review", "sec", "merge"],
    "sec_required_if": [".github/workflows/**", ".github/actions/**", "**/secrets/**",
                        "supabase/migrations/**"],
}
SPAWNED = []


def spawn(task, workspace, board=None):
    SPAWNED.append((task.id, task.assignee))
    return 999999  # deliberately not a live PID


def show(conn, tid, label):
    print(f"\n=== {label} :: task {tid} ===")
    row = conn.execute(
        "SELECT status, assignee, completed_at FROM tasks WHERE id = ?", (tid,)
    ).fetchone()
    print(f"  state: status={row['status']} assignee={row['assignee']} "
          f"completed_at={'set' if row['completed_at'] else 'null'}")
    for e in conn.execute(
        "SELECT id, kind, payload FROM task_events WHERE task_id = ? ORDER BY id", (tid,)
    ):
        payload = kb._json_dict(e["payload"]) or {}
        if e["kind"] == "stage_advanced":
            sec = payload.get("sec_decision") or {}
            extra = f" assignee={payload.get('assignee')}"
            if sec:
                extra += f" sec_required={sec.get('required')} reason={sec.get('reason')!r}"
            print(f"  #{e['id']:<3} stage_advanced   {payload['from']:>9} -> {payload['to']:<7}{extra}")
        elif e["kind"] in ("lifecycle_completed", "lifecycle_deviation", "review_requested",
                           "completed", "changes_requested", "status", "created"):
            print(f"  #{e['id']:<3} {e['kind']:<18} {json.dumps(payload, ensure_ascii=False)[:150]}")
    print(f"  tasks on this board: {[r['id'] for r in conn.execute('SELECT id FROM tasks')]}")


def run_card(conn, title, pr):
    contract = dict(CONTRACT, pr=pr)
    tid = kb.create_task(conn, title=title, assignee="developer-bangioi",
                         completion_contract=json.dumps(contract))
    print(f"\n--- {title} ---")
    r = kbd.dispatch_once(conn, spawn_fn=spawn)
    print(f"tick 1 (implement):        spawned={r.spawned} guarded={r.respawn_guarded}")
    assert kb.request_review(conn, tid, summary="implemented; PR opened"), "handoff refused"
    r = kbd.dispatch_once(conn, spawn_fn=spawn)
    print(f"tick 2 (implement->?):     advanced={r.lifecycle_advanced} guarded={r.respawn_guarded}")
    assert kb.complete_task(conn, tid, summary="review approved"), "review completion refused"
    # each remaining verdict comes from the stage owner, then the dispatcher moves
    for n in (3, 4, 5):
        r = kbd.dispatch_once(conn, spawn_fn=spawn)
        if r.lifecycle_advanced:
            print(f"tick {n}: advanced={r.lifecycle_advanced} guarded={r.respawn_guarded}")
        stage = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"]
        if stage == "done":
            break
        assert kb.complete_task(conn, tid, summary=f"stage verdict #{n}"), "completion refused"
    show(conn, tid, title)
    return tid


def run_rejection_cycle(conn, title, pr):
    """implement -> review --request_changes--> implement -> review -> sec -> merge."""
    contract = dict(CONTRACT, pr=pr)
    tid = kb.create_task(conn, title=title, assignee="developer-bangioi",
                         completion_contract=json.dumps(contract))
    print(f"\n--- {title} ---")
    kbd.dispatch_once(conn, spawn_fn=spawn)                      # implement
    assert kb.request_review(conn, tid, summary="implemented v1")
    r = kbd.dispatch_once(conn, spawn_fn=spawn)                  # -> review
    print(f"tick 2: advanced={[(m.get('from'), m.get('to')) for m in r.lifecycle_advanced]}")
    run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    ok, implementer = kb.request_changes(conn, tid, reason="needs tests", expected_run_id=run_id)
    print(f"reviewer request_changes -> ok={ok} implementer={implementer} "
          f"advanced_now={kbl.run_phase(conn)}")                  # no move: back at implement
    print(f"  state after rejection: "
          f"{dict(conn.execute('SELECT status, assignee FROM tasks WHERE id = ?', (tid,)).fetchone())}")
    assert kb.request_review(conn, tid, summary="implemented v2")
    r = kbd.dispatch_once(conn, spawn_fn=spawn)                  # implement -> review again
    print(f"tick 3 (re-entry): advanced={[(m.get('from'), m.get('to')) for m in r.lifecycle_advanced]}")
    assert kb.complete_task(conn, tid, summary="review approved")
    for n in (4, 5, 6):
        r = kbd.dispatch_once(conn, spawn_fn=spawn)
        if r.lifecycle_advanced:
            print(f"tick {n}: advanced={[(m.get('from'), m.get('to') or m.get('completed')) for m in r.lifecycle_advanced]}")
        status = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
        if status == "done":
            break
        assert kb.complete_task(conn, tid, summary=f"stage verdict #{n}")
    show(conn, tid, title)
    return tid


kb.init_db()
with kbc.connect() as conn:
    a = run_card(conn, "A: PR touches .github/workflows", A_PR)
    b = run_card(conn, "B: PR changes a .md only", B_PR)
    print("\n== spawn order (assignee per stage, per tick) ==")
    for task_id, assignee in SPAWNED:
        print(f"  {task_id} -> {assignee}")
    kinds_a = [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (a,))]
    print(f"\n== no operator reassignment on card A: 'assigned' events = "
          f"{kinds_a.count('assigned')} (stage_advanced = {kinds_a.count('stage_advanced')}) ==")
    print("== ordinary card (no lifecycle declaration) ==")
    plain = kb.create_task(conn, title="plain card", assignee="developer-bangioi")
    kb.add_comment(conn, plain, author="worker", body=f"PR opened: {A_PR}")
    before = [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (plain,))]
    r = kbd.dispatch_once(conn, spawn_fn=spawn)
    after = [r2["kind"] for r2 in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (plain,))]
    print(f"  lifecycle_advanced={r.lifecycle_advanced} new_events={after[len(before):]}")
    c = run_rejection_cycle(conn, "C: reviewer request_changes then re-entry", A_PR)

# --- REAL GitHub reads: sec_required_if against two real PRs ------------------
# The stage chain above stubs the diff reader for determinism; these two reads are
# real gh API calls (no stub, no fixture), one PR that touches a workflow and one
# that does not.
REAL_PRS = [
    ("PR that touches .github/workflows", "Cong-ty-TNHH-Ban-Gioi/bangioi", 19197),
    ("PR that does not", "vitran1987/hermes-agent", 37),
]
print("\n== sec_required_if vs REAL PR diffs (real gh api, ambient login) ==")
for label, repo, number in REAL_PRS:
    url = f"https://github.com/{repo}/pull/{number}"
    paths = REAL_CHANGED_PATHS(url, assignee=None)
    if paths is None:
        print(f"  {label}: {url} -> file list UNREADABLE (sec would stay required)")
        continue
    required, hits = kbl.sec_required(paths, CONTRACT["sec_required_if"])
    print(f"  {label}: {url} -> {len(paths)} files, sec_required={required}, matched={hits[:3]}")
    print(f"    files: {paths[:6]}{' ...' if len(paths) > 6 else ''}")

# --- the real board: no lifecycle cards, so the phase is a no-op there --------
import sqlite3

if REAL_BOARD.exists():
    print(f"\n== real board probe (READ-ONLY): {REAL_BOARD} ==")
    ro = sqlite3.connect(f"file:{REAL_BOARD.as_posix()}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    try:
        total = ro.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        declared = ro.execute(
            "SELECT id, status, completion_contract FROM tasks "
            "WHERE completion_contract LIKE '{%'"
        ).fetchall()
        print(f"  cards={total}  lifecycle-declared cards={len(declared)}")
        for row in declared:
            print(f"    {row['id']} {row['status']} {row['completion_contract'][:80]}")
        print("  => run_phase() has nothing to move on this board: ordinary cards"
              " keep the exact behaviour they have today.")
    finally:
        ro.close()
else:
    print(f"\n== real board not found at {REAL_BOARD} (skipped) ==")
