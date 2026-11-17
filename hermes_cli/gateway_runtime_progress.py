"""Adaptive startup deadline for ``ensure_gateway_runtime``: a cold start that keeps burning CPU is
slow, not hung.

On a CPU-starved host (a cgroup quota, a loaded CI runner) a cold boot's few seconds of CPU can take
minutes of wall time, and a fixed deadline turns every such start into ``starting: deadline``. A
default ensure therefore waits past its base deadline while the start's owner process measurably
progresses, up to :data:`MAX_ADAPTIVE_TIMEOUT`. An owner that makes no CPU progress (blocked on a
lock, deadlocked, stopped) still fails at the base deadline, so a hung start is reported as promptly
as before. Progress is read on the home's verified running gateway, else the process this call
spawned (not its descendants: on Windows a uv venv launcher waits on its child, so a start there
keeps the base deadline until the gateway publishes its lock).

An explicit timeout is hard. That covers ``hermes gateway ensure/attach --timeout`` (Desktop runs it
under its own outer timer) and the Ink TUI bootstrap; the adaptive wait serves the in-process clients
(``hermes -z``, ``chat -q``, cron, kanban workers, ACP).

The same evidence decides the other direction: :class:`HolderProgress` gates the stalled-holder
reclaim in ``ensure_gateway_runtime``, so a lock holder that is still booting is not stopped by a
sibling client's wall-clock grace either.
"""
from __future__ import annotations

import math
import time
from pathlib import Path

# Hard ceiling on a default ensure's wait while its owner keeps progressing (a cold boot measured
# ~160 s of wall time at a 5% CPU quota).
MAX_ADAPTIVE_TIMEOUT = 300.0
# CPU the owner must accrue per window to count as progressing. A parked or stopped process accrues
# ~0; a boot even at a 5% quota accrues several times this.
PROGRESS_WINDOW_S = 15.0
_MIN_CPU_PER_WINDOW_S = 0.1
# A progressing start keeps the deadline one window (plus slack) ahead, so it survives until the next
# verdict; an owner that stalls after progressing fails within about one window. Sampled at most this
# often (a few lock-record reads and one psutil call).
_LEAD_S = PROGRESS_WINDOW_S + 5.0
_SAMPLE_EVERY_S = 1.0


def _starting_owner_pid(homes, child) -> int | None:
    """The pending start's process: a verified running gateway of one of ``homes`` (the profile's
    own, or the multiplexer root this call started: runtime lock held, PID record matching a live
    gateway of that home), else the child this call spawned. Never a bare lock record, which a
    backup or restore holding the home also writes."""
    from gateway.status import get_running_pid
    for home in dict.fromkeys(homes):
        if (pid := get_running_pid(Path(home) / "gateway.pid", cleanup_stale=False)) is not None:
            return pid
    return getattr(child, "pid", None)


def _cpu_seconds(pid: int) -> float | None:
    import psutil

    try:
        times = psutil.Process(pid).cpu_times()
    except (psutil.Error, OSError):  # gone, or another user's (AccessDenied): no progress evidence
        return None
    return times.user + times.system


# --- the other half: a ``starting`` holder may be BOOTING, so a reclaim needs evidence ------------

def _record_age_s(home) -> float | None:
    """Seconds since the holder published *home*'s runtime lock record, or ``None`` when it has none.

    The record — and the file's mtime — is written when a boot RESERVES the home
    (``gateway.runtime_ownership.ProfileOwnership.reserve``), before it claims a PID file or binds
    its control socket, so its age is the boot's age.
    """
    try:
        return max(0.0, time.time() - (Path(home) / "gateway.lock").stat().st_mtime)
    except OSError:
        return None


PROGRESSING, STALLED, UNPROVEN = "progressing", "stalled", "unproven"


