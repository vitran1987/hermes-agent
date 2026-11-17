"""A default ensure waits out a slow cold start whose owner keeps burning CPU, never a stalled one."""
import pytest

from hermes_cli import gateway_runtime_progress as progress


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(progress.time, "monotonic", clock)
    monkeypatch.setattr(progress, "_starting_owner_pid", lambda homes, child: 4242)
    return clock


def _run(clock, monkeypatch, runway, cpu, *, until=1000, step=1.0):
    """Drive ``extend`` once per ``step`` until the clock reaches ``until`` or the deadline passes.
    ``cpu(t)`` is the owner's CPU seconds at elapsed ``t``. Returns the elapsed time it gave up at."""
    start = clock.now
    monkeypatch.setattr(progress, "_cpu_seconds", lambda pid: cpu(clock.now - start))
    while clock.now - start < until:
        deadline = runway.extend(("home",), None)
        if clock.now >= deadline:
            return clock.now - start
        clock.now += step
    return None


def test_progressing_owner_outlives_the_base_deadline_until_the_cap(clock, monkeypatch):
    runway = progress.StartRunway(None, 60.0)
    # A boot at a 5% quota: 0.05 s of CPU per wall second, still not READY.
    gave_up = _run(clock, monkeypatch, runway, lambda t: 0.05 * t)
    assert gave_up == pytest.approx(progress.MAX_ADAPTIVE_TIMEOUT, abs=1.0)


def test_owner_with_no_cpu_progress_fails_at_the_base_deadline(clock, monkeypatch):
    runway = progress.StartRunway(None, 60.0)
    gave_up = _run(clock, monkeypatch, runway, lambda t: 2.0)  # stopped / blocked on a lock
    assert gave_up == pytest.approx(60.0, abs=1.0)


def test_owner_that_stalls_after_progressing_fails_within_two_windows(clock, monkeypatch):
    runway = progress.StartRunway(None, 60.0)
    gave_up = _run(clock, monkeypatch, runway, lambda t: 0.05 * min(t, 90.0))
    assert 90.0 < gave_up <= 90.0 + 2 * progress.PROGRESS_WINDOW_S + progress._LEAD_S + 1


def test_explicit_timeout_is_a_hard_deadline(clock, monkeypatch):
    runway = progress.StartRunway(10.0, 60.0)
    gave_up = _run(clock, monkeypatch, runway, lambda t: 0.05 * t)
    assert gave_up == pytest.approx(10.0, abs=1.0)


def test_unreadable_owner_cpu_never_extends(clock, monkeypatch):
    runway = progress.StartRunway(None, 60.0)
    gave_up = _run(clock, monkeypatch, runway, lambda t: None)  # AccessDenied / gone
    assert gave_up == pytest.approx(60.0, abs=1.0)


def test_owner_cpu_readable_only_after_the_first_sample_still_extends(clock, monkeypatch):
    runway = progress.StartRunway(None, 60.0)
    gave_up = _run(clock, monkeypatch, runway, lambda t: None if t < 3 else 0.05 * t)
    assert gave_up == pytest.approx(progress.MAX_ADAPTIVE_TIMEOUT, abs=1.0)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout_is_refused(timeout):
    with pytest.raises(ValueError):
        progress.StartRunway(timeout, 60.0)


def test_cpu_seconds_reads_this_process():
    import os
    cpu = progress._cpu_seconds(os.getpid())
    assert cpu is not None and cpu > 0


def test_a_lock_record_without_a_running_gateway_is_never_the_owner(tmp_path, monkeypatch):
    """A backup/restore holding the home writes a lock record too; its CPU is not the start's."""
    from types import SimpleNamespace
    from gateway import status
    monkeypatch.setattr(status, "get_running_pid", lambda pid_path, cleanup_stale: None)
    assert progress._starting_owner_pid((tmp_path,), SimpleNamespace(pid=77)) == 77


# --- the other half: a ``starting`` holder may be BOOTING, so a reclaim needs evidence -------------

def test_a_holder_that_keeps_accruing_cpu_is_progressing(clock, monkeypatch):
    """A slow boot spends CPU: it is slow, not hung, and must never be reclaimed."""
    start = clock.now
    monkeypatch.setattr(progress, "_cpu_seconds", lambda pid: 0.05 * (clock.now - start))
    monkeypatch.setattr(progress, "_record_age_s", lambda home: 5.0)  # published seconds ago
    holder = progress.HolderProgress()
    assert holder.sample("home") == progress.UNPROVEN  # one window of evidence, not a single read
    clock.now += progress.PROGRESS_WINDOW_S
    assert holder.sample("home") == progress.PROGRESSING


def test_a_holder_that_accrues_no_cpu_is_stalled(clock, monkeypatch):
    """The 2026-11-17 ABI-500 daemon: alive, lock held, control plane dead, ~0 CPU."""
    monkeypatch.setattr(progress, "_cpu_seconds", lambda pid: 2.0)  # stopped, blocked, or wedged
    monkeypatch.setattr(progress, "_record_age_s", lambda home: 5.0)
    holder = progress.HolderProgress()
    assert holder.sample("home") == progress.UNPROVEN
    clock.now += progress.PROGRESS_WINDOW_S
    assert holder.sample("home") == progress.STALLED


def test_a_young_unreadable_holder_needs_a_whole_boot_budget_before_it_is_stalled(tmp_path, clock, monkeypatch):
    """No pid to sample: only a record older than a whole cold boot is evidence of a wedge."""
    import os
    import time
    monkeypatch.setattr(progress, "_starting_owner_pid", lambda homes, child: None)
    holder = progress.HolderProgress()
    assert holder.sample(tmp_path) == progress.UNPROVEN  # no record: a bare reservation
    lock = tmp_path / "gateway.lock"
    lock.write_text("{}", encoding="utf-8")
    clock.now += progress.PROGRESS_WINDOW_S
    assert holder.sample(tmp_path) == progress.UNPROVEN  # a young record may still be booting
    old = time.time() - progress.MAX_ADAPTIVE_TIMEOUT - 60.0
    os.utime(lock, (old, old))
    clock.now += progress.PROGRESS_WINDOW_S
    assert holder.sample(tmp_path) == progress.STALLED


def test_a_replacement_taking_the_lock_opens_a_fresh_window(clock, monkeypatch):
    start = clock.now
    current = {"pid": 4242}
    monkeypatch.setattr(progress, "_starting_owner_pid", lambda homes, child: current["pid"])
    monkeypatch.setattr(progress, "_record_age_s", lambda home: 5.0)
    monkeypatch.setattr(progress, "_cpu_seconds",
                        lambda pid: (12.0 if pid == 4242 else 0.05 * (clock.now - start)))
    holder = progress.HolderProgress()
    assert holder.sample("home") == progress.UNPROVEN
    clock.now += progress.PROGRESS_WINDOW_S
    assert holder.sample("home") == progress.STALLED
    current["pid"] = 9999  # a different process holds it now: nothing to compare against yet
    clock.now += progress.PROGRESS_WINDOW_S
    assert holder.sample("home") == progress.UNPROVEN
    clock.now += progress.PROGRESS_WINDOW_S
    assert holder.sample("home") == progress.PROGRESSING  # and it is booting
