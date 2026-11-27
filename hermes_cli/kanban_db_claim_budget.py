"""The breaker budget a card was DISPATCHED under, read back from the board.

``kanban.failure_limit`` is resolved by the dispatcher from ITS OWN home
(``gateway/kanban_watchers_dispatcher._resolve_dispatcher_settings``) once at
boot. A worker runs inside the assignee's profile home, which is a separate
island: on this host the dispatching ``pm-bangioi`` home resolves 4 while
``developer-bangioi`` resolves the built-in 2 (no ``failure_limit`` key), so a
config read inside the worker re-derives a number the dispatcher never used.

The budget therefore travels on the board: ``_dispatch_lane_task`` stamps the
resolved ``failure_limit`` on the ``claimed`` event, and a worker that books a
terminal outcome reads it back here and forwards it to
``kanban_db_dispatch._record_task_failure`` instead of letting the breaker
substitute ``DEFAULT_FAILURE_LIMIT``.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from hermes_cli.kanban_db import _json_dict

__all__ = ["dispatched_failure_limit"]


def dispatched_failure_limit(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    """``kanban.failure_limit`` stamped on the card's NEWEST claim, else ``None``.

    ``None`` means "this claim carries no dispatcher budget" — a claim taken
    outside the dispatcher (control-plane lanes pull via ``claim_task``), or a
    stamp that is not a usable positive integer. The caller must then let the
    breaker use its own default rather than invent a widening.

    Read-only, and deliberately conservative: only a JSON integer >= 1 counts,
    so ``""``, ``"banana"``, ``0``, ``-3`` and booleans all fall back instead of
    widening the breaker.

    Only the newest ``claimed`` event is consulted: a re-claim opens a new run
    with its own event, so an old stamp can never leak onto a later attempt.
    """
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'claimed' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    raw = _json_dict(row["payload"]).get("failure_limit")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return None
    return int(raw)
