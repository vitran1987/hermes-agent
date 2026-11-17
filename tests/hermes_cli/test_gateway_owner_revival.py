"""An on-demand owner that dies must come back — and a stalled "starting" must not wait forever.

2026-11-17 12:00 ICT: the developer-bangioi owner was killed by the loop watchdog and stayed dead
for 453 s. Every client of that profile asked ``ensure_gateway_runtime`` for an owner, was told
``starting`` (the runtime lock was held by a live PID whose control plane answered every session
upgrade with HTTP 500), waited out its whole deadline and returned ``Gateway starting: deadline``.
Nine cards crashed with that reason; nothing ever reclaimed the holder, because the only reclaim
path (``replace_incompatible_owner``) refuses when ``identify`` cannot be pinned.

These tests pin the decisions that fix it:

  * a lock record naming a DEAD process is a leftover, not a start in progress;
  * a holder that reports ``starting`` without being launched by this call is reclaimed after the
    startup grace + a whole window of no CPU progress, verified from its lock record, and the
    replacement is started here;
  * a holder that is still BOOTING (its CPU keeps growing) is slow, not hung: never reclaimed, even
    past the grace, however long the boot takes on a CPU-starved host (PR #18 review);
  * a start THIS call launched is never reclaimed, and an unverifiable holder is refused with a
    repair command instead of being killed on a guess.
"""

from __future__ import annotations

import time
import types

import pytest

from hermes_cli import gateway_runtime, gateway_runtime_discovery
import gateway.status as gateway_status


# --- the discovery verdict: "starting" must mean someone is actually coming ---------------------

@pytest.fixture
def held_lock(monkeypatch):
    """A lock that reads as held, with the record/liveness answers a test wants."""
    def install(*, record, live):
        monkeypatch.setattr(gateway_status, "_is_gateway_runtime_lock_active_strict", lambda lock: True)
        monkeypatch.setattr(gateway_status, "_read_gateway_lock_record", lambda lock: record)
        monkeypatch.setattr(gateway_status, "_pid_from_record", lambda rec, key="pid": (rec or {}).get(key))
        monkeypatch.setattr(gateway_status, "_live_pid_from_record", lambda rec: live)
    return install


def test_a_lock_record_naming_a_dead_process_is_absent(held_lock, tmp_path):
    held_lock(record={"pid": 999999, "kind": "gateway"}, live=None)
    home = tmp_path / "profile"
    home.mkdir()

    assert gateway_runtime_discovery.missing_owner_state(home) == "absent"


def test_a_lock_record_naming_a_live_process_is_still_starting(held_lock, tmp_path):
    held_lock(record={"pid": 4242, "kind": "gateway"}, live=4242)
    home = tmp_path / "profile"
    home.mkdir()

    assert gateway_runtime_discovery.missing_owner_state(home) == "starting"


def test_an_unpublished_reservation_is_not_a_stale_artifact(held_lock, tmp_path):
    """A boot that has not published its PID yet legitimately holds the lock: never race it."""
    held_lock(record=None, live=None)
    home = tmp_path / "profile"
    home.mkdir()

    assert gateway_runtime_discovery.missing_owner_state(home) == "starting"


# --- the ensure loop: reclaim a stalled holder, start the replacement ---------------------------

@pytest.fixture
def fast_runway(monkeypatch):
    """A real StartRunway on a sub-second scale, with no cheap progress sampling."""
    from hermes_cli import gateway_runtime_progress as progress
    monkeypatch.setattr(gateway_runtime, "DEFAULT_ENSURE_TIMEOUT", 1.0)
    monkeypatch.setattr(progress, "PROGRESS_WINDOW_S", 0.2)
    monkeypatch.setattr(progress, "_SAMPLE_EVERY_S", 0.05)
    monkeypatch.setattr(progress, "_LEAD_S", 1.0)
    monkeypatch.setattr(progress, "MAX_ADAPTIVE_TIMEOUT", 5.0)
    monkeypatch.setattr(progress, "_starting_owner_pid", lambda homes, child: None)
    monkeypatch.setattr(gateway_runtime, "_update_fenced", lambda homes: False)
    monkeypatch.setattr(gateway_runtime, "_starting_multiplexer_root", lambda home: None)
    monkeypatch.setattr(gateway_runtime, "_standalone_multiplexer_reason", lambda root: None)
    return gateway_runtime


