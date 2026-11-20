"""A missed worker receipt is recoverable: it must not latch the process and must not hard-interrupt
the turn (fix card t_9d90c842, C2).

Measured defect (diagnosis card t_091b2345): one miss set the sticky ``self.failure`` latch, so every
later persist in that process failed *instantly* with the same string (the two ``append_message
failed`` lines in the incident log were 4 ms apart); the pending journal entry was then refused with
``pending_receipt``; and ``DurableTurnLease.refresh_tick`` -- which retries a SQLite lock -- called
``_interrupt_turn`` for a receipt miss. Every one of those killed a turn whose transcript row landed
12-18 s later (the owner had applied the write and lost only the ACK).

The outbox makes the retry safe by construction: an entry is journalled BEFORE it is sent, and an
exact re-send resolves to its ORIGINAL receipt, so a replay must keep ``sequence`` unchanged.
"""
from __future__ import annotations

import contextlib
import time
from types import SimpleNamespace

import pytest

from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError
from agent.turn_facade_lease import LEASE_TTL_SECONDS, DurableTurnLease

# What the crash chain actually surfaced: the message survives RPC wrapping, the type may not.
_RECEIPT_MISS = "worker_receipt_timeout"


def _scope():
    return {"session_id": "s", "execution_id": "w", "epoch": 1}


def test_lost_receipt_does_not_latch_and_replays_its_own_sequence(tmp_path):
    sent = []
    owner = {"down": True}

    def rpc(method, **params):
        sent.append(params)
        if owner["down"]:
            raise TimeoutError(_RECEIPT_MISS)
        return {"count": 1, "annotations": [{"_row_id": 7}]}

    store = RuntimeSessionStore(rpc, _scope(), tmp_path / "private")
    try:
        with pytest.raises(TimeoutError):
            store.append_messages_batch("s", [{"role": "user", "content": "first"}])

        # Not latched: the sticky ``self.failure`` was what killed every later persist 4 ms later.
        assert store.failure is None
        assert store.journal["pending"][0]["sequence"] == 1

        owner["down"] = False
        assert store.append_messages_batch("s", [{"role": "user", "content": "second"}]) == 1

        # The unacknowledged entry is REPLAYED at its original sequence, then the new one applies:
        # no duplicate, no ``pending_receipt`` for the previous caller's late ACK.
        assert [params["sequence"] for params in sent] == [1, 1, 2]
        assert store.journal["pending"] == []
        assert store.journal["next_sequence"] == 3
    finally:
        store.close()


def test_a_durable_local_failure_still_latches(tmp_path):
    """Only a lost RECEIPT is retryable; a real write failure stays sticky.

    The first call surfaces the underlying error unchanged (``retry_pending`` never re-wraps
    what the transport raised); the point is the LATCH: the second call fails fast with the
    recorded failure instead of re-sending anything.
    """

    def rpc(method, **params):
        raise OSError("private/ is read-only")

    store = RuntimeSessionStore(rpc, _scope(), tmp_path / "private")
    try:
        with pytest.raises(OSError):
            store.append_messages_batch("s", [{"role": "user", "content": "x"}])
        assert store.failure
        with pytest.raises(WorkerPersistenceError):
            store.append_messages_batch("s", [{"role": "user", "content": "y"}])
    finally:
        with contextlib.suppress(WorkerPersistenceError):
            store.close()


def _lease(tmp_path, rpc, *, expires_in=LEASE_TTL_SECONDS):
    store = RuntimeSessionStore(rpc, {"session_id": "s"}, tmp_path / "private")
    interrupts = []
    agent = SimpleNamespace(
        session_id="s",
        _execution_thread_id=None,
        interrupt=lambda message, **kwargs: interrupts.append(message),
    )
    lease = DurableTurnLease(agent, store, "s", "holder", expires_at=time.time() + expires_in)
    lease.turn_active = True
    return store, lease, interrupts


def test_refresh_tick_retries_a_lost_receipt_without_interrupting(tmp_path):
    def rpc(method, **params):
        raise TimeoutError(_RECEIPT_MISS)

    store, lease, interrupts = _lease(tmp_path, rpc)
    try:
        assert lease.refresh_tick() is None
        assert interrupts == []
        assert lease.interrupt_message is None
    finally:
        with contextlib.suppress(WorkerPersistenceError):
            store.close()  # the entry stays pending on purpose: it is durable, not failed


def test_refresh_tick_still_interrupts_a_genuine_lease_loss(tmp_path):
    def rpc(method, **params):
        return {"value": False}

    store, lease, interrupts = _lease(tmp_path, rpc)
    try:
        assert lease.refresh_tick() is False
        assert len(interrupts) == 1
    finally:
        store.close()


def test_refresh_tick_does_not_retry_past_the_authority_deadline(tmp_path):
    """The deadline still governs: a receipt miss with no room for another attempt stops."""

    def rpc(method, **params):
        raise TimeoutError(_RECEIPT_MISS)

    store, lease, interrupts = _lease(tmp_path, rpc, expires_in=1.0)
    try:
        assert lease.refresh_tick() is False
        assert len(interrupts) == 1
    finally:
        with contextlib.suppress(WorkerPersistenceError):
            store.close()
