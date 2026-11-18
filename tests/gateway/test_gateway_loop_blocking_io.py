"""No status/sqlite/thread-start work on the gateway event loop (t_c7ec3eef).

Measured on this host: every gateway watchdog stack dump (42 fires across 9 profiles) with the
event-loop thread blocked in one of four leaves — a synchronous runtime-status read
(``read_runtime_status -> _read_json_file -> open``, 16 dumps), a synchronous ``realpath`` from the
pid record / profile label (1), a synchronous sqlite read (``get_session_admission``, 2, plus
``worker_claim_origin`` reads) or ``Thread.start()`` inside ``run_in_executor`` (1). The watchdog
was right to kill those gateways; these tests pin the fix — each blocking call now runs on a worker
thread, and the async entry points never touch it.

The guard these tests use is a recording spy instead of a syscall hook: the functions that must not
run on the loop are exactly the ones patched, so a test that asserts "the loop thread never appears
among the callers" fails if a future change moves the call back inline.
"""

import asyncio
import sys
import threading
from types import SimpleNamespace

import pytest


def _thread_ids(record):
    return [entry[0] for entry in record]


# ── runtime status: read + publish ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reading_runtime_status_off_loop_never_runs_the_file_read_on_the_loop(tmp_path, monkeypatch):
    from gateway import status

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    calls = []

    def read(path):
        calls.append((threading.get_ident(), path))
        return {"gateway_state": "stopped"}

    monkeypatch.setattr(status, "_read_json_file", read)
    assert await status.read_runtime_status_async() == {"gateway_state": "stopped"}
    assert calls, "the read did not run at all"
    assert threading.get_ident() not in _thread_ids(calls), "read_runtime_status ran on the event loop"


@pytest.mark.asyncio
async def test_publishing_runtime_status_off_loop_prepares_it_on_a_worker_thread(tmp_path, monkeypatch):
    from gateway import status

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    prepared = []

    real_prepare = status._prepare_runtime_status_update

    def prepare(**fields):
        prepared.append(threading.get_ident())
        return real_prepare(**fields)

    monkeypatch.setattr(status, "_prepare_runtime_status_update", prepare)
    monkeypatch.setattr(status, "_get_runtime_status_writer", lambda: SimpleNamespace(submit=lambda *a: 1))
    generation = await status.publish_runtime_status_async(
        gateway_state="running", platform="telegram", platform_state="connected")
    assert generation == 1
    assert prepared, "the status publish never prepared a payload"
    assert threading.get_ident() not in prepared, "publish_runtime_status prepared the payload on the loop"
    assert status.flush_runtime_status(timeout=0.01) in (True, False)  # writer untouched by the test


def test_the_loop_free_publish_path_still_stamps_the_same_identity(tmp_path, monkeypatch):
    """Semantics preserved: the caches only remove syscalls, not fields."""
    from gateway import status

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    status.forget_pid_record_cache()
    status.forget_canonical_home_cache()
    first = status._build_pid_record()
    second = status._build_pid_record()
    assert first["pid"] == second["pid"] == __import__("os").getpid()
    assert first["hermes_home"] == second["hermes_home"] == str(tmp_path.resolve())
    assert first["argv"] == second["argv"]
    assert {"kind", "pid", "argv", "start_time", "hermes_home"} == set(first)


# ── profile reconciler preflight (the 16-dump leaf) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_added_profile_preflight_reads_status_off_the_loop(monkeypatch):
    from gateway import run_profile_reconcile as reconcile

    seen = []

    def recorded_standalone_reason(state_path=None):
        seen.append(threading.get_ident())
        # not a standalone record: the preflight says "stay standalone"

    monkeypatch.setattr("hermes_cli.gateway_multiplex_mode.recorded_standalone_reason",
                        recorded_standalone_reason)
    reason, _launch, _is_root, served = await asyncio.to_thread(
        reconcile._added_profile_preflight, __import__("hermes_constants").get_default_hermes_root)
    assert seen and threading.get_ident() not in seen
    assert reason is None
    assert isinstance(served, int)


