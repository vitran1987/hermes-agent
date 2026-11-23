"""One-card lifecycle: the dispatcher walks a declared stage chain on ONE card.

A card that declares its stages once (``completion_contract`` as a JSON object,
:func:`hermes_cli.kanban_pr_acceptance.parse_lifecycle_contract`) is moved by the
dispatcher, stage after stage, without any operator reassignment::

    todo -> implement -> review -> [sec] -> merge -> done

Every transition is a ``stage_advanced`` event on the SAME card. No child card is
created per stage, and no ``assigned`` event is written by hand -- that is what
makes a stage handoff legible to the respawn guard
(``kanban_db_dispatch._is_handoff_event`` counts ``stage_advanced`` as a
dispatcher-produced handoff, so ``active_pr`` is lifted for the profile that must
act on the PR next).

Stage -> lane:

* ``implement`` -- the card's own assignee, ``ready`` lane (the starting point).
* ``review``, ``sec`` -- the ``review`` lane: a verdict stop, not an
  implementation. A reviewer approves with ``kanban_complete`` and rejects with
  ``kanban_request_changes`` (which lands the card back on the implementer).
* ``merge`` -- the ``ready`` lane: the profile merges the PR, then completes; the
  declared ``pr`` makes that completion run the exact-head acceptance gate.

A stage ends when the card's state proves it: ``review`` status with a
``review_requested`` event for the implement stage, ``done`` status with a
``completed`` event for every later stage. The dispatcher only re-opens
(``done`` -> the next stage's lane) after such proof, so a card that was merely
already ``done`` (or a stale ``done`` from an earlier cycle) is never resurrected.

Deviations are reported, never guessed: an implement stage that completes
without a review handoff, or a stage with no resolvable assignee, emits
``lifecycle_deviation`` and leaves the card where it is for a human.
"""
from __future__ import annotations

import fnmatch
import re
from typing import Any, Iterable, Optional

from hermes_cli import kanban_db as _kb

_LIFECYCLE_QUERY = (
    "SELECT id, status, assignee, completed_at, completion_contract FROM tasks "
    "WHERE status IN ('review', 'done') "
    "AND completion_contract LIKE '%\"lifecycle\"%'"
)

#: Event kinds that close a stage boundary. ``changes_requested`` is included
#: because a reviewer's rejection resets the card to ``implement`` -- that is a
#: stage move the lifecycle must follow, not one it must ignore.
BOUNDARY_KINDS = ("stage_advanced", "lifecycle_completed", "changes_requested")

#: Review-lane stages: a stop for a verdict. Everything else is a work stage.
REVIEW_LANE_STAGES = ("review", "sec")

#: Assignee per stage, used when the contract or ``kanban.lifecycle_assignees``
#: names none. ``implement`` is always the card's own assignee.
DEFAULT_STAGE_ASSIGNEES = {
    "review": "code-reviewer-bangioi",
    "sec": "security-reviewer-bangioi",
    "merge": "code-reviewer-bangioi",
}

#: Default ``sec_required_if`` when a contract declares the ``sec`` stage but no
#: patterns: anything that changes a workflow, an action, a secret or a migration
#: needs a security verdict bound to the PR head.
DEFAULT_SEC_REQUIRED_IF = (
    ".github/workflows/**",
    ".github/actions/**",
    "**/secrets/**",
    "supabase/migrations/**",
)

_PR_URL = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)"
)


def lifecycle_spec(contract: Optional[str]) -> Optional[dict]:
    """The lifecycle block of a card contract, or ``None`` for an ordinary card.

    Never raises: a contract that cannot be parsed cannot drive a lifecycle, and
    a dispatcher tick must not die on one bad row.
    """
    if not contract or not contract.lstrip().startswith("{"):
        return None
    try:
        from hermes_cli.kanban_pr_acceptance import parse_lifecycle_contract
        return parse_lifecycle_contract(contract)
    except Exception as exc:  # ValueError on junk contract, ImportError on partial installs
        _kb._log.warning("kanban lifecycle: unusable contract ignored (%s)", exc)
        return None


def configured_stage_assignees() -> dict[str, str]:
    """``kanban.lifecycle_assignees`` -- board-level stage -> profile map.

    Accepts a mapping or the ``"review=alice,merge=bob"`` string form. Anything
    unreadable yields ``{}`` so the built-in defaults still apply.
    """
    try:
        from hermes_cli.config import load_config
        value = ((load_config() or {}).get("kanban") or {}).get("lifecycle_assignees")
    except Exception:
        return {}
    if isinstance(value, dict):
        return {
            str(k).strip(): str(v).strip()
            for k, v in value.items()
            if isinstance(v, str) and v.strip()
        }
    if isinstance(value, str):
        pairs: dict[str, str] = {}
        for chunk in value.split(","):
            stage, _, profile = chunk.partition("=")
            if stage.strip() and profile.strip():
                pairs[stage.strip()] = profile.strip()
        return pairs
    return {}


