"""A Kanban worker outlasts its owner's restart instead of spending the card's life on it.

2026-11-17: developer-bangioi's owner was dead for 453 s (12:00:38 watchdog exit -> 12:08:11
replacement) against ``OWNER_WAIT_S = 180``, so every worker it served gave up 186-248 s in — roughly
three minutes before a replacement existed — and each exit was booked as a card crash. A worker
waiting on a ``starting``/``draining`` owner is waiting out a RESTART, which can legitimately outlast
the silence budget; every other restartable failure keeps the old budget.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_worker_client as worker
from hermes_cli.gateway_client import GatewayUnavailableError

GUE = GatewayUnavailableError


def test_the_revival_budget_covers_the_worst_measured_downtime():
    assert worker.OWNER_REVIVAL_WAIT_S > 453.0, (
        "the revival budget must cover the 453 s owner downtime measured on 2026-11-17")
    assert worker.OWNER_REVIVAL_WAIT_S > worker.OWNER_WAIT_S


@pytest.mark.parametrize("state", ["starting", "draining"])
def test_a_restarting_owner_gets_the_revival_budget(state):
    assert worker._owner_budget(GUE("Gateway starting: deadline", state, "deadline")) == worker.OWNER_REVIVAL_WAIT_S


def test_any_other_restartable_failure_keeps_the_silence_budget():
    assert worker._owner_budget(RuntimeError("connection reset")) == worker.OWNER_WAIT_S
    assert worker._owner_budget(GUE("Gateway inaccessible", "inaccessible", "runtime_start_failed")) \
        == worker.OWNER_WAIT_S


class _DeadThenUpOwner:
    """The owner is refused/gone for the first ``down_s`` seconds, then answers a receipt."""

    def __init__(self, down_s: float, clock: list):
        self.down_s, self.clock, self.dials = down_s, clock, 0

    def connect(self):
        owner = self

        class _Client:
            def __init__(self) -> None:
                # Built inside the running loop, like the real client's queue.
                self.events = asyncio.Queue()

            async def rpc(self, method, **params):
                if method == "kanban.run":
                    return {"session_id": "s1",
                            "receipt": {"status": "terminal", "admission_id": "a1"}}
                # ``prompt.receipt`` is polled until ``terminal``/``unknown``: any other status
                # makes the worker poll for ever (that is the hang this fake used to cause).
                return {"status": "terminal"}

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _connect(*_, **__):
            owner.dials += 1
            if owner.clock[0] < owner.down_s:
                raise GUE("Gateway starting: deadline", "starting", "deadline")
            yield _Client()

        return _connect


@pytest.fixture
def simulated_clock(monkeypatch):
    """A simulated clock for the worker module ONLY.

    Patching the global ``asyncio.sleep`` (as the restart-wait test does) also disables the loop's
    own scheduling, and ``asyncio.to_thread`` in the claim path then never completes — the scenario
    hangs instead of failing. Only this module's handle is replaced.
    """
    clock = [0.0]

    async def _sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(worker, "asyncio", SimpleNamespace(
        sleep=_sleep, create_task=asyncio.create_task, to_thread=asyncio.to_thread))
    return clock


@pytest.mark.asyncio
async def test_a_worker_survives_a_restart_longer_than_the_old_silence_budget(monkeypatch, tmp_path,
                                                                              simulated_clock, capsys):
    """230 s of downtime: past the old 180 s budget, well inside the revival budget."""
    from hermes_cli import gateway_client
    owner = _DeadThenUpOwner(down_s=230.0, clock=simulated_clock)
    monkeypatch.setattr(gateway_client, "connect_gateway", owner.connect())
    monkeypatch.setattr(worker, "_exit_code", lambda receipt, params, db: 0)

    code = await worker.run({"task_id": "t1"}, str(tmp_path / "kanban.db"))

    assert code == 0
    assert simulated_clock[0] >= 230.0
    err = capsys.readouterr().err
    assert "waiting up to 900s" in err, err
    assert "the worker is not at fault" in err
    assert err.count("Kanban owner restarting") == 1, "the revival notice is said once, not per redial"


@pytest.mark.asyncio
async def test_an_owner_that_never_returns_still_fails_the_worker_at_the_bounded_budget(
        monkeypatch, tmp_path, simulated_clock):
    from hermes_cli import gateway_client
    owner = _DeadThenUpOwner(down_s=10_000.0, clock=simulated_clock)
    monkeypatch.setattr(gateway_client, "connect_gateway", owner.connect())

    with pytest.raises(gateway_client.GatewayClientError, match="Kanban owner silent"):
        await worker.run({"task_id": "t1"}, str(tmp_path / "kanban.db"))

    assert worker.OWNER_REVIVAL_WAIT_S <= simulated_clock[0] < worker.OWNER_REVIVAL_WAIT_S + 60