def _holder(monkeypatch, *, pid: int = 4242, cpu=lambda elapsed: 2.0) -> None:
    """The lock holder as the progress sampler sees it: *pid*, and its CPU seconds at *elapsed*.

    ``cpu`` flat is a WEDGED holder (alive, no progress); growing is a boot in progress.
    """
    from hermes_cli import gateway_runtime_progress as progress
    t0 = time.monotonic()
    monkeypatch.setattr(progress, "_starting_owner_pid", lambda homes, child: pid)
    monkeypatch.setattr(progress, "_cpu_seconds", lambda sampled: cpu(time.monotonic() - t0))


def _aged_lock(home, *, age_s: float) -> None:
    """A lock record whose holder published it ``age_s`` seconds ago."""
    import os
    home.mkdir(parents=True, exist_ok=True)
    lock = home / "gateway.lock"
    lock.write_text('{"pid": 4242, "kind": "gateway"}', encoding="utf-8")
    when = time.time() - age_s
    os.utime(lock, (when, when))


def _past_deadline(deadline: float) -> None:
    """The real observer raises ``TimeoutError`` once its budget is spent; a fake must too, or the
    ensure loop spins on a deadline nothing is checking."""
    if time.monotonic() >= deadline:
        raise TimeoutError


def test_a_stalled_starting_holder_is_reclaimed_and_the_client_is_served(fast_runway, tmp_path, monkeypatch):
    from hermes_cli import gateway_runtime_replace as replace

    _holder(monkeypatch)  # alive, holding the lock, accruing no CPU: the 2026-11-17 ABI-500 daemon
    state = {"reclaimed": False}
    reclaimed: list = []

    def observe(home, deadline, replaced):
        _past_deadline(deadline)
        return gateway_runtime.GatewayDiscovery("ready" if state["reclaimed"] else "starting")

    def reclaim(home, *, deadline):
        reclaimed.append(home)
        state["reclaimed"] = True
        return None  # verified and stopped: the caller starts the replacement

    monkeypatch.setattr(gateway_runtime, "_observe_replacing_stale", observe)
    monkeypatch.setattr(replace, "replace_unresponsive_owner", reclaim)

    result = gateway_runtime.ensure_gateway_runtime(tmp_path, stalled_start_grace_s=0.0)

    assert result.state == "ready"
    assert len(reclaimed) == 1, "the unresponsive holder must be reclaimed exactly once"


def test_a_holder_that_keeps_booting_is_never_reclaimed_even_past_the_grace(fast_runway, tmp_path, monkeypatch):
    """PR #18: a sibling client's grace stopped an owner that was alive and still booting.

    A boot publishes its lock record at ``run_bootstrap`` and only then binds the control socket, so
    a legal foreign client sees ``starting`` for as long as the boot takes — ~160 s of wall time on
    a CPU-starved host, against a 20 s grace. The reclaim must wait for that boot: the same CPU
    evidence that keeps a start of this call's own alive (:class:`StartRunway`) protects it.
    """
    from hermes_cli import gateway_runtime_replace as replace

    booted_after = 3.0
    _holder(monkeypatch, cpu=lambda elapsed: 5.0 * elapsed)  # booting: CPU grows, no socket yet
    killed: list = []
    t0 = time.monotonic()

    def observe(home, deadline, replaced):
        _past_deadline(deadline)
        if time.monotonic() - t0 >= booted_after:
            return gateway_runtime.GatewayDiscovery("ready")
        return gateway_runtime.GatewayDiscovery("starting")

    monkeypatch.setattr(gateway_runtime, "_observe_replacing_stale", observe)
    monkeypatch.setattr(replace, "replace_unresponsive_owner",
                        lambda home, *, deadline: killed.append(home) or "owner_unverified")

    result = gateway_runtime.ensure_gateway_runtime(tmp_path, stalled_start_grace_s=0.5)

    assert killed == [], "a foreign owner that is still booting must never be stopped"
    assert result.state == "ready", "the boot must be awaited, not killed, and then served"