class HolderProgress:
    """Is the owner holding a home's runtime lock still booting?

    The stalled-holder reclaim in ``ensure_gateway_runtime`` stops a lock holder that reports
    ``starting`` without being a start of its own making. ``starting`` is derived from the lock
    alone, so it covers a boot IN PROGRESS as well as a wedged daemon, and wall-clock grace cannot
    tell them apart: on a CPU-starved host a cold boot measures ~160 s of wall time. Every verdict
    here is therefore EVIDENCE, and a holder that cannot be read is never stopped:

    * :data:`PROGRESSING` — the holder's CPU grew across a whole :data:`PROGRESS_WINDOW_S` window.
      It is slow, not hung; the caller restarts its startup grace.
    * :data:`STALLED` — a whole window passed with no CPU progress (a wedged daemon accrues ~0: the
      2026-11-17 ABI-500 gateway), or the holder's CPU cannot be read at all and its record is older
      than a whole boot budget (:data:`MAX_ADAPTIVE_TIMEOUT`).
    * :data:`UNPROVEN` — no verdict yet: a window still open, a record younger than a boot budget
      (it has not had the chance to finish booting), or a reservation that has published neither a
      PID record nor a CPU this process may read. Never a reason to stop anything.

    Sampling is throttled to :data:`_SAMPLE_EVERY_S` per home (a throttled call repeats the last
    verdict). One holder is judged at a time, by pid: a new pid opens a fresh window.
    """

    def __init__(self) -> None:
        self._windows: dict[Path, tuple[int, float, float]] = {}  # home -> (pid, when, cpu)
        self._last: dict[Path, tuple[float, str]] = {}  # home -> (sampled_at, verdict)

    def sample(self, home) -> str:
        home = Path(home)
        now = time.monotonic()
        previous = self._last.get(home)
        if previous is not None and now - previous[0] < _SAMPLE_EVERY_S:
            return previous[1]
        verdict = self._read(home, now)
        self._last[home] = (now, verdict)
        return verdict

    def _read(self, home: Path, now: float) -> str:
        age = _record_age_s(home)
        young = age is None or age < MAX_ADAPTIVE_TIMEOUT
        pid = _starting_owner_pid((home,), None)
        cpu = _cpu_seconds(pid) if pid is not None else None
        if pid is None or cpu is None:
            self._windows.pop(home, None)
            # Nothing to sample. A whole boot budget of record age is the only evidence left, and a
            # record that young — or absent: a reservation that has not published its PID — has not
            # had its chance to finish booting.
            return UNPROVEN if young else STALLED
        window = self._windows.get(home)
        if window is None or window[0] != pid:
            self._windows[home] = (pid, now, cpu)
            return UNPROVEN  # a new holder: nothing to compare against yet
        if now - window[1] < PROGRESS_WINDOW_S:
            return UNPROVEN  # the window is still open
        progressed = cpu - window[2] >= _MIN_CPU_PER_WINDOW_S
        self._windows[home] = (pid, now, cpu)
        return PROGRESSING if progressed else STALLED


class StartRunway:
    """The ensure loop's deadline: ``timeout`` (hard), or the default base deadline that
    :meth:`extend` pushes forward while the start's owner keeps progressing."""

    def __init__(self, timeout: float | None, default: float):
        self._adaptive = timeout is None
        timeout = default if timeout is None else timeout
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        now = time.monotonic()
        self.deadline = now + timeout
        self._cap = now + max(timeout, MAX_ADAPTIVE_TIMEOUT)
        self._window: tuple[int, float, float] | None = None  # (owner pid, when, cpu) at window start
        self._sampled = 0.0

    def extend(self, homes, child) -> float:
        """The deadline after one pending iteration; never earlier than before, never past the cap.
        Nothing to wait on yet (no ``homes``, no ``child``) is not a sample: it must not spend the
        throttle that the start's first window needs. A due deadline is never throttled: it gets the
        verdict sample whatever the last one's age."""
        now = time.monotonic()
        throttled = now - self._sampled < _SAMPLE_EVERY_S and now < self.deadline
        if not self._adaptive or (not homes and child is None) or throttled:
            return self.deadline
        self._sampled = now
        pid = _starting_owner_pid(homes, child)
        cpu = _cpu_seconds(pid) if pid is not None else None
        if pid is None or cpu is None or self._window is None or self._window[0] != pid:
            # No reading, or a new owner: open a fresh window and claim nothing yet.
            self._window = (pid, now, cpu) if pid is not None and cpu is not None else None
        elif now - self._window[1] >= PROGRESS_WINDOW_S:
            if cpu - self._window[2] >= _MIN_CPU_PER_WINDOW_S:
                self.deadline = min(self._cap, max(self.deadline, now + _LEAD_S))
            self._window = (pid, now, cpu)
        return self.deadline
