"""Rule 4 (``active_pr``) must not hold the card that OWNS the PR it cites.

Measured 2026-11-27: 113 of 153 ``respawn_guarded{reason: active_pr}`` denials in
24 h (74%) were the owning card. ``t_378d2f92`` (#19355) was denied 55 times,
sat 21.6 h, and its PR went CONFLICTING — which minted a rebase card and re-ran
CI. Ownership is therefore read from the fields that RECORD it
(``completion_contract`` / ``metadata.published_pr`` / ``idempotency_key`` /
``#<n>`` in the title), never from comment prose.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

PR_URL = "https://github.com/vitran1987/hermes-agent/pull/{n}"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _card(conn, *, title="card", contract=None, metadata=None, key=None, pr_numbers=()):
    """Create a ready card and let a worker comment the PR URL(s) it opened."""
    tid = kb.create_task(conn, title=title, assignee="a",
                         completion_contract=contract, idempotency_key=key)
    if metadata is not None:
        # ``published_pr`` lands on the completion handoff, not on ``tasks``.
        claim = kb.claim_task(conn, tid)
        assert claim is not None
        conn.execute("UPDATE task_runs SET metadata=? WHERE id=?",
                     (json.dumps(metadata), claim.current_run_id))
    for n in pr_numbers:
        kb.add_comment(conn, tid, "worker", "opened %s" % PR_URL.format(n=n))
    conn.commit()
    return tid


def test_active_pr_does_not_arm_when_metadata_owns_the_pr(kanban_home):
    """``metadata.published_pr`` names the card's own PR → the merge/review work
    is this card's, so it must be dispatched (the 74% false positive)."""
    with kbc.connect() as conn:
        tid = _card(conn, title="rebase and merge the PR",
                    metadata={"published_pr": PR_URL.format(n=19355)},
                    pr_numbers=(19355,))
        assert kbd._own_pr_numbers(conn, tid) == {19355}
        assert kbd.check_respawn_guard(conn, tid) is None


def test_active_pr_does_not_arm_when_contract_owns_the_pr(kanban_home):
    """An exact PR URL in ``completion_contract`` is an ownership record too."""
    with kbc.connect() as conn:
        tid = _card(conn, title="merge-close",
                    contract=PR_URL.format(n=19355), pr_numbers=(19355,))
        assert kbd._own_pr_numbers(conn, tid) == {19355}
        assert kbd.check_respawn_guard(conn, tid) is None


@pytest.mark.parametrize("owner_field", ["idempotency_key", "title"])
def test_active_pr_does_not_arm_for_pr_key_or_title(kanban_home, owner_field):
    """``pr:<n>`` and ``#<n>`` are the other two structured ownership records."""
    with kbc.connect() as conn:
        if owner_field == "idempotency_key":
            tid = _card(conn, title="[FIX][CI] finish the PR", key="pr:19355",
                        pr_numbers=(19355,))
        else:
            tid = _card(conn, title="[REVIEW][SEC] PR #19371 head-bound verdict",
                        pr_numbers=(19371,))
        assert kbd.check_respawn_guard(conn, tid) is None


def test_active_pr_still_arms_for_a_foreign_pr(kanban_home):
    """A card that owns nothing and cites someone else's PR is the real
    duplicate-PR risk — the brake must stay closed."""
    with kbc.connect() as conn:
        tid = _card(conn, title="implement the retry fix", pr_numbers=(19355,))
        assert kbd._own_pr_numbers(conn, tid) == set()
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_active_pr_still_arms_for_foreign_pr_when_card_owns_another(kanban_home):
    """Owning one PR does not license citing a different one."""
    with kbc.connect() as conn:
        tid = _card(conn, title="my own PR", contract=PR_URL.format(n=19355),
                    metadata={"published_pr": PR_URL.format(n=19355)},
                    pr_numbers=(19355, 99999))
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_active_pr_does_not_arm_without_any_pr_url(kanban_home):
    """No PR URL in any comment → the rule never had anything to arm on, even
    when the prose names a number."""
    with kbc.connect() as conn:
        tid = _card(conn, title="follow-up on the merged work")
        kb.add_comment(conn, tid, "pm", "PR #19355 is merged; verify the board.")
        conn.commit()
        assert kbd.check_respawn_guard(conn, tid) is None