def test_a_reservation_that_published_nothing_is_never_reclaimed(fast_runway, tmp_path, monkeypatch):
    """No record and no pid: a boot too young to judge. Waiting is the only safe verdict."""
    from hermes_cli import gateway_runtime_replace as replace

    killed: list = []
    monkeypatch.setattr(gateway_runtime, "_observe_replacing_stale",
                        lambda home, deadline, replaced: (_past_deadline(deadline),
                                                          gateway_runtime.GatewayDiscovery("starting"))[1])
    monkeypatch.setattr(replace, "replace_unresponsive_owner",
                        lambda home, *, deadline: killed.append(home))

    result = gateway_runtime.ensure_gateway_runtime(tmp_path, stalled_start_grace_s=0.0)

    assert killed == []
    assert (result.state, result.reason_code) == ("starting", "deadline")


def test_a_holder_that_cannot_be_verified_is_refused_with_a_repair_command(fast_runway, tmp_path, monkeypatch):
    from hermes_cli import gateway_runtime_replace as replace

    _aged_lock(tmp_path, age_s=600.0)  # a record older than any boot, and no CPU this call can read
    monkeypatch.setattr(gateway_runtime, "_observe_replacing_stale",
                        lambda home, deadline, replaced: gateway_runtime.GatewayDiscovery("starting"))
    monkeypatch.setattr(replace, "replace_unresponsive_owner",
                        lambda home, *, deadline: "owner_unverified")

    result = gateway_runtime.ensure_gateway_runtime(tmp_path, stalled_start_grace_s=0.0)

    assert (result.state, result.reason_code) == ("inaccessible", "owner_unresponsive")
    assert "hermes gateway stop" in (result.detail or "")
    assert "verified" in (result.detail or "")



def test_a_start_this_call_launched_is_never_reclaimed(fast_runway, tmp_path, monkeypatch):
    """A cold boot takes tens of seconds: its own ``starting`` must not look like a stalled holder."""
    from hermes_cli import gateway_runtime_multiplex as multiplex
    from hermes_cli import gateway_runtime_replace as replace
    from hermes_cli import gateway_runtime_service as service
    from hermes_cli import gateway_runtime_start as start

    reclaimed: list = []
    spawns: list = []
    probes = {"n": 0}

    def observe(home, deadline, replaced):
        _past_deadline(deadline)
        probes["n"] += 1
        return gateway_runtime.GatewayDiscovery("absent" if probes["n"] == 1 else "starting")

    def spawn(target, *, deadline, idle_exit=False):
        spawns.append(target)
        return types.SimpleNamespace(poll=lambda: None, pid=4242)

    monkeypatch.setattr(gateway_runtime, "_observe_replacing_stale", observe)
    monkeypatch.setattr(gateway_runtime, "discover_gateway_endpoint",
                        lambda home, timeout: gateway_runtime.GatewayDiscovery("absent"))
    monkeypatch.setattr(multiplex, "multiplexer_serves_home", lambda home: None)
    monkeypatch.setattr(multiplex, "implied_host_root", lambda home: None)
    monkeypatch.setattr(service, "discover_existing_gateway_service", lambda *a, **k: None)
    monkeypatch.setattr(start, "spawn_unmanaged_gateway", spawn)
    monkeypatch.setattr(start, "stdio_log_size", lambda home: 0)
    monkeypatch.setattr(replace, "replace_unresponsive_owner",
                        lambda home, *, deadline: reclaimed.append(home))

    result = gateway_runtime.ensure_gateway_runtime(tmp_path, stalled_start_grace_s=0.0)

    assert spawns, "the absent home must have been started by this call"
    assert reclaimed == [], "this call's own child is a start in progress, not a stalled holder"
    assert (result.state, result.reason_code) == ("starting", "deadline")
