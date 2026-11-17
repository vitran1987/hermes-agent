"""A starved event loop is not a wedged one — and a wedged one still gets replaced.

2026-11-17 12:00 ICT: a CI runner at 204% CPU with the host at 88.9% memory left three 10 s probes
unanswered, the watchdog exited the developer-bangioi gateway with code 75, and the nine cards that
gateway was serving crashed with it. The watchdog could not tell "this loop is blocked" from "this
host gave the loop no CPU", so it killed a gateway that was merely starved.

Both paths are driven here with an injected headroom verdict, so the test does not depend on the
machine it runs on.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import types
from unittest.mock import MagicMock, patch

import gateway.shutdown_watchdog as watchdog


class _SilentLoop:
    """An event loop that never runs a callback: the probe is scheduled into a void."""

    def call_soon_threadsafe(self, _callback) -> None:  # noqa: D102 - the point of the fake
        return None


SATURATED = {"saturated": True, "reasons": ["memory 89% used, 11% available"], "cpu_load_per_core": 0.6,
             "mem_available_frac": 0.11}
IDLE = {"saturated": False, "reasons": [], "cpu_load_per_core": 0.1, "mem_available_frac": 0.55}


def _run_watchdog(monkeypatch, *, sampler, seconds: float = 0.45, **kwargs):
    """Drive one watchdog run; returns (exit codes, critical calls, warning texts)."""
    exit_codes: list[int] = []
    criticals: list[str] = []
    warnings: list[str] = []
    handle_ref: dict = {}
    ready = threading.Event()

    def hard_exit(code: int) -> None:
        exit_codes.append(code)
        assert ready.wait(timeout=2.0)
        handle_ref["handle"].stop()  # a patched exit returns: stop the loop like a real one

    with patch.object(watchdog, "_hard_exit", side_effect=hard_exit), \
            patch.object(watchdog, "_mark_exited_quietly"), \
            patch.object(watchdog, "_write_watchdog_dump"), \
            patch.object(watchdog.logger, "critical",
                         side_effect=lambda msg, *a, **k: criticals.append(msg % a)), \
            patch.object(watchdog.logger, "warning",
                         side_effect=lambda msg, *a, **k: warnings.append(msg % a)):
        handle = watchdog.start_loop_liveness_watchdog(
            _SilentLoop(), probe_interval=0.01, probe_timeout=0.01, max_strikes=2,
            pressure_sampler=lambda: sampler, **kwargs)
        handle_ref["handle"] = handle
        ready.set()
        time.sleep(seconds)
        handle.stop()
        handle.join(timeout=2.0)
    assert not handle.is_alive()
    return exit_codes, criticals, warnings


def test_starved_loop_under_host_pressure_is_not_killed(monkeypatch):
    """Criterion 1: saturation extends the deadline and names the environmental cause."""
    exit_codes, criticals, warnings = _run_watchdog(monkeypatch, sampler=SATURATED)

    assert exit_codes == [], "a starved gateway must not be exited"
    assert criticals == []
    starved = [w for w in warnings if "STARVED, not wedged" in w]
    assert starved, f"the log must say the loop was starved, got: {warnings}"
    assert "memory 89% used" in starved[0], "the environmental cause must be named"
    assert "extending the probe deadline" in starved[0]


def test_wedged_loop_with_host_headroom_still_exits_with_the_same_message(monkeypatch):
    """Criterion 2: a genuinely blocked loop still dies, with the error string callers key on."""
    exit_codes, criticals, _ = _run_watchdog(monkeypatch, sampler=IDLE)

    assert exit_codes == [watchdog.GATEWAY_SERVICE_RESTART_EXIT_CODE]
    assert criticals, "the wedge exit must still be logged"
    assert ("Gateway event loop missed 2 consecutive liveness probes; dumping all thread stacks "
            "and exiting with code 75 so the service supervisor can restart it.") == criticals[0]


def test_saturation_ceiling_keeps_an_unresponsive_gateway_replaceable(monkeypatch):
    """The watchdog is not weakened into never firing: sustained saturation is bounded."""
    exit_codes, _, warnings = _run_watchdog(
        monkeypatch, sampler=SATURATED, seconds=0.9, starvation_ceiling_s=0.05)

    assert exit_codes == [watchdog.GATEWAY_SERVICE_RESTART_EXIT_CODE]
    assert any("ceiling" in w for w in warnings), "the ceiling exit must say why it stopped waiting"


def test_pressure_sampler_that_raises_is_not_evidence_of_pressure(monkeypatch):
    """An unreadable host is unknown headroom, never a reason to keep a wedged gateway alive."""

    def boom():
        raise OSError("no psutil here")

    exit_codes, _, _ = _run_watchdog(monkeypatch, sampler=boom)

    assert exit_codes == [watchdog.GATEWAY_SERVICE_RESTART_EXIT_CODE]


def test_sample_host_pressure_reads_memory_and_cpu_headroom(monkeypatch):
    def sample(*, total: int, available: int, load: float | None = 0.02):
        fake = types.SimpleNamespace(
            virtual_memory=lambda: types.SimpleNamespace(total=total, available=available),
            cpu_percent=lambda interval=None: 0.0,
        )
        if load is not None:
            fake.getloadavg = lambda: (load, load, load)
        monkeypatch.setitem(sys.modules, "psutil", fake)
        return watchdog.sample_host_pressure()

    saturated = sample(total=1000, available=110)  # the incident's 11% available
    assert saturated["saturated"] is True
    assert any("memory" in r for r in saturated["reasons"]), saturated

    headroom = sample(total=1000, available=550)
    assert headroom["saturated"] is False, headroom

    busy_cpu = sample(total=1000, available=550, load=40.0)
    assert busy_cpu["saturated"] is True
    assert any("CPU load" in r for r in busy_cpu["reasons"]), busy_cpu


def test_sample_host_pressure_without_psutil_reports_no_pressure(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)

    verdict = watchdog.sample_host_pressure()

    assert verdict["saturated"] is False and verdict["reasons"] == []