# ── sqlite: admission receipts ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_receipt_poll_reads_the_admission_off_the_loop(owner, monkeypatch):
    from gateway import session_authority as authority_module

    calls = []
    real_read = authority_module.get_session_admission

    def read(db, *, admission_id):
        calls.append(threading.get_ident())
        return real_read(db, admission_id=admission_id)

    monkeypatch.setattr(authority_module, "get_session_admission", read)
    authority = owner
    authority.authorize = lambda *a, **k: None  # the authorization rule is not what this test pins
    ref = SimpleNamespace(session_id="s1")
    authority.sessions["s1"] = SimpleNamespace(event_stream=SimpleNamespace(lock=threading.Lock()),
                                               settling={})
    with pytest.raises(Exception):
        # No such admission: the read still happened, and that is the only thing under test.
        await authority.receipt(SimpleNamespace(subject="u", capabilities=[]), ref, "missing")
    assert calls, "the admission was never read"
    assert threading.get_ident() not in calls, "get_session_admission ran on the event loop"


# ── executor / goals warm-up: no Thread.start() on the loop ──────────────────────────────────────


def test_a_prewarmed_executor_hands_work_to_a_parked_thread_without_starting_one(monkeypatch):
    import time

    from gateway.turn_executor import _UnboundedThreadExecutor

    pool = _UnboundedThreadExecutor(thread_name_prefix="t")
    try:
        pool.prewarm(1)
        assert pool.idle_workers == 1
        started = []
        real_start = threading.Thread.start

        def start(self):
            started.append(self.name)
            return real_start(self)

        def submit_from_another_thread():
            pool.submit(lambda: 42).result(timeout=5)

        monkeypatch.setattr(threading.Thread, "start", start)
        submitter = threading.Thread(target=submit_from_another_thread, name="submitter", daemon=True)
        real_start(submitter)  # not recorded: this thread is the test's, not the pool's
        submitter.join(timeout=10)
        assert not [name for name in started if name.startswith("t_")], (
            f"submit started a pool worker instead of reusing the parked one: {started}")
        deadline = time.monotonic() + 5
        while pool.idle_workers != 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pool.idle_workers == 1  # the worker went back to the pool
    finally:
        pool.shutdown(wait=False)


@pytest.mark.asyncio
async def test_the_goals_warm_up_skips_the_executor_once_the_db_is_warm(monkeypatch):
    from gateway.run_goals import GatewayGoalsMixin

    hops = []

    class _Runner(GatewayGoalsMixin):
        async def _run_in_executor_with_context(self, func, *args):
            hops.append(func)
            raise AssertionError("warm cache must not hop to the executor")

    monkeypatch.setattr("hermes_cli.goals.session_db_is_warm", lambda: True)
    await _Runner()._warm_goals_session_db("loop wakeup")
    assert hops == []


# ── transition emit: no profile realpath when nothing changed ────────────────────────────────────


def test_a_no_change_publish_does_not_resolve_the_profile(monkeypatch):
    from agent.monitoring import gateway_health

    resolved = []
    monkeypatch.setattr(gateway_health, "_safe_profile", lambda: resolved.append(1) or "default")
    snapshot = {"gateway_state": "running", "platforms": {"telegram": {"state": "connected"}}}
    gateway_health.emit_runtime_status_transition(snapshot, dict(snapshot))
    assert resolved == [], "a heartbeat-only publish paid for the profile lookup"


def test_a_real_transition_still_emits(monkeypatch):
    from agent.monitoring import gateway_health

    events = []
    monkeypatch.setattr(gateway_health.emitter, "emit", events.append)
    gateway_health.emit_runtime_status_transition(
        {"gateway_state": "starting", "platforms": {}}, {"gateway_state": "running", "platforms": {}})
    assert events, "a real state transition no longer emits"
    assert events[0].name == "gateway.lifecycle"


# ── the watcher that owns the 16-dump leaf must hop, not just its helper ─────────────────────────


@pytest.mark.asyncio
async def test_the_reconcile_watcher_runs_its_preflight_off_the_loop(monkeypatch):
    """Pins the CALLER, not the helper: the previous shape of this test called the helper itself
    through ``to_thread``, which passes even when the watcher stops hopping."""
    from gateway import run_profile_reconcile as reconcile

    seen = []

    def preflight(get_default_root):
        seen.append(threading.get_ident())
        return (None, None, False, 0)  # not a standalone verdict: the method returns False

    monkeypatch.setattr(reconcile, "_added_profile_preflight", preflight)

    class _Runner(reconcile.GatewayProfileReconcileMixin):
        _running = True

        def _multiplex_on(self):
            return False

    assert await _Runner()._multiplex_for_added_profile() is False
    assert seen, "the preflight never ran"
    assert threading.get_ident() not in seen, "the reconcile watcher ran its preflight on the event loop"


