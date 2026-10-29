"""Fail-closed gate: a completion whose evidence names a GitHub PR the card's
``completion_contract`` does not bind (or whose head is not green) is refused
before any state change.

The contract is the card's publication claim — ``local-only``/absent makes no
PR claim, ``owner/repo`` binds every PR of that repo, an exact PR URL binds
that one PR. A completion naming a PR outside the bound scope would let a
worker close a publication card on a sibling PR (or a PR of another repo)
whose CI the card never declared. The gate refuses after an auditable
``completion_blocked_unbound_pr`` event; nothing is mutated.

Fail-closed on ambiguity: a bare ``#<n>`` / ``PR #<n>`` ref whose owning repo
cannot be resolved is refused, not silently completed.
"""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    yield


def _events(conn, task_id, kind):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=?", (task_id, kind))]


def test_unbound_pr_in_result_is_refused(board):
    """A full PR URL in the result that is not bound by the contract is
    refused (reason=unbound)."""
    with connect() as conn:
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        with pytest.raises(kb.UnboundPrClaimError) as exc:
            kb.complete_task(conn, tid, result="shipped in https://github.com/other/repo/pull/9")
        assert exc.value.task_id == tid
        assert exc.value.pr_url == "https://github.com/other/repo/pull/9"
        assert exc.value.contract == "acme/repo"
        assert exc.value.reason == "unbound"
        assert kb.get_task(conn, tid).status != "done"
        events = _events(conn, tid, "completion_blocked_unbound_pr")
        assert len(events) == 1
        assert events[0]["contract"] == "acme/repo"
        assert events[0]["unbound_prs"] == ["https://github.com/other/repo/pull/9"]
        assert events[0]["reason"] == "unbound"


def test_unbound_pr_in_summary_is_refused(board):
    with connect() as conn:
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        with pytest.raises(kb.UnboundPrClaimError) as exc:
            kb.complete_task(conn, tid, summary="see https://github.com/other/repo/pull/9")
        assert exc.value.reason == "unbound"
        assert kb.get_task(conn, tid).status != "done"


def test_unbound_pr_in_metadata_is_refused(board):
    with connect() as conn:
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        with pytest.raises(kb.UnboundPrClaimError) as exc:
            kb.complete_task(conn, tid, result="done",
                             metadata={"published_pr": "https://github.com/other/repo/pull/9"})
        assert exc.value.reason == "unbound"
        assert kb.get_task(conn, tid).status != "done"


def test_sibling_pr_in_same_repo_is_refused(board):
    """An exact-URL contract binds that one PR; a sibling PR of the same repo
    is outside the bound scope."""
    with connect() as conn:
        tid = kb.create_task(conn, title="publish",
                             completion_contract="https://github.com/acme/repo/pull/7")
        with pytest.raises(kb.UnboundPrClaimError) as exc:
            kb.complete_task(conn, tid, result="done",
                             metadata={"published_pr": "https://github.com/acme/repo/pull/8"})
        assert exc.value.pr_url == "https://github.com/acme/repo/pull/8"
        assert kb.get_task(conn, tid).status != "done"


