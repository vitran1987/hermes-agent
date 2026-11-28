"""The claim/verify legs of every worker RPC route must not run on the gateway event loop.

A synchronous ``psutil`` probe or SQLite read inside ``_claim``/``_verify`` stalls the
loop while several workers save at once. A worker then misses its hard-coded 20 s
``worker.persist`` reply window (``agent/runtime_session_store.py``, ``link.receive(20)``),
latches ``worker_receipt_timeout``, exits, and the card dies with
``Kanban owner refused: Kanban execution unknown; retained by owner`` (#145022).

Ported from ``NousResearch/hermes-agent#155874``. That file cannot be used verbatim: it
imports ``hermes_state_runtime.RuntimeStoreError`` and monkeypatches module-level
``register_worker_execution`` / ``adopt_worker_execution`` / ``mutate_worker_execution``,
none of which exist in this tree. Here the refusal is
``persistence.core.errors.AuthorityRefusal`` and the writes go through
``authority_persistence(authority).aio.worker_executions`` and
``...worker_executions.persist_operation``. The contracts are the upstream contracts,
expressed against this tree's shape.

Two contracts:

* ``_claim`` and ``_verify`` never execute on the event-loop thread, on all three routes
  (``worker_request``, ``worker_child_request``, ``worker_delegation_request``), and the
  loop keeps serving other work while a leg is parked.
* Moving the legs off-loop inserts yield points between the ``require_serving`` check at
  the top of each route and the write it guards, so each route re-checks serving after
  the yield. A retirement that lands while a leg is parked must still refuse the write.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
from types import SimpleNamespace

import pytest

from gateway import session_worker, session_worker_child, session_worker_delegation
from persistence.core.errors import AuthorityRefusal

_SCOPE = session_worker._SCOPE


@dataclasses.dataclass
class _Row:
    """Stand-in for an execution row: ``worker_request`` answers ``asdict(row)``."""

    kind: str = "compute"


class _FakeActor:
    profile_id = "profile"
    subject = "worker-subject"
    capabilities = frozenset({"worker:adopt"})


class _Child:
    session_id = "child-session"
    execution_id = "child-exec"
    generation = 1


class _SyncExecutions:
    """The synchronous capability the routes reach off-loop."""

    def __init__(self, writes):
        self._writes = writes

    def persist_operation(self, **kwargs):
        self._writes.append("persist")
        return {"operation": kwargs.get("operation")}

    def reserve_child(self, **kwargs):
        self._writes.append("reserve_child")
        return SimpleNamespace(child=_Child(), refused=None)


class _AsyncExecutions:
    """The ``.aio`` capability ``worker_request`` awaits for register/adopt."""

    def __init__(self, writes):
        self._writes = writes

    async def register(self, **kwargs):
        self._writes.append("register")
        return _Row()

    async def adopt(self, **kwargs):
        self._writes.append("adopt")
        return _Row(kind="adopted")


class _FakeAuthority:
    profile_id = "profile"
    epoch = 7
    db = object()

    def __init__(self):
        self.retired = False
        self.writes: list[str] = []
        self.persistence = SimpleNamespace(
            worker_executions=_SyncExecutions(self.writes),
            aio=SimpleNamespace(worker_executions=_AsyncExecutions(self.writes)),
        )

    # surfaces the three routes touch on the authority itself
    def _require_admission_open(self):
        pass

    def authorize(self, actor, ref, capability):
        pass

    def wake_after_worker(self, session_id):
        pass

    def logical_owner(self, session_id):
        return session_id


class _FakeConnection:
    def __init__(self, authority=None):
        self.authority = authority if authority is not None else _FakeAuthority()
        self.actor = _FakeActor()


def _scope_params(operation=None):
    """A ``_SCOPE`` payload, plus the extras ``operation`` requires (None = adopt)."""
    params = {
        "profile_id": "profile",
        "session_id": "sid",
        "execution_id": "exec",
        "generation": 1,
        "pid": 1,
        "birth": 1,
        "secret": "s" * 32,
    }
    assert set(params) == _SCOPE
    if operation == "register":
        params["kind"] = "compute"
    elif operation not in (None, "adopt"):
        params.update(
            epoch=1,
            sequence=1,
            operation=operation,
            payload={},
        )
    return params


_ROUTE_PARAMS = {
    # worker_request adopt: the shortest path that runs BOTH legs
    "worker_request": lambda: _scope_params(None),
    "child": lambda: _scope_params("child.reserve"),
    "delegation": lambda: _scope_params("delegation.read"),
}


def _drive(route, connection, params, operation="adopt"):
    if route == "worker_request":
        return session_worker.worker_request(connection, "ref", params, operation=operation)
    if route == "child":
        return session_worker_child.worker_child_request(connection, "ref", params)
    return session_worker_delegation.worker_delegation_request(connection, "ref", params)


def _stub_route_writes(monkeypatch):
    """Silence the write half of every route so only the authentication legs are under test."""
    monkeypatch.setattr(
        session_worker_child,
        "delegation_limits",
        lambda *args, **kwargs: (5, 1),
    )
    monkeypatch.setattr(
        session_worker_delegation,
        "persist_worker_delegation",
        lambda *args, **kwargs: {"state": "ok"},
    )


def _record_leg(name, seen):
    def run(*args, **kwargs):
        seen[name] = threading.get_ident()
        return "claim" if name == "claim" else None

    return run


# --------------------------------------------------------------------------------------
# Contract 1: both legs run off the event loop, on all three routes.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_and_verify_run_off_the_event_loop(monkeypatch):
    """One connection, both legs, and neither one lands on the loop thread."""
    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}
    _stub_route_writes(monkeypatch)
    monkeypatch.setattr(session_worker, "_claim", _record_leg("claim", seen))
    monkeypatch.setattr(session_worker, "_verify", _record_leg("verify", seen))

    result = await _drive(
        "worker_request", _FakeConnection(), _ROUTE_PARAMS["worker_request"]()
    )

    assert result == {"kind": "adopted"}
    assert seen["claim"] != loop_thread, "_claim blocked the event loop"
    assert seen["verify"] != loop_thread, "_verify blocked the event loop"


@pytest.mark.asyncio
async def test_child_route_runs_claim_and_verify_off_the_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}
    _stub_route_writes(monkeypatch)
    monkeypatch.setattr(session_worker, "_claim", _record_leg("claim", seen))
    monkeypatch.setattr(session_worker, "_verify", _record_leg("verify", seen))

    await _drive("child", _FakeConnection(), _ROUTE_PARAMS["child"]())

    assert seen["claim"] != loop_thread, "child _claim blocked the event loop"
    assert seen["verify"] != loop_thread, "child _verify blocked the event loop"


@pytest.mark.asyncio
async def test_delegation_route_runs_claim_and_verify_off_the_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}
    _stub_route_writes(monkeypatch)
    monkeypatch.setattr(session_worker, "_claim", _record_leg("claim", seen))
    monkeypatch.setattr(session_worker, "_verify", _record_leg("verify", seen))

    await _drive("delegation", _FakeConnection(), _ROUTE_PARAMS["delegation"]())

    assert seen["claim"] != loop_thread, "delegation _claim blocked the event loop"
    assert seen["verify"] != loop_thread, "delegation _verify blocked the event loop"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["worker_request", "child", "delegation"])
async def test_the_loop_keeps_serving_while_claim_is_parked(monkeypatch, route):
    """The positive control: a parked claim must not stop the loop from running tasks.

    A thread-identity assertion alone would pass on a blocking call that happened to be
    executed in a thread; this one requires the loop to keep scheduling while the leg is
    provably still inside the worker thread.
    """
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()

    def parked_claim(*args, **kwargs):
        assert threading.get_ident() != loop_thread, "claim ran on the event loop"
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(30), "claim was never released"
        return "claim"

    _stub_route_writes(monkeypatch)
    monkeypatch.setattr(session_worker, "_claim", parked_claim)
    monkeypatch.setattr(session_worker, "_verify", lambda *a, **k: None)

    request = asyncio.create_task(
        _drive(route, _FakeConnection(), _ROUTE_PARAMS[route]())
    )
    ticks = 0
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        for _ in range(25):
            await asyncio.sleep(0)
            ticks += 1
    finally:
        release.set()
        await asyncio.wait_for(request, timeout=15)
    assert ticks == 25, "the event loop did not keep scheduling while the claim was parked"


# --------------------------------------------------------------------------------------
# Contract 2: a retirement during a parked leg still refuses the write.
# --------------------------------------------------------------------------------------


async def _park_and_retire(monkeypatch, route, params, blocked_leg, operation="adopt"):
    """Park ``blocked_leg`` in its worker thread, retire the authority, release, return the task."""
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()

    def leg(name):
        def run(*args, **kwargs):
            assert threading.get_ident() != loop_thread, f"{name} blocked the event loop"
            if name == blocked_leg:
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(30), f"{name} was never released"
            return "claim" if name == "claim" else None

        return run

    _stub_route_writes(monkeypatch)
    monkeypatch.setattr(session_worker, "_claim", leg("claim"))
    monkeypatch.setattr(session_worker, "_verify", leg("verify"))

    connection = _FakeConnection()
    request = asyncio.create_task(_drive(route, connection, params, operation))
    entered_task = asyncio.create_task(entered.wait())
    done: set = set()
    try:
        done, _ = await asyncio.wait(
            {entered_task, request}, timeout=10, return_when=asyncio.FIRST_COMPLETED
        )
        if request in done:
            await request  # surfaces the real failure instead of a bare timeout
        assert entered_task in done, f"{blocked_leg} did not start"
        connection.authority.retired = True
    finally:
        release.set()
        entered_task.cancel()
        await asyncio.gather(entered_task, return_exceptions=True)
        if entered_task not in done:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
    return connection, request


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,blocked_leg",
    [("register", "claim"), ("adopt", "claim"), ("adopt", "verify"),
     ("persist", "claim"), ("persist", "verify")],
)
async def test_retirement_during_authentication_refuses_the_worker_write(
    monkeypatch, operation, blocked_leg
):
    """A retirement during either leg must refuse register/adopt/persist."""
    connection, request = await _park_and_retire(
        monkeypatch, "worker_request", _scope_params(operation), blocked_leg, operation
    )
    with pytest.raises(AuthorityRefusal) as excinfo:
        await asyncio.wait_for(request, timeout=10)
    assert excinfo.value.reason == "runtime_retired"
    assert connection.authority.writes == [], (
        f"a retired authority accepted a {operation} write: {connection.authority.writes}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_leg", ["claim", "verify"])
async def test_retirement_during_child_authentication_refuses_the_reservation(
    monkeypatch, blocked_leg
):
    connection, request = await _park_and_retire(
        monkeypatch, "child", _ROUTE_PARAMS["child"](), blocked_leg
    )
    with pytest.raises(AuthorityRefusal) as excinfo:
        await asyncio.wait_for(request, timeout=10)
    assert excinfo.value.reason == "runtime_retired"
    assert "reserve_child" not in connection.authority.writes


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_leg", ["claim", "verify"])
async def test_retirement_during_delegation_authentication_refuses_the_write(
    monkeypatch, blocked_leg
):
    connection, request = await _park_and_retire(
        monkeypatch, "delegation", _ROUTE_PARAMS["delegation"](), blocked_leg
    )
    with pytest.raises(AuthorityRefusal) as excinfo:
        await asyncio.wait_for(request, timeout=10)
    assert excinfo.value.reason == "runtime_retired"
    assert connection.authority.writes == []