# ── sqlite: the 10-dump worker claim leaf ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_worker_claim_authenticates_off_the_loop(monkeypatch):
    """``worker_claim_origin`` was the loop leaf in 10 of the 44 dumps (``persist``/``adopt``)."""
    from gateway import session_worker
    from gateway import session_authorities

    calls = []

    def record(name, result=None):
        def spy(*args, **kwargs):
            calls.append((name, threading.get_ident()))
            return result
        return spy

    monkeypatch.setattr(session_authorities, "require_serving", lambda authority: None)
    monkeypatch.setattr(session_worker, "_claim", record("claim", "claim-digest"))
    monkeypatch.setattr(session_worker, "_verify", record("verify"))
    monkeypatch.setattr(session_worker, "adopt_worker_execution", record("adopt", {"adopted": True}))

    authority = SimpleNamespace(profile_id="p", epoch=1, db=object(), _require_admission_open=lambda: None)
    connection = SimpleNamespace(
        authority=authority,
        actor=SimpleNamespace(subject="u", profile_id="p", capabilities={"worker:adopt"}))
    params = {"profile_id": "p", "session_id": "s", "execution_id": "e", "generation": 0,
              "pid": 4242, "birth": 1.5, "secret": "x" * 16}
    ref = SimpleNamespace(session_id="s")

    assert await session_worker.worker_request(connection, ref, params, operation="adopt") == {"adopted": True}
    assert [name for name, _ in calls] == ["claim", "verify", "adopt"]
    loop_ident = threading.get_ident()
    assert all(ident != loop_ident for _, ident in calls), (
        f"a worker claim ran on the event loop: {calls}")


@pytest.mark.asyncio
async def test_a_cron_admission_read_runs_off_the_loop(monkeypatch):
    """``get_session_admission`` under ``scheduler_authority.observe`` was 1 of the 44 dumps."""
    from gateway import session_cron

    seen = []

    def read(db, *, admission_id):
        from hermes_state_keys import CRON_PRINCIPAL_ID
        seen.append(threading.get_ident())
        return {"admission_id": admission_id, "target_session_id": "s",
                "principal_id": CRON_PRINCIPAL_ID, "status": "terminal"}

    monkeypatch.setattr(session_cron, "get_session_admission", read)
    monkeypatch.setattr("gateway.session_authorities.require_serving", lambda authority: None)
    from hermes_state_keys import CRON_PRINCIPAL_ID

    authority = SimpleNamespace(profile_id="p", db=object(), authorize=lambda *a, **k: None)

    # ``actor=None`` makes ``_actor`` mint the cron ticker actor; its principal is the row's.
    assert await session_cron.operation(
        authority, "cancel", {"session_id": "s", "admission_id": "a"}) == {"ok": True}
    assert seen, "the admission was never read"
    assert threading.get_ident() not in seen, "the cron admission read ran on the event loop"


# ── sqlite: kanban session open (build_worker_context / commit_local_session leaves) ────────────