def test_bound_pr_passes_gate(board, monkeypatch):
    """A PR inside the bound scope passes the gate (the green check is
    mocked green here)."""
    with connect() as conn:
        # owner/repo contract: any PR of that repo is bound.
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        monkeypatch.setattr(kb, "_check_pr_green", lambda *a, **kw: None)
        try:
            kb.complete_task(conn, tid, result="done",
                             metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        except kb.UnboundPrClaimError:
            pytest.fail("bound PR must not trip the unbound-PR gate")
        # exact-URL contract: the bound PR itself is allowed.
        tid2 = kb.create_task(conn, title="publish2",
                              completion_contract="https://github.com/acme/repo/pull/7")
        try:
            kb.complete_task(conn, tid2, result="done",
                             metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        except kb.UnboundPrClaimError:
            pytest.fail("bound PR must not trip the unbound-PR gate")


def test_local_only_contract_refuses_pr(board):
    """A local-only card makes no PR claim; naming a PR is a publication claim
    the contract does not bind. Refused (reason=unbound)."""
    with connect() as conn:
        tid = kb.create_task(conn, title="local", completion_contract="local-only")
        with pytest.raises(kb.UnboundPrClaimError) as exc:
            kb.complete_task(conn, tid,
                             summary="https://github.com/acme/repo/pull/7 is background context")
        assert exc.value.reason == "unbound"
        assert exc.value.contract == "local-only"
        assert kb.get_task(conn, tid).status != "done"


def test_absent_contract_refuses_pr(board):
    """A card with no completion_contract (None) makes no PR claim; naming a
    PR is refused (reason=unbound)."""
    with connect() as conn:
        tid = kb.create_task(conn, title="no-contract")
        with pytest.raises(kb.UnboundPrClaimError) as exc:
            kb.complete_task(conn, tid, result="shipped in https://github.com/acme/repo/pull/7")
        assert exc.value.reason == "unbound"
        assert exc.value.contract == "local-only"
        assert kb.get_task(conn, tid).status != "done"


def test_bare_ref_unresolvable_is_refused(board):
    """A bare #<n> ref with no contract repo to resolve against is refused
    (reason=unresolvable). Fail-closed: an unknown-repo PR is exactly the
    shape that hides a red CI."""
    with connect() as conn:
        tid = kb.create_task(conn, title="local", completion_contract="local-only")
        with pytest.raises(kb.UnboundPrClaimError) as exc:
            kb.complete_task(conn, tid, result="shipped in PR #42")
        assert exc.value.reason == "unresolvable"
        assert exc.value.pr_url == "#42"
        assert kb.get_task(conn, tid).status != "done"
        events = _events(conn, tid, "completion_blocked_unbound_pr")
        assert len(events) == 1
        assert events[0]["reason"] == "unresolvable"


def test_bare_ref_resolves_to_bound_repo_passes_gate(board, monkeypatch):
    """A bare #<n> ref with an owner/repo contract resolves to a bound PR.
    The gate passes the binding check; the green check runs (mocked green
    here)."""
    with connect() as conn:
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        # Mock the green check to pass (no gh on PATH in the test env).
        monkeypatch.setattr(kb, "_check_pr_green", lambda *a, **kw: None)
        try:
            kb.complete_task(conn, tid, result="shipped in PR #7")
        except kb.UnboundPrClaimError:
            pytest.fail("bare ref resolving to the bound repo must not trip the gate")


def test_bare_ref_resolves_to_different_pr_is_refused(board):
    """A bare #<n> ref that resolves to a different PR than the exact-URL
    contract is refused (reason=unbound)."""
    with connect() as conn:
        tid = kb.create_task(conn, title="publish",
                             completion_contract="https://github.com/acme/repo/pull/7")
        with pytest.raises(kb.UnboundPrClaimError) as exc:
            kb.complete_task(conn, tid, result="shipped in PR #8")
        assert exc.value.reason == "unbound"
        assert exc.value.pr_url == "https://github.com/acme/repo/pull/8"
        assert kb.get_task(conn, tid).status != "done"


def test_not_green_is_refused(board, monkeypatch):
    """A bound PR whose head is not green is refused by prepare_acceptance
    (returns False, not an exception). The unbound-PR gate passes the binding
    check; the green check is the job of the PR acceptance layer."""
    with connect() as conn:
        tid = kb.create_task(conn, title="publish",
                             completion_contract="https://github.com/acme/repo/pull/7")
        # Mock prepare_acceptance to return False (simulating a red CI).
        monkeypatch.setattr(
            "hermes_cli.kanban_pr_acceptance_store.prepare_acceptance",
            lambda *a, **kw: False,
        )
        ok = kb.complete_task(conn, tid, result="done",
                              metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        assert ok is False
        assert kb.get_task(conn, tid).status != "done"


def test_no_pr_cited_passes_gate(board):
    with connect() as conn:
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        try:
            kb.complete_task(conn, tid, result="done locally")
        except kb.UnboundPrClaimError:
            pytest.fail("no PR cited must not trip the unbound-PR gate")


def test_review_status_is_exempt(board):
    """A human vouches for a review card; the approval is the record."""
    with connect() as conn:
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        assert kb.request_review(conn, tid, summary="ready for review")
        assert kb.get_task(conn, tid).status == "review"
        try:
            kb.complete_task(conn, tid, result="done",
                             metadata={"published_pr": "https://github.com/other/repo/pull/9"})
        except kb.UnboundPrClaimError:
            pytest.fail("review status must be exempt from the unbound-PR gate")


def test_gate_leaves_no_state_change(board):
    """The refusal is fail-closed: no status change, no pr_acceptance receipt,
    exactly one audit event."""
    with connect() as conn:
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        with pytest.raises(kb.UnboundPrClaimError):
            kb.complete_task(conn, tid, result="shipped in https://github.com/other/repo/pull/9")
        task = kb.get_task(conn, tid)
        assert task.status != "done"
        assert conn.execute(
            "SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'",
            (tid,)).fetchone()[0] == 0
        assert len(_events(conn, tid, "completion_blocked_unbound_pr")) == 1


# --- Defect 3: contract downgrade must not switch the gate OFF ---

def test_downgrade_from_pr_url_to_local_only_is_refused(board):
    """A card armed with a PR URL contract, then downgraded to local-only via
    an ``edited`` event carrying ``old_contract``, must still refuse a
    completion whose stored result names the PR."""
    with connect() as conn:
        tid = kb.create_task(conn, title="publish",
                             completion_contract="https://github.com/acme/repo/pull/7")
        # Simulate a prior run that stored a result naming the PR.
        conn.execute(
            "UPDATE tasks SET result='shipped in https://github.com/acme/repo/pull/7' WHERE id=?",
            (tid,))
        # Simulate the downgrade: an ``edited`` event with ``old_contract``
        # carrying the PR URL, and the contract rewritten to local-only.
        kb._append_event(conn, tid, "edited", {
            "pm_contract_fix": True,
            "old_contract": "https://github.com/acme/repo/pull/7",
        })
        conn.execute(
            "UPDATE tasks SET completion_contract='local-only' WHERE id=?", (tid,))
        # Complete with a new result that does NOT name the PR — the stored
        # result still does, and the downgrade must not unbind it.
        with pytest.raises(kb.UnboundPrClaimError) as exc:
            kb.complete_task(conn, tid, result="done")
        assert exc.value.reason == "downgrade"
        assert kb.get_task(conn, tid).status != "done"
        assert len(_events(conn, tid, "completion_blocked_unbound_pr")) == 1


def test_downgrade_without_stored_pr_passes(board):
    """A card downgraded from a PR URL to local-only, whose stored result does
    NOT name a PR, must complete normally (no regression)."""
    with connect() as conn:
        tid = kb.create_task(conn, title="publish",
                             completion_contract="https://github.com/acme/repo/pull/7")
        # Simulate a prior run that stored a result NOT naming the PR.
        conn.execute(
            "UPDATE tasks SET result='done locally' WHERE id=?", (tid,))
        # Simulate the downgrade.
        kb._append_event(conn, tid, "edited", {
            "pm_contract_fix": True,
            "old_contract": "https://github.com/acme/repo/pull/7",
        })
        conn.execute(
            "UPDATE tasks SET completion_contract='local-only' WHERE id=?", (tid,))
        # Complete — no PR in the stored result, so the gate passes.
        try:
            kb.complete_task(conn, tid, result="done")
        except kb.UnboundPrClaimError:
            pytest.fail("downgrade without a stored PR must not trip the gate")


def test_no_downgrade_event_passes(board):
    """A local-only card with no downgrade event in the log must complete
    normally (no regression)."""
    with connect() as conn:
        tid = kb.create_task(conn, title="publish", completion_contract="local-only")
        conn.execute(
            "UPDATE tasks SET result='done locally' WHERE id=?", (tid,))
        try:
            kb.complete_task(conn, tid, result="done")
        except kb.UnboundPrClaimError:
            pytest.fail("local-only card with no downgrade must not trip the gate")
