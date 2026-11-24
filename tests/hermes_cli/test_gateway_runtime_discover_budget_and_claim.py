"""A cold discovery must not outrun its budget, and a start must not race a live claim.

Two defects from the kanban crash review (card t_bc80590e, verdict t_d35486e5):

1. ``discover_gateway_endpoint``'s *default* budget was 2.0 s. One cold identify on the host
   measured 1246 ms (62% of that budget) — no headroom, and the same failure shape S5 already
   fixed in ``gateway/control_socket.py`` (2.0 -> 8.0). The default is now
   ``DEFAULT_DISCOVER_TIMEOUT``; an over-budget peer must still fail CLOSED with a bounded
   ``control_timeout`` and never the admission-level ``unknown`` a worker reports as
   "execution unknown".
2. ``spawn_unmanaged_gateway`` launched a child for a home whose profile claim a LIVE runtime
   already held. The child then failed its own reserve inside a detached process
   ("Cannot reserve gateway profiles: Gateway runtime already owns profile …"), the caller waited
   out its whole ensure deadline, and the card was booked ``execution unknown``. The claim is now
   read before the spawn and refused as ``profile_claimed``.
"""
import asyncio
import time

import pytest

import hermes_dir_acl

# A deadline has to outlast the *fixture and import* cost of the real spawn path, not the work under
# test: ``spawn_unmanaged_gateway`` resolves the environment's interpreter and imports its child-env
# helpers before the first ``remaining()``. 5 s was measurably too tight on a cold pycache (a full-file
# run timed out at the second call before its assertion ran), and a proof that fails for an unrelated
# timeout proves nothing. 60 s equals ``DEFAULT_ENSURE_TIMEOUT``; neither call here actually waits.
_SPAWN_DEADLINE_SECONDS = 60.0


@pytest.fixture(autouse=True)
def _non_elevated_client(monkeypatch):
    """Every start here is a normal client's: an elevated one is refused before any spawn."""
    monkeypatch.setattr(hermes_dir_acl, "process_is_elevated", lambda: False)


def _identity(home):
    resolved = str(home.resolve())
    return {
        "protocol": 1,
        "runtime_protocol": 1,
        "instance_id": "fixture-instance",
        "authority_epoch": 1,
        "hermes_home": resolved,
        "state": "ready",
        "api_origin": "http://127.0.0.1:41817",
        "supervisor": "none",
        "served_profiles": [{"profile_id": resolved, "home": resolved}],
        "capabilities": ["session-authority-v1"],
    }


class _SlowPeer:
    """The REAL ``GatewayControlServer``, answering ``identify`` only after ``delay`` seconds.

    An async context manager: ``start``/``stop`` must run in the SAME loop as the probes served
    against it, as ``tests/hermes_cli/test_gateway_runtime.py``'s ``control_peer`` does.
    """

    def __init__(self, home, delay):
        self.home, self.delay, self._server = home, delay, None

    async def __aenter__(self):
        from gateway.control_socket import GatewayControlServer

        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = _identity(self.home)

        def slow_identify():
            time.sleep(self.delay)  # blocks the server's loop, so the answer really is late
            return payload

        self._server = GatewayControlServer(self.home, verb_handlers={"identify": slow_identify})
        assert await self._server.start(), "the fixture control peer did not start"
        pointer = self.home / "gateway.sock.path"
        if pointer.exists():
            pointer.chmod(0o600)
        return self

    async def __aexit__(self, *exc):
        await self._server.stop()
        return False


async def _discover(home, **kwargs):
    from hermes_cli import gateway_runtime
    return await asyncio.to_thread(gateway_runtime.discover_gateway_endpoint, home, **kwargs)