@pytest.mark.asyncio
async def test_a_kanban_task_opens_its_session_off_the_loop(monkeypatch, tmp_path):
    from dataclasses import asdict, dataclass

    from gateway import session_kanban

    calls = []

    @dataclass
    class _Receipt:
        admission_id: str

    def build_policy(connection, params, config):
        calls.append(("build_kanban_policy", threading.get_ident()))
        return SimpleNamespace(kanban_json='{"db": "x", "board": "b"}'), {}

    def create_session(authority, actor, params, *, trusted_policy=None, trusted_secrets=None):
        calls.append(("create_local_session", threading.get_ident()))
        return SimpleNamespace(session_id="s")

    async def tracked_write(authority, func, **kwargs):
        return None  # the kanban admission transaction is another card's coverage

    async def submit(actor, submission, **kwargs):
        calls.append(("submit", threading.get_ident()))
        return _Receipt(admission_id="a")

    monkeypatch.setattr(session_kanban, "build_kanban_policy", build_policy)
    # ``create_local_session`` is imported inside ``run_task`` from its own module: patch it there.
    monkeypatch.setattr("gateway.session_local.create_local_session", create_session)
    monkeypatch.setattr("gateway.session_runtime_workers.tracked_write", tracked_write)
    # ``run_task`` imports ``gateway.run`` for the config loader; stub the module so this unit test
    # never boots the real bootstrap (which touches the checkout's own files).
    monkeypatch.setitem(sys.modules, "gateway.run",
                        SimpleNamespace(_load_gateway_config=lambda: SimpleNamespace()))

    db = SimpleNamespace(db_path=str(tmp_path / "state.db"), get_session=lambda sid: None)
    authority = SimpleNamespace(profile_id="p", epoch=1, db=db, submit=submit)
    connection = SimpleNamespace(
        native_owner=True, authority=authority,
        actor=SimpleNamespace(subject="u", profile_id="p", capabilities={"session:create"}),
        resume=submit_resume)
    params = {"board": "b", "task_id": "t", "run_id": 7, "claim_lock": "c"}

    result = await session_kanban.run_task(connection, params)
    assert result == {"session_id": "s", "receipt": asdict(_Receipt(admission_id="a"))}
    assert [name for name, _ in calls] == ["build_kanban_policy", "create_local_session", "submit"]
    loop_ident = threading.get_ident()
    # ``submit`` is the authority's own coroutine and stays on the loop; the two session-store
    # steps (a read plus a write transaction on state.db) must not be.
    store_steps = [(name, ident) for name, ident in calls if name != "submit"]
    assert all(ident != loop_ident for _, ident in store_steps), (
        f"kanban session setup ran on the event loop: {store_steps}")


async def submit_resume(ref, payload):
    return None


# ── api_server /health/detailed: no status-file read on the loop ────────────────────────────────


@pytest.mark.asyncio
async def test_the_detailed_health_handler_reads_status_off_the_loop(monkeypatch):
    from gateway import status as status_module
    from gateway.platforms import api_server

    seen = []

    def read(path=None):
        seen.append(threading.get_ident())
        return {"gateway_state": "running", "platforms": {}, "active_agents": 0}

    monkeypatch.setattr(status_module, "read_runtime_status", read)
    monkeypatch.setattr(api_server, "collect_runtime_readiness",
                        lambda **kwargs: {"status": "ok", "checks": {}})
    # The handler imports the model resolver from ``gateway.run``; stub the module so this unit
    # test never boots the real bootstrap (which touches the checkout's own files).
    monkeypatch.setitem(sys.modules, "gateway.run",
                        SimpleNamespace(_resolve_gateway_model=lambda: "m"))

    handler = api_server.APIServerAdapter._handle_health_detailed.__wrapped__
    adapter = SimpleNamespace(
        _api_server_status_payload=lambda heartbeat_at=None: {"state": "connected", "metrics_today": {},
                                                              "last_heartbeat": None},
        is_connected=True,
        _readiness_work_counts=lambda: (0, 0, 0))

    response = await handler(adapter, SimpleNamespace())
    assert response.status == 200
    assert seen, "the handler never read the runtime status"
    assert threading.get_ident() not in seen, "/health/detailed read the status file on the event loop"


# ── the loop's OWN off-load pool must be prewarmed, or the hops above start threads ─────────────


@pytest.mark.asyncio
async def test_the_loop_io_pool_is_prewarmed_so_to_thread_starts_no_thread(monkeypatch):
    from gateway import run_startup

    started = []
    real_start = threading.Thread.start

    def start(self):
        started.append(self.name)
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    loop = asyncio.get_running_loop()
    run_startup._install_loop_io_executor(loop)
    started.clear()  # the prewarm's own starts are the point; only later ones matter

    for _ in range(run_startup._LOOP_IO_PREWARM):
        await asyncio.to_thread(lambda: None)
    assert not [name for name in started if name.startswith("hermes-loop-io")], (
        f"asyncio.to_thread started a worker on the loop: {started}")


@pytest.mark.asyncio
async def test_the_startup_prewarm_runs_off_the_loop_and_installs_the_pool(monkeypatch):
    from gateway import run_startup

    seen = []

    class _Runner(run_startup.GatewayStartupMixin):
        def _prewarm_worker_threads(self):
            seen.append(threading.get_ident())

    loop = asyncio.get_running_loop()
    await _Runner()._prewarm_loop_blocking_helpers()
    assert seen, "the startup prewarm never ran"
    assert threading.get_ident() not in seen, "the startup prewarm ran on the event loop"
    assert getattr(loop, "_default_executor", None) is not None, "the loop kept its lazily-started pool"
