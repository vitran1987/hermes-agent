"""Notification replay: a stale crash must not be delivered as a fresh alert.

Measured on the live board before this fix (2026-11-22, 10:00-13:00): 48 notifier wakes
for 61 real crash/gave_up events, with a wake->turn lag of median 43.5 min / max 111 min,
because every crashed card admitted its own turn into the destination session's serial
FIFO and the text carried no timestamp. The operator could not tell a replayed 111-minute
crash from a fresh one.

These tests pin the two halves of the fix at the claim/wake boundary:

* the age gate drops events that are already stale when claimed (and still delivers the
  fresh ones — it must not silence a real crash), with the subscription cursor advanced
  so the stale event can never be re-claimed;
* the wake coalescer admits at most one turn per destination route per window, while the
  per-event ping still goes out for every event.
"""
import asyncio
import time

from gateway import kanban_watchers_notifier as kwn
from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _age_events(conn, task_id, *, seconds):
    """Backdate a task's events, so they read as an old failure at claim time.

    The notifier decides staleness from ``task_events.created_at``, exactly as the live
    board records it; backdating is how an old crash reaches a notifier that had not yet
    claimed it (gateway restart, DB-lock retry, rewound cursor).
    """
    cutoff = int(time.time()) - seconds
    conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ?", (cutoff, task_id))
    conn.commit()


def _notified_task(conn, title, *, chat_id="chat-1"):
    tid = kb.create_task(conn, title=title, assignee="worker")
    kbn.add_notify_sub(
        conn, task_id=tid, platform="telegram", chat_id=chat_id,
        delivery_mode="notify+wake",
    )
    return tid


def test_two_stale_crashes_and_one_fresh_crash_delivers_only_the_fresh_one(tmp_path, monkeypatch):
    """AC4: 2 old crashes + 1 new crash -> exactly one notification, for the new crash.

    The stale pair must be dropped at the claim *and* the fresh one must still be
    delivered (AC3: the fix narrows what is replayed, it does not silence the channel).
    """
    db_path = tmp_path / "replay-age-gate.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kbc.connect()
    try:
        stale_a = _notified_task(conn, "old crash A")
        kb.block_task(conn, stale_a, reason="worker died long ago")
        _age_events(conn, stale_a, seconds=7200)   # 2h old

        stale_b = _notified_task(conn, "old crash B")
        kb.block_task(conn, stale_b, reason="worker died long ago")
        _age_events(conn, stale_b, seconds=1800)   # 30m old

        fresh = _notified_task(conn, "fresh crash")
        kb.block_task(conn, fresh, reason="worker just died")

        cursors_before = {
            tid: kbn._notify_cursor(conn, tid, "telegram", "chat-1", "")
            for tid in (stale_a, stale_b, fresh)
        }
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    delivered = " \n".join(m["text"] for m in adapter.sent)
    assert fresh in delivered, f"the fresh crash must be delivered: {delivered!r}"
    assert stale_a not in delivered, f"a 2h-old crash was replayed: {delivered!r}"
    assert stale_b not in delivered, f"a 30m-old crash was replayed: {delivered!r}"
    assert len(adapter.sent) == 1

    # The stale events were claimed, so their cursors advanced: dropping them at the
    # claim is what makes the replay impossible, not a per-tick filter.
    conn = kbc.connect()
    try:
        for tid in (stale_a, stale_b):
            after = kbn._notify_cursor(conn, tid, "telegram", "chat-1", "")
            assert after > cursors_before[tid], f"{tid} cursor did not advance past its stale event"
            _, events = kbn.unseen_events_for_sub(
                conn, task_id=tid, platform="telegram", chat_id="chat-1", kinds=kwn.TERMINAL_KINDS,
            )
            assert events == [], f"{tid} still has unclaimed events: {[e.kind for e in events]}"
    finally:
        conn.close()


def test_age_gate_off_delivers_every_claimed_event(tmp_path, monkeypatch):
    """``notify_max_event_age_seconds: 0`` restores the old deliver-everything behaviour."""
    db_path = tmp_path / "replay-age-gate-off.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(kwn, "_notify_policy", lambda: (0, 0))
    kb.init_db()
    conn = kbc.connect()
    try:
        old = _notified_task(conn, "old crash")
        kb.block_task(conn, old, reason="worker died long ago")
        _age_events(conn, old, seconds=7200)
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert old in adapter.sent[0]["text"]


def test_crash_wave_coalesces_to_one_wake_but_pings_every_card(tmp_path, monkeypatch):
    """A wave of N fresh crashes: N pings, one wake turn for the destination route.

    This is the measured 6-card wave shape (2026-11-22 12:10:34). Without coalescing it
    admits 6 turns into a serial FIFO, which is what delayed later alerts by 35-111 min.
    """
    db_path = tmp_path / "replay-wave.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(kwn, "_notify_policy", lambda: (300, 120))
    kb.init_db()
    conn = kbc.connect()
    try:
        tids = []
        for i in range(4):
            tid = _notified_task(conn, f"wave card {i}")
            kb.block_task(conn, tid, reason="wave crash")
            tids.append(tid)
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Every card is still announced (nothing silenced), one message each...
    assert len(adapter.sent) == len(tids)
    assert {tid for tid in tids if tid in " \n".join(m["text"] for m in adapter.sent)} == set(tids)
    # ...but only ONE turn was admitted into the destination session.
    assert len(adapter.handled) == 1


def test_wake_coalescing_off_admits_one_wake_per_card(tmp_path, monkeypatch):
    """``notify_wake_min_interval_seconds: 0`` restores one wake per event."""
    db_path = tmp_path / "replay-wave-off.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(kwn, "_notify_policy", lambda: (300, 0))
    kb.init_db()
    conn = kbc.connect()
    try:
        for i in range(3):
            tid = _notified_task(conn, f"wave card {i}")
            kb.block_task(conn, tid, reason="wave crash")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 3
    assert len(adapter.handled) == 3


def test_ping_carries_the_event_clock_so_a_late_wake_is_identifiable(tmp_path, monkeypatch):
    """Every ping states WHEN the event happened, so a queued delivery cannot read as fresh."""
    db_path = tmp_path / "replay-clock.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(kwn, "_notify_policy", lambda: (3600, 0))
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = _notified_task(conn, "clocked crash")
        kb.block_task(conn, tid, reason="worker died")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    text = adapter.sent[0]["text"]
    assert "event " in text and ":" in text.split("event ", 1)[1]


def test_event_age_helpers():
    """The gate reads the row's own ``created_at``; a disabled/threshold value is honoured."""
    class _Ev:
        def __init__(self, created_at):
            self.created_at = created_at

    now = int(time.time())
    fresh, stale = kwn._split_by_age([_Ev(now), _Ev(now - 600)], 300)
    assert [e.created_at for e in fresh] == [now]
    assert [e.created_at for e in stale] == [now - 600]
    # 0 disables the gate: every event is fresh, however old.
    assert len(kwn._split_by_age([_Ev(now - 600)], 0)[0]) == 1
    # An event with no usable stamp is treated as fresh, never dropped on a guess.
    assert kwn._event_age_seconds(_Ev(None)) < 0
    assert len(kwn._split_by_age([_Ev(None)], 300)[0]) == 1
    assert kwn._event_clock(_Ev(None)) == ""
    assert len(kwn._event_clock(_Ev(now))) == 8