def test_slow_endpoint_inside_the_default_budget_succeeds(tmp_path):
    """Acceptance 2: a peer slower than the OLD 2.0 s default but inside the new one must resolve.

    This is the regression the card asks for: at 2.0 s this peer returned ``control_timeout`` and
    the caller (a worker's boot) had no endpoint at all.
    """
    from hermes_cli import gateway_runtime

    delay = 3.0
    assert gateway_runtime.DEFAULT_DISCOVER_TIMEOUT > delay, "the default must absorb this delay"
    home = tmp_path / "profile"

    async def probe():
        async with _SlowPeer(home, delay):
            observed = await _discover(home)
            assert observed.state == "ready", (observed.state, observed.reason_code)
            assert observed.endpoint is not None
            # The old budget provably did not cover the same peer, so this is a real behaviour
            # change and not a peer that happened to answer quickly.
            too_tight = await _discover(home, timeout=2.0)
            assert (too_tight.state, too_tight.reason_code) == ("inaccessible", "control_timeout")

    asyncio.run(probe())


def test_over_budget_endpoint_fails_closed_with_a_clear_class(tmp_path):
    """Acceptance 3: over budget => a *named* refusal, never the ``unknown`` a worker reports.

    ``unknown`` is an admission status (``AdmissionStatus.UNKNOWN``), written when an owner loses
    its settlement — it is not a ``RuntimeState`` and discovery can never produce it. A discovery
    that spends its budget must answer ``inaccessible``/``control_timeout`` so the caller can
    distinguish "no owner answered in time" from "we lost track of the work".
    """
    from gateway.runtime_contract import RuntimeState
    from hermes_cli import gateway_runtime

    assert "unknown" not in RuntimeState.__args__

    budget = 1.0
    delay = gateway_runtime.DEFAULT_DISCOVER_TIMEOUT + 1.0
    home = tmp_path / "profile"

    async def probe():
        async with _SlowPeer(home, delay):
            t0 = time.monotonic()
            observed = await _discover(home, timeout=budget)
            return observed, time.monotonic() - t0

    observed, elapsed = asyncio.run(probe())
    assert (observed.state, observed.reason_code) == ("inaccessible", "control_timeout")
    assert observed.endpoint is None
    # Fail-closed stayed fail-closed: the call gave up on its own budget instead of hanging.
    assert elapsed < delay, f"returned after {elapsed:.1f}s, i.e. it waited out the peer"


def test_spawn_refuses_a_profile_claim_a_live_runtime_holds(tmp_path, monkeypatch):
    """Acceptance: no orphan. A held claim refuses the spawn instead of launching a doomed child."""
    from gateway.runtime_ownership import ProfileOwnership
    from hermes_cli import gateway_runtime_start as start
    from hermes_cli.gateway_runtime_service import RuntimeStartError

    home = tmp_path / "profile"
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    spawned = []
    monkeypatch.setattr(start.subprocess, "Popen",
                        lambda command, **kwargs: spawned.append(command) or object())

    ownership = ProfileOwnership()
    ownership.reserve([home])  # a LIVE claim: a record this process wrote, its flock held
    try:
        with pytest.raises(RuntimeStartError) as caught:
            start.spawn_unmanaged_gateway(home, deadline=time.monotonic() + _SPAWN_DEADLINE_SECONDS)
        assert caught.value.reason == "profile_claimed"
        # The caller's verdict must read "another owner exists, wait for it", not a hard failure.
        assert caught.value.state == "starting"
        detail = caught.value.detail or ""
        assert "already holds this profile's claim" in detail, detail
        assert str(home) not in detail, f"the refusal leaked the profile home path: {detail}"
        assert spawned == [], "a child was launched for a claim another live runtime holds"
    finally:
        ownership.release(home)

    # The claim gone, the same call must be able to start again — a released record never wedges it.
    sentinel = object()
    monkeypatch.setattr(start.subprocess, "Popen", lambda command, **kwargs: sentinel)
    assert start.spawn_unmanaged_gateway(home, deadline=time.monotonic() + _SPAWN_DEADLINE_SECONDS) is sentinel

