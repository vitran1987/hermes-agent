"""The restart lease: a lane mid-restart is not a lane a client may resurrect.

Measured 2026-11-26 06:16-06:19 ICT (kanban t_784ed637, Defect 2 P1). The sanctioned restart had
already stopped the ROOT lane and was still working; nine surviving ``kanban_worker_client``
processes replayed the lane argv recorded in ``gateway_state.json`` within 40 s. One child took
``gateway.lock`` and never published a host record, the other eight exited with "Another gateway
instance is already running (PID 24004 ...) and did not publish a host record this process could
attach to", the respawn-storm backoff fired at 11 restarts in 120 s, and :9920 stayed unbound for
another 11 minutes.

``hermes_cli/gateway_runtime_start.py::_restart_lease`` makes the spawn path read the restart
tooling's claim first. These cases pin the reader's contract, including the property that matters
most: it must never be able to hold a lane down.
"""
import json
import time

import pytest

from hermes_cli.gateway_runtime_start import _restart_lease, spawn_unmanaged_gateway
from hermes_cli.gateway_runtime_service import RuntimeStartError

LEASE = ".gateway-restart-in-progress.json"


def write_lease(home, *, pid=None, started_at=None, expires_at=None, **extra):
    now = time.time()
    record = {
        "pid": int(pid if pid is not None else 4242),
        "started_at": float(started_at if started_at is not None else now - 5),
        "expires_at": float(expires_at if expires_at is not None else now + 60),
        "reason": "restart-remote-inline",
        "lane": home.name,
    }
    record.update(extra)
    (home / LEASE).write_text(json.dumps(record), encoding="utf-8")
    return record


def test_a_live_lease_names_the_restarter(tmp_path):
    record = write_lease(tmp_path)
    assert _restart_lease(tmp_path) == record["pid"]


def test_no_lease_is_not_a_claim(tmp_path):
    """The overwhelmingly common case: nothing is restarting, a client must be able to start."""
    assert _restart_lease(tmp_path) is None


def test_an_expired_lease_never_holds_a_lane_down(tmp_path):
    """The 2026-11-26 restarter HUNG. Its own declared window is the only backstop, so a lease
    past ``expires_at`` must stop blocking even though the restarter's pid is still alive."""
    now = time.time()
    write_lease(tmp_path, pid=1, started_at=now - 600, expires_at=now - 1)
    assert _restart_lease(tmp_path) is None


def test_a_lease_window_beyond_the_cap_is_not_evidence(tmp_path):
    """The format is ours; an out-of-range window (a millisecond stamp, a corrupted file) is not
    a restart this code can recognise, so it is ignored rather than honoured for hours."""
    now = time.time()
    write_lease(tmp_path, started_at=now, expires_at=now + 10 * 24 * 3600)
    assert _restart_lease(tmp_path) is None


@pytest.mark.parametrize("body", [
    "",                                  # empty file
    "not json at all",                   # unreadable
    json.dumps([1, 2, 3]),               # not an object
    json.dumps({"pid": 1}),              # missing fields
    json.dumps({"pid": None, "started_at": 1.0, "expires_at": 2.0}),
    json.dumps({"pid": "x", "started_at": 1.0, "expires_at": 2.0}),
    json.dumps({"pid": 1, "started_at": 5.0, "expires_at": 4.0}),  # inverted window
])
def test_a_malformed_lease_is_ignored(tmp_path, body):
    (tmp_path / LEASE).write_text(body, encoding="utf-8")
    assert _restart_lease(tmp_path) is None


def test_a_lease_blocks_the_spawn(tmp_path):
    """The whole point: a client that finds the lane absent mid-restart does not spawn a
    successor. It reports a bounded, actionable reason instead of racing the restart."""
    record = write_lease(tmp_path)
    with pytest.raises(RuntimeStartError) as raised:
        spawn_unmanaged_gateway(tmp_path, deadline=time.monotonic() + 5)
    assert raised.value.reason == "profile_restarting"
    assert str(record["pid"]) in (raised.value.detail or "")


def test_force_still_spawns_through_a_lease(tmp_path, monkeypatch):
    """``force`` is the operator's own "start it anyway" and keeps its arbitration: a lease must
    never be able to make a lane unstartable by hand."""
    from hermes_cli import gateway_runtime_start as start

    write_lease(tmp_path)
    monkeypatch.setattr(start, "_claimed_owner", lambda home: None)
    monkeypatch.setattr(start, "resolve_gateway_interpreter", lambda root: __import__("sys").executable)
    spawned = {}

    def fake_popen(command, **kwargs):
        spawned["argv"] = command
        return object()

    monkeypatch.setattr(start.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(start, "windows_detach_popen_kwargs", lambda: {})
    # A generous deadline: this case must not be flaky on a loaded host, and the assertion is
    # "force spawns THROUGH a lease", not "spawn completes in 5 s".
    assert start.spawn_unmanaged_gateway(tmp_path, deadline=time.monotonic() + 120, force=True) is not None
    assert "--force" in spawned["argv"]