def stage_assignee(
    spec: dict, stage: str, *, implementer: Optional[str] = None,
    configured: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """Profile that owns ``stage``: contract override, board config, default."""
    if stage == "implement":
        return implementer
    declared = (spec.get("assignees") or {}).get(stage)
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    if configured is None:
        configured = configured_stage_assignees()
    chosen = configured.get(stage) or DEFAULT_STAGE_ASSIGNEES.get(stage)
    return (chosen or "").strip() or None


def sec_required(changed_paths: Iterable[str], patterns: Iterable[str]) -> tuple[bool, list[str]]:
    """``(required, matched_paths)`` for the PR's changed files against globs.

    Shell globs, matched case-sensitively against the repo-relative POSIX path.
    A pattern's trailing ``/**`` also matches the directory itself, so
    ``.github/workflows/**`` covers a rename of the directory entry too.
    """
    globs = [str(p).replace("\\", "/").strip() for p in patterns if str(p).strip()]
    matched: list[str] = []
    for raw in changed_paths:
        path = str(raw).replace("\\", "/")
        if path.startswith("./"):
            path = path[2:]
        for glob in globs:
            if fnmatch.fnmatchcase(path, glob) or (
                glob.endswith("/**") and fnmatch.fnmatchcase(path, glob[:-3])
            ):
                matched.append(path)
                break
    return bool(matched), matched


def changed_paths(pr_url: Optional[str], *, assignee: Optional[str] = None) -> Optional[list[str]]:
    """Files the PR currently changes, or ``None`` when that cannot be proven.

    Read through the assignee profile's own ``gh`` login (the identity that owns
    the repo, exactly as the acceptance gate does). ``None`` means "unreadable",
    which the caller must treat as ``sec required`` -- never as "clean diff".
    """
    match = _PR_URL.fullmatch(pr_url or "")
    if not match:
        return None
    repo, number = match[1], int(match[2])
    try:
        from hermes_cli.kanban_pr_acceptance import _api, _assignee_profile_home
        profile_home = _assignee_profile_home(assignee)
        pages = _api(
            f"repos/{repo}/pulls/{number}/files?per_page=100",
            paginate=True, profile_home=profile_home,
        )
    except Exception as exc:
        # A 401/403/404, a missing gh or a network blip all land here: the
        # lifecycle records the reason and keeps the security stop in place.
        _kb._log.warning(
            "kanban lifecycle: PR file list for %s unreadable (%s: %s) -- sec stays required",
            pr_url, type(exc).__name__, exc,
        )
        return None
    paths = [str(f.get("filename") or "") for page in pages for f in page]
    paths = [p for p in paths if p]
    return paths or None


def _latest_pr_url(conn, task_id: str) -> Optional[str]:
    """Newest PR URL a worker posted on the card (bounded: the last 40 comments)."""
    for row in conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id DESC LIMIT 40",
        (task_id,),
    ):
        body = _kb._lossy_text(row["body"]) or ""
        found = _PR_URL.search(body)
        if found:
            return found.group(0)
    return None


def sec_decision(
    conn, row, spec: dict, *, changed_paths_fn=None,
) -> dict:
    """``{"required": bool, "reason": str, ...}`` -- the security-stop decision.

    Fail-closed at every unproven step: no ``sec_required_if`` is a *declared*
    opt-out (recorded with its reason), but a declared gate whose diff cannot be
    read keeps ``sec`` in the chain.
    """
    patterns = spec.get("sec_required_if")
    if patterns is None:
        # A declared ``sec`` stage without its own patterns keeps the built-in
        # sensitive-path set: an omitted gate is not an opt-out.
        patterns = list(DEFAULT_SEC_REQUIRED_IF)
    patterns = [str(p) for p in patterns if str(p).strip()]
    if not patterns:
        return {"required": True, "reason": "sec_required_if is empty -- fail closed"}
    pr_url = spec.get("pr") or _latest_pr_url(conn, row["id"])
    if not pr_url:
        return {
            "required": True,
            "reason": "no PR URL on the card; the diff cannot be proven non-sensitive",
            "pr": None,
        }
    reader = changed_paths_fn or changed_paths
    paths = reader(pr_url, assignee=row["assignee"])
    if paths is None:
        return {"required": True, "reason": "PR file list unreadable -- fail closed", "pr": pr_url}
    matched, hits = sec_required(paths, patterns)
    decision = {
        "required": matched,
        "pr": pr_url,
        "changed": len(paths),
        "patterns": patterns,
    }
    if matched:
        decision["matched"] = hits[:10]
        decision["reason"] = f"{len(hits)} changed path(s) match sec_required_if"
    else:
        decision["reason"] = f"none of {len(paths)} changed path(s) match sec_required_if"
    return decision


