"""A dead persistence owner is not a provider timeout (kanban t_56e39a6d).

Measured on the shipping classifier BEFORE this fix, with the two shapes the same condition
produces (the first is what ``_OwnerLink.receive`` raised, the second is what the sticky-failure
latch re-raised on every later call):

    classify_api_error(TimeoutError('worker_receipt_timeout'))
        -> reason=timeout          retryable=True  context={'transport': 'timeout'}
    classify_api_error(WorkerPersistenceError('worker_receipt_timeout'))
        -> reason=unknown          retryable=True  context={}

Both grant in-place model recovery, so one stalled owner burned three recovery turns per worker
and then exited non-zero -- booked by the dispatcher as a card crash (26 workers' worth on
2026-11-26, ``t_caf39157`` re-ran already-merged work 16 times).

This module pins the four properties the fix must keep:

* the receipt miss has its OWN type, and the type SURVIVES the sticky latch;
* it classifies as ``store_unavailable``, which is not retryable and not a transport timeout;
* a genuine provider ``TimeoutError`` still classifies as ``timeout`` and still retries in place;
* the receipt budget escalates without ever resending the request.
"""

from __future__ import annotations

import queue

import pytest

from agent.error_classifier import TRANSIENT_REASONS, FailoverReason, classify_api_error
from agent.kanban_turn_recovery import should_recover_turn
from agent.runtime_session_store import (
    _RECEIPT_BUDGETS_S,
    WorkerPersistenceError,
    WorkerReceiptTimeout,
    RuntimeSessionStore,
)

SCOPE = {"session_id": "s"}


# ── the type ─────────────────────────────────────────────────────────────────────────────────────

def test_the_receipt_timeout_is_a_persistence_error_of_its_own_type():
    """Existing callers catch ``WorkerPersistenceError``; the new distinction must not break them."""
    assert issubclass(WorkerReceiptTimeout, WorkerPersistenceError)
    assert not issubclass(WorkerReceiptTimeout, TimeoutError), (
        "the whole bug is that this condition was a bare TimeoutError, which the classifier reads "
        "as a provider read timeout"
    )


def test_the_latch_keeps_the_type_on_every_later_call(tmp_path):
    """``self.failure`` alone lost the type: the next raise became ``unknown`` (retryable)."""
    def owner_silent(method, **params):
        raise WorkerReceiptTimeout("worker_receipt_timeout")

    store = RuntimeSessionStore(owner_silent, SCOPE, tmp_path / "private")
    try:
        with pytest.raises(WorkerReceiptTimeout):
            store.append_messages_batch("s", [{"role": "user", "content": "x"}])
        # Failure is sticky: this second call re-raises the LATCHED error.
        with pytest.raises(WorkerReceiptTimeout):
            store.queue_token_counts("s", input_tokens=1)
        assert store.failure == "worker_receipt_timeout"
        assert store.failure_type is WorkerReceiptTimeout
    finally:
        with pytest.raises(WorkerReceiptTimeout):
            store.close()


def test_a_foreign_exception_still_latches_as_a_plain_persistence_error(tmp_path):
    """Only our own family keeps its type -- no OSError/TimeoutError leaks into the latched raise."""
    def offline(method, **params):
        raise TimeoutError("offline")

    store = RuntimeSessionStore(offline, SCOPE, tmp_path / "private")
    try:
        with pytest.raises(TimeoutError):
            store.append_messages_batch("s", [{"role": "user", "content": "x"}])
        with pytest.raises(WorkerPersistenceError) as excinfo:
            store.queue_token_counts("s", input_tokens=1)
        assert not isinstance(excinfo.value, WorkerReceiptTimeout)
        assert store.failure_type is WorkerPersistenceError
    finally:
        with pytest.raises(WorkerPersistenceError):
            store.close()


# ── classification (AC3 regression + the new verdict) ────────────────────────────────────────────

def test_a_dead_owner_is_store_unavailable_and_not_retryable():
    classified = classify_api_error(
        WorkerReceiptTimeout("worker_receipt_timeout"), provider="bangioi-gateway", model="m")
    assert classified.reason is FailoverReason.store_unavailable
    assert classified.retryable is False
    assert "transport" not in classified.error_context, "a dead owner is not a transport timeout"
    assert FailoverReason.store_unavailable not in TRANSIENT_REASONS


def test_a_wrapped_receipt_timeout_is_still_store_unavailable():
    """A turn can wrap the error before the retry loop classifies it."""
    try:
        try:
            raise WorkerReceiptTimeout("worker_receipt_timeout")
        except WorkerReceiptTimeout as inner:
            raise RuntimeError("turn failed") from inner
    except RuntimeError as exc:
        assert classify_api_error(exc).reason is FailoverReason.store_unavailable


def test_a_real_provider_timeout_still_retries_in_place():
    """AC3 -- the half of the original behaviour that MUST survive."""
    classified = classify_api_error(TimeoutError("Request timed out"), provider="bangioi-gateway")
    assert classified.reason is FailoverReason.timeout
    assert classified.retryable is True
    assert classified.error_context.get("transport") == "timeout"
    result = {"failed": True, "failure_reason": classified.reason.value,
              "failure_retryable": classified.retryable}
    assert should_recover_turn(result, attempt=0, task_id="t1") is True


def test_a_dead_owner_grants_no_in_place_model_recovery():
    """Typed refusal, and a second belt for a classifier that still said retryable."""
    typed = {"failed": True, "failure_reason": "store_unavailable", "failure_retryable": False}
    assert should_recover_turn(typed, attempt=0, task_id="t1") is False
    optimistic = {"failed": True, "failure_reason": "store_unavailable", "failure_retryable": True}
    assert should_recover_turn(optimistic, attempt=0, task_id="t1") is False


# ── the receipt budget escalates without resending ───────────────────────────────────────────────

class _Replies:
    """Stands in for the _OwnerLink reply queue (a real SimpleQueue takes no attribute patches)."""

    def __init__(self, answer_on_call=None):
        self.answer_on_call = answer_on_call
        self.waits = []

    def get(self, timeout=None):
        self.waits.append(timeout)
        if self.answer_on_call is not None and len(self.waits) >= self.answer_on_call:
            return {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}
        raise queue.Empty


def _bare_link(replies):
    """A _OwnerLink with no socket and no reader thread: only ``receive`` is under test."""
    from agent.runtime_session_store import _OwnerLink
    link = _OwnerLink.__new__(_OwnerLink)
    link.replies = replies
    link.awaiting = 7
    return link


def _bare_rpc():
    """A WorkerRPC with no discovery state: only ``_receive_receipt`` is under test."""
    from agent.runtime_session_store import WorkerRPC
    return WorkerRPC.__new__(WorkerRPC)


def test_the_receipt_budget_escalates_to_the_last_window_before_giving_up():
    replies = _Replies()
    with pytest.raises(WorkerReceiptTimeout):
        _bare_rpc()._receive_receipt(_bare_link(replies))
    assert replies.waits == list(_RECEIPT_BUDGETS_S)
    assert sum(_RECEIPT_BUDGETS_S) > 60.0, (
        "the total must outlive the owner's own ~60 s transcript-write patience, or a slow but "
        "alive owner can never answer"
    )


def test_a_late_receipt_inside_a_later_window_is_still_returned():
    replies = _Replies(answer_on_call=2)
    assert _bare_rpc()._receive_receipt(_bare_link(replies)) == {
        "jsonrpc": "2.0", "id": 7, "result": {"ok": True}}
    assert replies.waits == list(_RECEIPT_BUDGETS_S)[:2]
