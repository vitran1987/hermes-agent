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


def test_active_pr_does_not_arm_without_any_pr_url(kanban_home):
    """No PR URL in any comment → the rule never had anything to arm on, even
    when the prose names a number."""
    with kbc.connect() as conn:
        tid = _card(conn, title="follow-up on the merged work")
        kb.add_comment(conn, tid, "pm", "PR #19355 is merged; verify the board.")
        conn.commit()
        assert kbd.check_respawn_guard(conn, tid) is None


# --- pair-cite / title-reference (t_00a88dad) ------------------------------
#
# Measured on the live board 2026-11-28: 227 ``respawn_guarded{active_pr}``
# events in 4 h, concentrated on two cards.
#
# * ``t_ca100e2d`` owns 19416 (completion_contract) and its comment cites the
#   fork PR ``vitran1987/hermes-agent#66`` together with the upstream PR
#   ``#19416``. ``cited <= own`` could never match a comment that also names a
#   foreign number, so a LIVE card was held on every tick.
# * ``t_31f09963``'s title is "[FIX][TEST] Post-#19405 hardening ..." -- 19405
#   is the SUPERSEDED PR the card references, not the PR it delivered (19413).
#   ``#(\d{2,})`` read that reference as ownership, so ``own`` held the wrong
#   number and the delivered card was held on the right one.
#
# The contract these cases pin: a comment that cites ANY PR the card owns is
# the card's work (dispatch), a comment whose PRs are all foreign -- and any
# comment on a card with no structured owner -- still arms the brake, and only
# a title that makes the PR its SUBJECT records ownership.

FORK_PR_URL = "https://github.com/vitran1987/hermes-agent/pull/{n}"
UPSTREAM_PR_URL = "https://github.com/bangioi/hermes-agent/pull/{n}"


def test_active_pr_does_not_arm_when_comment_cites_own_pr_plus_fork_pr(kanban_home):
    """The standard "port to the fork, then open the upstream PR" comment cites
    BOTH numbers; ONE owned number is enough -- it is one work item."""
    with kbc.connect() as conn:
        tid = _card(conn, title="[FIX][docs] delta row for the fork port",
                    contract=UPSTREAM_PR_URL.format(n=19416))
        kb.add_comment(conn, tid, "worker",
                       "fork %s (OPEN) ported, upstream %s merged"
                       % (FORK_PR_URL.format(n=66),
                          UPSTREAM_PR_URL.format(n=19416)))
        conn.commit()
        assert kbd._own_pr_numbers(conn, tid) == {19416}
        assert kbd.check_respawn_guard(conn, tid) is None


def test_active_pr_still_arms_when_every_cited_pr_is_foreign(kanban_home):
    """Owning one PR does not license citing a DIFFERENT one: a comment whose
    PRs are all foreign is still the duplicate-PR brake's job."""
    with kbc.connect() as conn:
        tid = _card(conn, title="my own PR", contract=UPSTREAM_PR_URL.format(n=19355),
                    pr_numbers=(99999,))
        assert kbd._own_pr_numbers(conn, tid) == {19355}
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_title_reference_is_not_ownership(kanban_home):
    """``Post-#19405`` NAMES the superseded PR; it is not a record of ownership,
    so the delivered card is held on nothing (own stays empty)."""
    with kbc.connect() as conn:
        tid = _card(conn,
                    title="[FIX][TEST] Post-#19405 hardening: explicit "
                          "Approved=false gate arm + T22k",
                    pr_numbers=(19413,))
        assert kbd._own_pr_numbers(conn, tid) == set()
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


@pytest.mark.parametrize("title, expected", [
    ("[REVIEW][SEC] PR #19413 head-bound verdict", {19413}),
    ("[MERGE] PR #19413 -- rebase and land", {19413}),
    ("[FIX] #19413 gate the wake", {19413}),
    ("PR #19413 closes the loop", {19413}),
    ("PR#19413 no space form", {19413}),
    ("issue #19413 owns nothing", set()),
    ("after #19413 the guard must lift", set()),
    ("[FIX] Post-#19405 hardening", set()),
    ("[FIX] respawn_guarded{active_pr} holds 227 events", set()),
])
def test_title_ownership_needs_the_pr_as_the_subject(kanban_home, title, expected):
    """``#N`` is ownership only when the title puts the PR in subject position
    (leading ``#N`` / ``PR #N``, after any ``[TAG]`` prefix)."""
    with kbc.connect() as conn:
        tid = _card(conn, title=title)
        assert kbd._own_pr_numbers(conn, tid) == expected


def test_pr_key_ownership_is_unchanged(kanban_home):
    """``idempotency_key = pr:<n>`` remains the primary ownership record."""
    with kbc.connect() as conn:
        tid = _card(conn, title="finish the PR", key="pr:19413")
        kb.add_comment(conn, tid, "worker", "opened %s" % PR_URL.format(n=19413))
        conn.commit()
        assert kbd._own_pr_numbers(conn, tid) == {19413}
        assert kbd.check_respawn_guard(conn, tid) is None
