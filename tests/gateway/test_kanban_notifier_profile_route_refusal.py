"""A refused profile route never delivers under the launch profile (#154610).

``_owner_scope`` resolves the subscription's profile home under ``multiplex_profiles``, and that
resolver refuses a profile this gateway does not serve by raising ``ProfileRouteRejected``. The
notifier must not fall back to the LAUNCH profile's scope for such a delivery: a profile that
still EXISTS (parked) has its claim rewound so the batch delivers once the profile is served
again, while a DELETED one spends the send-failure budget so its subscription is eventually
dropped instead of retrying forever.

The refusal's producer on the live lineage is
``gateway/run_profile_home.resolve_profile_home_for_source`` (``bangioi/main`` does not carry that
module yet — that is its own card), so these cases raise the same ``ProfileRouteRejected`` at the
identical seam the notifier calls: ``runner._resolve_profile_home_for_source``.
"""
import asyncio
from pathlib import Path

from gateway.config import GatewayConfig, Platform
from gateway.kanban_watchers_notifier import MAX_SEND_FAILURES, _KanbanNotification, _notifier_collect
from gateway.profile_routing import ProfileRouteRejected, parse_profile_routes
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_notify as kbn

CHAT = "C_ROUTE"
PARKED = "parked"


class RecordingAdapter:
    supports_async_delivery = True

    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))

    async def handle_message(self, event):
        event._gateway_accepted = True


def setup_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    for name in ("default", PARKED):
        profile = home / "profiles" / name
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "config.yaml").write_text("{}\n", encoding="utf-8")
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {Platform.SLACK: RecordingAdapter()}
    runner._profile_adapters = {}
    runner._primary_profile_name = "default"
    runner._kanban_notifier_profile = "default"
    runner._kanban_dispatcher_lock_handle = object()
    # The route admits the parked profile to the notifier's destination gate; the REFUSAL under
    # test happens later, at delivery, exactly as a profile parked between the two ticks does.
    runner.config = GatewayConfig(
        multiplex_profiles=True,
        profile_routes=parse_profile_routes([dict(platform="slack", chat_id=CHAT, profile=PARKED)]),
    )
    return runner


def parked_subscription():
    with kbc.connect() as conn:
        task = kb.create_task(conn, title="completion", assignee="worker")
        kbn.add_notify_sub(conn, task_id=task, platform="slack", chat_id=CHAT,
                           notifier_profile=PARKED, delivery_mode="notify+wake")
        kb.complete_task(conn, task, result="finished")
    return task


def note_for(runner, board_calls):
    """The claimed batch for the one subscription, with the board writes recorded."""
    rows = _notifier_collect(runner, kb, notifier_profile=PARKED, gc_due=False, gc_retention_days=30)
    assert len(rows) == 1, rows
    runner._kanban_rewind = lambda sub, claimed, old, board=None: board_calls.append("rewind")
    runner._kanban_unsub = lambda sub, board=None: board_calls.append("unsub")
    return _KanbanNotification(runner, rows[0], platform_cls=Platform, sub_fail_counts={})


def refuse_owner_scope(runner):
    def _raise(_source):
        raise ProfileRouteRejected(PARKED, PARKED)
    runner._resolve_profile_home_for_source = _raise


def test_a_parked_profile_rewinds_without_spending_the_failure_budget(tmp_path, monkeypatch):
    runner = setup_runner(tmp_path, monkeypatch)
    parked_subscription()
    calls = []
    note = note_for(runner, calls)
    refuse_owner_scope(runner)

    asyncio.run(note.deliver())

    assert calls == ["rewind"], "a parked profile's claim must be rewound, not dropped"
    assert note.sub_fail_counts == {}, "a parked profile is served again: no failure may be spent"
    assert runner.adapters[Platform.SLACK].sent == []  # never delivered under the launch profile


def test_a_deleted_profile_spends_the_failure_budget(tmp_path, monkeypatch, caplog):
    import hermes_cli.profiles as profiles

    runner = setup_runner(tmp_path, monkeypatch)
    parked_subscription()
    calls = []
    note = note_for(runner, calls)
    refuse_owner_scope(runner)
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: False)

    with caplog.at_level("WARNING", logger="gateway.run"):
        asyncio.run(note.deliver())

    assert note.sub_fail_counts == {note.sub_key: 1}, "a deleted profile must spend a failure"
    assert calls == ["rewind"], "below the limit the claim is retried; the sub drops at the limit"
    assert 1 < MAX_SEND_FAILURES
    assert any("is not served" in rec.getMessage() for rec in caplog.records), caplog.text
    assert runner.adapters[Platform.SLACK].sent == []  # never delivered under the launch profile