def _boundary_event(conn, task_id: str):
    """Newest stage boundary for the card (``None`` before the first advance)."""
    placeholders = ", ".join("?" for _ in BOUNDARY_KINDS)
    return conn.execute(
        f"SELECT id, kind, payload FROM task_events WHERE task_id = ? "
        f"AND kind IN ({placeholders}) ORDER BY id DESC LIMIT 1",
        (task_id, *BOUNDARY_KINDS),
    ).fetchone()


def _event_after(conn, task_id: str, kind: str, after_id: int):
    return conn.execute(
        "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = ? AND id > ? "
        "ORDER BY id LIMIT 1",
        (task_id, kind, int(after_id)),
    ).fetchone()


def current_stage(boundary, stages: list[str]) -> tuple[str, bool]:
    """``(stage, completed)`` derived from the newest boundary event."""
    if boundary is None:
        return stages[0], False
    kind = boundary["kind"]
    payload = _kb._json_dict(boundary["payload"])
    if kind == "lifecycle_completed":
        return stages[-1], True
    if kind == "changes_requested":
        # A rejection hands the card back to the implementer: same card, stage 1.
        return ("implement" if "implement" in stages else stages[0]), False
    to = payload.get("to")
    if to == "done":
        return stages[-1], True
    if isinstance(to, str) and to in stages:
        return to, False
    return stages[0], False


def _resolve_next(conn, row, spec: dict, stage: str, *, changed_paths_fn=None):
    """``(next_stage | None, sec_decision | None)`` after ``stage``."""
    stages = spec["stages"]
    try:
        start = stages.index(stage) + 1
    except ValueError:
        return None, None
    skipped: Optional[dict] = None
    for candidate in stages[start:]:
        if candidate == "sec":
            decision = sec_decision(conn, row, spec, changed_paths_fn=changed_paths_fn)
            if not decision["required"]:
                skipped = decision
                continue
            return candidate, decision
        return candidate, skipped
    return None, skipped


def plan_advance(
    conn, row, spec: dict, *, changed_paths_fn=None, configured: Optional[dict] = None,
) -> Optional[dict]:
    """What (if anything) this card's lifecycle should do on this tick.

    Pure read: no writes. Returns ``{"from", "to", ...}`` for a stage move,
    ``{"completed": True, ...}`` for a finished chain, or ``{"deviation": ...}``.
    """
    stages = spec["stages"]
    boundary = _boundary_event(conn, row["id"])
    stage, completed = current_stage(boundary, stages)
    if completed:
        return None
    after = int(boundary["id"]) if boundary is not None else 0
    status = row["status"]

    if status == "review" and stage == "implement":
        handoff = _event_after(conn, row["id"], "review_requested", after)
        if handoff is None:
            return None
        # Next stage straight after implement: the review verdict. No diff read
        # here -- the sec gate is decided when the REVIEW stage ends, so a tick
        # never pays for a gh call it cannot use.
        nxt = stages[1] if len(stages) > 1 else None
        payload = _kb._json_dict(handoff["payload"])
        return {
            "from": stage,
            "to": nxt,
            "implementer": payload.get("implementer") or row["assignee"],
            "reason": "review request",
            "sec_decision": None,
        }

    if status != "done":
        return None

    if _event_after(conn, row["id"], "completed", after) is None:
        # ``done`` without a completion after this boundary: not this stage's end.
        return None

    if stage == "implement":
        # The implementer closed the card instead of handing it to review; a
        # lifecycle card cannot skip its verdict, and only a human can decide
        # whether that was intended.
        return {
            "deviation": "stage 'implement' completed without kanban_request_review",
            "stage": stage,
            "expected": stages[1] if len(stages) > 1 else None,
        }

    nxt, decision = _resolve_next(conn, row, spec, stage, changed_paths_fn=changed_paths_fn)
    if nxt is None:
        return {"completed": True, "from": stage}
    implementer = row["assignee"]
    return {
        "from": stage,
        "to": nxt,
        "implementer": implementer,
        "reason": f"stage '{stage}' completed",
        "sec_decision": decision,
    }


def _last_event(conn, task_id: str):
    return conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()


def _emit_once(conn, task_id: str, kind: str, payload: dict) -> bool:
    """Append ``kind`` unless it already is the card's newest event with this payload."""
    last = _last_event(conn, task_id)
    if last is not None and last["kind"] == kind:
        if _kb._json_dict(last["payload"]) == payload:
            return False
    _kb._append_event(conn, task_id, kind, payload)
    return True


