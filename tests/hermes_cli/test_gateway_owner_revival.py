"""An on-demand owner that dies must come back — and a stalled "starting" must not wait forever.

2026-11-17 12:00 ICT: the developer-bangioi owner was killed by the loop watchdog and stayed dead
for 453 s. Every client of that profile asked ``ensure_gateway_runtime`` for an owner, was told
``starting`` (the runtime lock was held by a live PID whose control plane answered every session
upgrade with HTTP 500), waited out its whole deadline and returned ``Gateway starting: deadline``.
Nine cards crashed with that reason; nothing ever reclaimed the holder, because the only reclaim
path (``replace_incompatible_owner``) refuses when ``identify`` cannot be pinned.

These tests pin the three decisions that fix it:

  * a lock record naming a DEAD process is a leftover, not a start in progress;
  * a holder that reports ``starting`` without being launched by this call is reclaimed after the
    startup grace, verified from its lock record, and the replacement is started here;
  * a start THIS call launched is never reclaimed, and an unverifiable holder is refused with a
    repair command instead of being killed on a guess.
"""

from __future__ import annotations

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


def _past_deadline(deadline: float) -> None:
    """The real observer raises ``TimeoutError`` once its budget is spent; a fake must too, or the
    ensure loop spins on a deadline nothing is checking."""
    import time as _time
    if _time.monotonic() >= deadline:
        raise TimeoutError


def test_a_stalled_starting_holder_is_reclaimed_and_the_client_is_served(fast_runway, tmp_path, monkeypatch):
    from hermes_cli import gateway_runtime_replace as replace

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


def test_a_holder_that_cannot_be_verified_is_refused_with_a_repair_command(fast_runway, tmp_path, monkeypatch):
    from hermes_cli import gateway_runtime_replace as replace

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
