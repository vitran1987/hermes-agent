"""The worker receipt budget must outlive the owner's own write patience (fix card t_9d90c842, C3).

Measured defect (diagnosis card t_091b2345): the client waited a hard-coded 20 s
(``link.receive(20)``) while the owner applies the SAME write under
``SessionDB._TRANSCRIPT_WRITE_PATIENCE_S`` = 60 s (``hermes_state_runtime.apply_worker_receipt``), so
a slow-but-alive owner could never deliver a receipt. The transcript row landed 12-18 s AFTER the
20 s budget expired (session ``local-ecdc3023…``: worker gave up 15:00:07, row landed 15:00:19), i.e.
the turn was killed for a late ACK while the write itself was fine -- 463 ``worker_receipt_timeout``
events in one day on one profile.
"""
from __future__ import annotations

from agent import runtime_session_store as store_mod
from agent.runtime_session_store import WorkerRPC


def test_receipt_budget_exceeds_the_owners_maximum_patience():
    from hermes_state import SessionDB

    owner_patience = max(
        SessionDB._WRITE_PATIENCE_S, SessionDB._TRANSCRIPT_WRITE_PATIENCE_S
    )
    assert store_mod._RECEIPT_BUDGET_S > owner_patience, (
        store_mod._RECEIPT_BUDGET_S, owner_patience
    )
    # The measured defect: the owner's row landed 12-18 s AFTER a 20 s budget expired.
    assert store_mod._RECEIPT_BUDGET_S > 20.0


def test_worker_rpc_waits_the_full_receipt_budget(tmp_path):
    """The wire uses the constant, not a stale literal."""
    recorded = {}

    class _Link:
        def send(self, request_id, message):
            recorded["sent"] = message

        def receive(self, timeout):
            recorded["timeout"] = timeout
            return {"result": {"ok": True}}

    rpc = WorkerRPC(tmp_path)
    rpc._connection = lambda: _Link()  # no dial, no ticket: the budget is the subject
    try:
        assert rpc("worker.persist", session_id="s") == {"ok": True}
    finally:
        rpc.close()
    assert recorded["timeout"] == store_mod._RECEIPT_BUDGET_S
    assert recorded["timeout"] > 20.0