def apply_advance(conn, row, spec: dict, plan: dict, *, configured: Optional[dict] = None) -> Optional[dict]:
    """Write one planned transition (or its deviation/finish record).

    ``None`` when the record would duplicate the card's newest event.
    """
    task_id = row["id"]
    stages = spec["stages"]

    if "deviation" in plan:
        payload = {"stage": plan["stage"], "reason": plan["deviation"],
                   "expected": plan.get("expected"), "action": "human"}
        with _kb.write_txn(conn):
            emitted = _emit_once(conn, task_id, "lifecycle_deviation", payload)
        return {"task": task_id, "deviation": plan["deviation"], "emitted": emitted} if emitted else None

    if plan.get("completed"):
        payload = {"stage": plan["from"], "stages": list(stages), "status": "done"}
        with _kb.write_txn(conn):
            emitted = _emit_once(conn, task_id, "lifecycle_completed", payload)
        return {"task": task_id, "completed": True, "emitted": emitted} if emitted else None

    target = plan["to"]
    if not target:
        return {"task": task_id, "skipped": "no next stage"}
    assignee = stage_assignee(spec, target, implementer=plan.get("implementer"), configured=configured)
    if not assignee:
        payload = {"stage": plan["from"], "target": target,
                   "reason": f"no assignee resolved for stage '{target}'",
                   "action": "human"}
        with _kb.write_txn(conn):
            emitted = _emit_once(conn, task_id, "lifecycle_deviation", payload)
        return {"task": task_id, "deviation": payload["reason"], "emitted": emitted}

    reopened = row["status"] == "done"
    lane = "review" if target in REVIEW_LANE_STAGES else "ready"
    if reopened:
        new_status = "review" if lane == "review" else _kb._landing_status_after_parents(conn, task_id)
    else:
        new_status = "review"  # the card already sits in the review lane (review_requested)
    payload = {
        "from": plan["from"],
        "to": target,
        "stage_index": stages.index(target) + 1,
        "stages": list(stages),
        "assignee": assignee,
        "lane": lane,
        "reason": plan.get("reason") or "stage end",
        "source": "dispatcher",
    }
    if plan.get("implementer"):
        payload["implementer"] = plan["implementer"]
    if plan.get("sec_decision"):
        payload["sec_decision"] = plan["sec_decision"]

    with _kb.write_txn(conn):
        current = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if current is None or current["status"] != row["status"]:
            return {"task": task_id, "skipped": "status changed during advance"}
        if reopened:
            conn.execute(
                "UPDATE tasks SET status = ?, assignee = ?, completed_at = NULL, "
                "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
                "worker_started_at = NULL WHERE id = ? AND status = 'done'",
                (new_status, assignee, task_id),
            )
        else:
            # Already in the review lane: only the owner changes.
            conn.execute(
                "UPDATE tasks SET assignee = ?, claim_lock = NULL, claim_expires = NULL, "
                "worker_pid = NULL, worker_started_at = NULL "
                "WHERE id = ? AND status = 'review'",
                (assignee, task_id),
            )
        _kb._append_event(conn, task_id, "stage_advanced", payload)
        if reopened:
            # The respawn guard's ``recent_success`` hold is lifted by a re-queue
            # event after the completed run; the legacy 'status' event is that
            # signal (and what the live feed renders).
            _kb._append_event(
                conn, task_id, "status",
                {"status": new_status, "reason": "lifecycle_stage_advanced",
                 "previous_status": "done"},
            )
    return {"task": task_id, "from": payload["from"], "to": target,
            "assignee": assignee, "status": new_status}


def run_phase(
    conn, *, dry_run: bool = False, changed_paths_fn=None,
) -> list[dict]:
    """Advance every lifecycle card whose current stage has provably ended.

    Called by the dispatcher tick after the reclaim/promote phase so a card that
    moved on this tick can be claimed in the same tick. ``dry_run`` reports the
    planned moves without writing anything.
    """
    try:
        rows = conn.execute(_LIFECYCLE_QUERY).fetchall()
    except Exception as exc:  # a pre-migration board without the column set
        _kb._log.warning("kanban lifecycle: phase skipped (%s)", exc)
        return []
    if not rows:
        return []
    configured = configured_stage_assignees()
    advanced: list[dict] = []
    for row in rows:
        spec = lifecycle_spec(row["completion_contract"])
        if not spec:
            continue
        plan = plan_advance(conn, row, spec, changed_paths_fn=changed_paths_fn, configured=configured)
        if not plan:
            continue
        if dry_run:
            advanced.append({"task": row["id"], **plan, "dry_run": True})
            continue
        entry = apply_advance(conn, row, spec, plan, configured=configured)
        if entry:
            advanced.append(entry)
    return advanced
