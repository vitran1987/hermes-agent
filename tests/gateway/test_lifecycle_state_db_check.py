"""An unclean gateway death must trigger a state.db integrity check.

Regression for the 2026-08-31 incident. ``state.db`` was corrupt from
2026-08-26 evening (a SIGKILL landed on a gateway mid-WAL-checkpoint during a
``--replace`` restart storm), but nothing checked the file. The damage sat in
old, rarely-read session rows for 3.5 days until a Desktop read tripped over
it on 2026-08-30 17:15 and surfaced as "Session not found".

``record_startup`` already detects the unclean exit and logs "SIGKILL / OOM /
VM death" — it just never looked at the database that death may have torn.
The check is gated on the unclean exit precisely because it costs ~2s on a
500MB store; a clean boot must not pay it.

The scan is now DEFERRED by default: the boot entry point writes
the exit-diag record and claims the sentinel immediately, and the forensic scan
runs post-connect (``run_deferred_integrity_check``) so a large store can never
gate platform startup (the ``:8644`` fleet peer-DM transport). The tests below
keep the historic INLINE behaviour covered via ``defer_integrity_check=False``.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from gateway.lifecycle_ledger import (
    check_state_db_integrity,
    get_lifecycle_sentinel_path,
    record_startup,
    run_deferred_integrity_check,
)

_DEAD_PID = 2 ** 22 + 12345  # beyond default pid_max; never alive


def _write_sentinel(home: Path, phase: str = "running") -> None:
    path = get_lifecycle_sentinel_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "phase": phase,
            "pid": _DEAD_PID,
            "start_time": 1000.0,
            "started_at": "2026-08-26T23:56:45+00:00",
        }),
        encoding="utf-8",
    )


def _make_state_db(home: Path, *, corrupt: bool, journal_mode: str = "delete") -> Path:
    """Build a real SQLite file, optionally with a genuinely torn b-tree page."""
    path = home / "state.db"
    conn = sqlite3.connect(path)
    if journal_mode != "delete":
        assert conn.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0] == journal_mode
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany(
        "INSERT INTO sessions (v) VALUES (?)", [(f"row-{i}" * 40,) for i in range(4000)]
    )
    conn.commit()
    conn.close()
    if corrupt:
        with open(path, "r+b") as handle:
            handle.seek(4096 * 6)
            handle.write(b"\xEF" * 4096)
    return path


def _make_multi_page_state_db(home: Path, rows: int = 300_000, journal_mode: str = "delete") -> Path:
    """A store large enough that ``quick_check`` takes measured tens of ms, so a
    wall-clock deadline can fire MID-scan. Cheap to build (~1s)."""
    path = home / "state.db"
    conn = sqlite3.connect(path)
    if journal_mode != "delete":
        assert conn.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0] == journal_mode
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany(
        "INSERT INTO sessions (v) VALUES (?)", [(f"row-{i}" * 40,) for i in range(rows)]
    )
    conn.commit()
    conn.close()
    return path


def _exit_diag_records(home: Path) -> list:
    log = home / "logs" / "gateway-exit-diag.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


# ── the checker itself ──────────────────────────────────────────────────────


def test_checker_passes_a_healthy_store(tmp_path: Path) -> None:
    _make_state_db(tmp_path, corrupt=False)
    assert check_state_db_integrity(home=tmp_path) == "ok"


def test_checker_reports_a_torn_btree_page(tmp_path: Path) -> None:
    _make_state_db(tmp_path, corrupt=True)
    verdict = check_state_db_integrity(home=tmp_path)
    assert verdict != "ok"
    assert "btreeInitPage" in verdict or "malformed" in verdict.lower()


def test_checker_tolerates_a_missing_store(tmp_path: Path) -> None:
    assert check_state_db_integrity(home=tmp_path) == "absent"


# ── wiring into the unclean-exit path ───────────────────────────────────────


def test_unclean_exit_records_the_corruption_verdict(tmp_path: Path) -> None:
    _make_state_db(tmp_path, corrupt=True)
    _write_sentinel(tmp_path)

    evidence = record_startup(home=tmp_path, defer_integrity_check=False)

    assert evidence is not None
    assert evidence["state_db_integrity"] != "ok"
    record = _exit_diag_records(tmp_path)[0]
    assert record["state_db_integrity"] != "ok"


def test_unclean_exit_on_a_healthy_store_records_ok(tmp_path: Path) -> None:
    _make_state_db(tmp_path, corrupt=False)
    _write_sentinel(tmp_path)

    evidence = record_startup(home=tmp_path, defer_integrity_check=False)

    assert evidence is not None
    assert evidence["state_db_integrity"] == "ok"


def test_clean_exit_does_not_pay_for_the_check(tmp_path: Path, monkeypatch) -> None:
    """A clean boot must not scan the store — that is the whole cost gate."""
    _make_state_db(tmp_path, corrupt=True)
    _write_sentinel(tmp_path, phase="exited")

    called = []
    import gateway.lifecycle_ledger as ledger

    monkeypatch.setattr(
        ledger, "check_state_db_integrity", lambda **kw: called.append(1) or "ok"
    )
    record_startup(home=tmp_path)

    assert not called, "integrity check ran on a clean boot"


# ── deferred startup: the scan must not gate platform startup ───


def test_deferred_startup_never_runs_the_check_inline(tmp_path: Path, monkeypatch) -> None:
    """DEFERRAL IS THE DEFAULT: the boot entry point must return WITHOUT the forensic
    scan completing, and the exit-diag record must record that the verdict is pending.
    The blocking stub below never returns unless the deferred path wrongly runs it."""
    import gateway.lifecycle_ledger as ledger

    started = threading.Event()
    released = threading.Event()

    def _blocking_check(**kwargs):
        started.set()
        released.wait(10)  # never set by the caller -> proves the boot path did not run it
        return "ok"

    monkeypatch.setattr(ledger, "check_state_db_integrity", _blocking_check)
    _write_sentinel(tmp_path)

    begin = time.monotonic()
    evidence = record_startup(home=tmp_path)  # default: defer_integrity_check=True
    elapsed = time.monotonic() - begin

    assert evidence is not None
    assert evidence["state_db_integrity"] == "deferred"
    assert not started.is_set(), "the inline integrity scan ran on the deferred boot path"
    assert elapsed < 5.0
    record = _exit_diag_records(tmp_path)[0]
    assert record["state_db_integrity"] == "deferred"
    assert record["tag"] == "gateway.previous_unclean_exit"


def test_deferred_scan_appends_a_second_record_and_returns_the_verdict(tmp_path: Path) -> None:
    _make_state_db(tmp_path, corrupt=True, journal_mode="wal")
    _write_sentinel(tmp_path)

    evidence = record_startup(home=tmp_path, defer_integrity_check=True)
    assert evidence is not None and evidence["state_db_integrity"] == "deferred"

    verdict = run_deferred_integrity_check(evidence, home=tmp_path, deadline_s=60.0)

    records = _exit_diag_records(tmp_path)
    assert [r["tag"] for r in records] == [
        "gateway.previous_unclean_exit", "gateway.state_db_integrity"]
    deferred = records[1]
    assert deferred["deferred"] is True
    assert deferred["state_db_integrity"] == verdict
    assert deferred["prior_pid"] == _DEAD_PID
    assert verdict != "ok"
    assert evidence["state_db_integrity"] == verdict


def test_deferred_scan_never_raises(tmp_path: Path, monkeypatch) -> None:
    import gateway.lifecycle_ledger as ledger

    _write_sentinel(tmp_path)
    evidence = record_startup(home=tmp_path, defer_integrity_check=True)

    def _boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(ledger, "check_state_db_integrity", _boom)

    assert run_deferred_integrity_check(evidence, home=tmp_path).startswith("check-failed")


# ── rollback-journal stores: a background scan would block live writers ──────
# In journal_mode=delete a quick_check holds a SHARED lock for its whole run, so every
# live gateway commit waits on it; WAL readers never block writers.


def _spy_on_scan(monkeypatch) -> list:
    """Record every scan while still running the real one."""
    import gateway.lifecycle_ledger as ledger

    calls: list = []
    real = ledger.check_state_db_integrity

    def _spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(ledger, "check_state_db_integrity", _spy)
    return calls


def test_deferred_scan_still_runs_on_a_wal_store(tmp_path: Path, monkeypatch) -> None:
    _make_state_db(tmp_path, corrupt=False, journal_mode="wal")
    calls = _spy_on_scan(monkeypatch)

    verdict = run_deferred_integrity_check({"prior_pid": _DEAD_PID}, home=tmp_path, deadline_s=60.0)

    assert verdict == "ok"
    assert len(calls) == 1
    assert _exit_diag_records(tmp_path)[-1]["state_db_integrity"] == "ok"


def test_deferred_scan_skips_a_rollback_journal_store(tmp_path: Path, monkeypatch, caplog) -> None:
    path = _make_state_db(tmp_path, corrupt=False)
    calls = _spy_on_scan(monkeypatch)
    evidence = {"prior_pid": _DEAD_PID}

    with caplog.at_level(logging.WARNING, logger="gateway.lifecycle_ledger"):
        verdict = run_deferred_integrity_check(evidence, home=tmp_path, deadline_s=60.0)

    assert not calls, "quick_check ran beside live traffic on a rollback-journal store"
    assert verdict == ("skipped: rollback-journal store (journal_mode=delete), "
                       "scan would block live writes")
    assert evidence["state_db_integrity"] == verdict
    record = _exit_diag_records(tmp_path)[-1]
    assert record["tag"] == "gateway.state_db_integrity"
    assert record["state_db_integrity"] == verdict
    assert record["prior_pid"] == _DEAD_PID
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "unclean" in warnings[0].lower() and "skipped" in warnings[0].lower()
    assert str(path) in warnings[0] and "PRAGMA quick_check" in warnings[0]
    # The journal-mode probe left no lock behind: a live writer commits without waiting.
    writer = sqlite3.connect(path, timeout=0)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        writer.execute("INSERT INTO sessions (v) VALUES ('live')")
        writer.commit()
    finally:
        writer.close()


def test_deferred_scan_skips_when_the_journal_mode_is_undeterminable(tmp_path: Path, monkeypatch) -> None:
    _make_state_db(tmp_path, corrupt=False, journal_mode="wal")
    calls = _spy_on_scan(monkeypatch)

    def _unreadable(conn):
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr("persistence.sqlite.admin._doctor._on_disk_journal_mode", _unreadable)

    verdict = run_deferred_integrity_check({"prior_pid": _DEAD_PID}, home=tmp_path, deadline_s=60.0)

    assert not calls
    assert verdict == ("skipped: rollback-journal store (journal_mode=unknown), "
                       "scan would block live writes")
    assert _exit_diag_records(tmp_path)[-1]["state_db_integrity"] == verdict


def test_deferred_scan_of_an_absent_store_is_unchanged(tmp_path: Path) -> None:
    verdict = run_deferred_integrity_check({"prior_pid": _DEAD_PID}, home=tmp_path, deadline_s=60.0)

    assert verdict == "absent"
    assert _exit_diag_records(tmp_path)[-1]["state_db_integrity"] == "absent"
    assert not (tmp_path / "state.db").exists()


# ── bounded scan: a deadline aborts through the progress handler ─────────────


def test_deadline_bounds_the_scan(tmp_path: Path, monkeypatch) -> None:
    _make_multi_page_state_db(tmp_path)
    monkeypatch.setattr("persistence.sqlite.admin._doctor._INTEGRITY_CHECK_PROGRESS_OPS", 1_000)

    verdict = check_state_db_integrity(home=tmp_path, deadline_s=0.01)

    assert verdict.startswith("check-incomplete: exceeded"), verdict


def test_no_deadline_keeps_the_historic_verdict_contract(tmp_path: Path, monkeypatch) -> None:
    """``deadline_s=None`` must be byte-for-byte today's behaviour: the handler never aborts."""
    _make_multi_page_state_db(tmp_path)
    monkeypatch.setattr("persistence.sqlite.admin._doctor._INTEGRITY_CHECK_PROGRESS_OPS", 1_000)

    assert check_state_db_integrity(home=tmp_path) == "ok"
    assert check_state_db_integrity(home=tmp_path, deadline_s=None) == "ok"


# ── startup-watchdog lease during the check (#115542) ───────────────────────


def _armed_handle(monkeypatch):
    """A real, thread-less StartupWatchdogHandle installed as the module singleton.

    ``report_startup_progress`` resolves the singleton, so the lease bookkeeping the
    check performs lands on this handle; no watchdog thread means nothing can exit pytest.
    """
    import hermes_startup_watchdog as sw

    handle = sw.StartupWatchdogHandle(timeout_s=300.0, exit_code=75)
    monkeypatch.setattr(sw, "_handle", handle)
    return handle


def test_unclean_exit_check_renews_the_startup_lease_while_sqlite_progresses(
    tmp_path: Path, monkeypatch
) -> None:
    """A 37 GB store needs ~4200 s of quick_check; one 900 s entry lease cannot cover it
    and the watchdog killed every boot with exit 75 (#115542). The check must keep renewing
    a phase-owned lease from SQLite's progress handler for as long as the PRAGMA advances,
    through the real unclean-exit entry point.
    """
    handle = _armed_handle(monkeypatch)
    # Renew on every handler tick so the renewal count is deterministic, not clock-bound.
    monkeypatch.setattr("persistence.sqlite.admin._doctor._INTEGRITY_CHECK_LEASE_RENEW_S", 0.0)
    monkeypatch.setattr("persistence.sqlite.admin._doctor._INTEGRITY_CHECK_PROGRESS_OPS", 1_000)
    _make_state_db(tmp_path, corrupt=False)
    _write_sentinel(tmp_path)

    evidence = record_startup(home=tmp_path, defer_integrity_check=False)

    assert evidence is not None and evidence["state_db_integrity"] == "ok"
    assert handle._lease_phase == "state_db_unclean_integrity_check"
    assert handle._lease_count > 1, "lease was taken once at entry and never renewed"


def test_unclean_exit_check_keeps_the_verdict_contract_under_the_lease(
    tmp_path: Path, monkeypatch
) -> None:
    """The progress handler must never convert corruption into success or abort the PRAGMA:
    a torn page still yields the first complaint, and the phase is still on record."""
    handle = _armed_handle(monkeypatch)
    _make_state_db(tmp_path, corrupt=True)

    verdict = check_state_db_integrity(home=tmp_path)

    assert verdict not in ("ok", "absent") and not verdict.startswith("check-failed")
    assert handle._lease_phase == "state_db_unclean_integrity_check"
    assert check_state_db_integrity(home=tmp_path / "nowhere") == "absent"


# ── the budget follows the STORE, and an abort is not a corruption finding ──
# Measured 2026-11-20 on `profiles/pm-bangioi/state.db` (page_count 892902 x page_size 4096 =
# 3.66 GB, journal_mode=wal): `PRAGMA quick_check(1)` had NOT finished after 749 s, so the flat
# 1800 s bound aborted a healthy store and the caller logged the abort as
# "FAILED integrity check ... Run `hermes doctor`" — 8 such ERRORs, 0 corruptions.


def _live_store_bytes() -> int:
    """The live store the defect was measured on: page_count x page_size."""
    return 892_902 * 4096


def test_deadline_scales_with_the_store_between_floor_and_ceiling() -> None:
    import gateway.lifecycle_ledger as ledger

    live_budget = ledger._integrity_deadline_for_bytes(_live_store_bytes())

    assert ledger.STATE_DB_INTEGRITY_DEADLINE_S < live_budget < ledger.STATE_DB_INTEGRITY_DEADLINE_CEILING_S
    assert live_budget > 749.0 * 2, "the budget still aborts the scan that the 749 s probe could not finish"


def test_deadline_keeps_the_flat_constant_for_a_small_or_unsized_store() -> None:
    """Below the floor nothing changes — the historic budget for every store we know is small."""
    import gateway.lifecycle_ledger as ledger

    assert ledger._integrity_deadline_for_bytes(None) == ledger.STATE_DB_INTEGRITY_DEADLINE_S
    assert ledger._integrity_deadline_for_bytes(0) == ledger.STATE_DB_INTEGRITY_DEADLINE_S
    assert ledger._integrity_deadline_for_bytes(512 * 1024**2) == ledger.STATE_DB_INTEGRITY_DEADLINE_S
    assert ledger._integrity_deadline_for_bytes(1024**3) == ledger.STATE_DB_INTEGRITY_DEADLINE_S


def test_deadline_ceiling_still_bounds_a_pathological_store() -> None:
    import gateway.lifecycle_ledger as ledger

    assert ledger._integrity_deadline_for_bytes(10**13) == ledger.STATE_DB_INTEGRITY_DEADLINE_CEILING_S


def test_store_size_comes_from_page_count_and_page_size(tmp_path: Path) -> None:
    """The budget's input is the header's logical size, not the file's apparent size."""
    import gateway.lifecycle_ledger as ledger
    from hermes_cli.sqlite_safe_read import connect_tracked, page_count_bytes

    path = _make_multi_page_state_db(tmp_path, rows=2_000)
    with connect_tracked(str(path), tracking_path=path) as conn:
        expected = page_count_bytes(conn)

    assert expected and expected > 0
    assert ledger._state_db_size_bytes(path) == expected
    assert ledger.state_db_integrity_deadline_s(path) == ledger.STATE_DB_INTEGRITY_DEADLINE_S


def test_deferred_scan_sizes_its_own_deadline_when_none_is_pinned(tmp_path: Path, monkeypatch) -> None:
    """The gateway must get the derived budget, not the flat constant: `run_bootstrap` passes none."""
    import gateway.lifecycle_ledger as ledger

    _make_multi_page_state_db(tmp_path, journal_mode="wal")
    monkeypatch.setattr(ledger, "_state_db_size_bytes", lambda path: _live_store_bytes())
    seen: dict = {}

    def _spy(**kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(ledger, "check_state_db_integrity", _spy)

    assert run_deferred_integrity_check({"prior_pid": _DEAD_PID}, home=tmp_path) == "ok"
    assert seen["deadline_s"] == pytest.approx(
        _live_store_bytes() / ledger.STATE_DB_INTEGRITY_SCAN_BYTES_PER_S)
    assert seen["deadline_s"] > ledger.STATE_DB_INTEGRITY_DEADLINE_S


def test_deferred_scan_start_does_not_pin_the_flat_deadline(monkeypatch) -> None:
    """A pinned constant here is what made the size-aware budget dead code."""
    import gateway.run_bootstrap as boot

    captured: dict = {}

    def _spawn(fn, args=(), kwargs=None, name=None):
        captured.update({"fn": fn, "args": args, "kwargs": kwargs, "name": name})

        class _Thread:
            def start(self) -> None:
                return None

        return _Thread()

    monkeypatch.setattr("agent.memory_provider.spawn_context_thread", _spawn)

    boot._start_deferred_state_db_integrity_check({"prior_pid": _DEAD_PID})

    assert captured["fn"] is run_deferred_integrity_check
    assert captured["kwargs"] in (None, {}), "the deferred start still pins a deadline"
    assert not captured["kwargs"] or "deadline_s" not in captured["kwargs"]


def test_deadline_abort_logs_a_warning_and_never_the_failure_line(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """A cut-off scan must say it was cut off — and must not name the repair remedy."""
    _make_multi_page_state_db(tmp_path, journal_mode="wal")
    monkeypatch.setattr("persistence.sqlite.admin._doctor._INTEGRITY_CHECK_PROGRESS_OPS", 1_000)

    with caplog.at_level(logging.DEBUG, logger="gateway.lifecycle_ledger"):
        verdict = run_deferred_integrity_check({"prior_pid": _DEAD_PID}, home=tmp_path, deadline_s=0.01)

    assert verdict.startswith("check-incomplete: exceeded"), verdict
    records = [(r.levelno, r.getMessage()) for r in caplog.records]
    failures = [m for level, m in records if level >= logging.ERROR and "FAILED integrity check" in m]
    assert not failures, f"a deadline abort was logged as a failed integrity check: {failures}"
    aborted = [m for level, m in records if level == logging.WARNING and "ABORTED at its deadline" in m]
    assert len(aborted) == 1, records
    assert "NOT evidence of corruption" in aborted[0]
    assert "hermes doctor" not in aborted[0], "a timeout must not point at the repair remedy"
    assert "check-incomplete" in aborted[0]
    assert _exit_diag_records(tmp_path)[-1]["state_db_integrity"] == verdict


def test_a_genuinely_damaged_store_still_fails_closed(tmp_path: Path, caplog) -> None:
    """The corruption path is untouched: a torn page keeps the ERROR and the remedy."""
    _make_state_db(tmp_path, corrupt=True, journal_mode="wal")

    with caplog.at_level(logging.DEBUG, logger="gateway.lifecycle_ledger"):
        verdict = run_deferred_integrity_check({"prior_pid": _DEAD_PID}, home=tmp_path, deadline_s=60.0)

    assert verdict not in ("ok", "absent")
    assert not verdict.startswith("check-"), verdict
    records = [(r.levelno, r.getMessage()) for r in caplog.records]
    failures = [m for level, m in records if level >= logging.ERROR and "FAILED integrity check" in m]
    assert len(failures) == 1, records
    assert "hermes doctor" in failures[0]
    assert not [m for m in failures if "NOT evidence of corruption" in m]
    assert _exit_diag_records(tmp_path)[-1]["state_db_integrity"] == verdict


def test_inline_deadline_abort_is_a_warning_too(tmp_path: Path, monkeypatch, caplog) -> None:
    """Both call sites share one severity decision (`_report_unclean_exit` is not special)."""
    import gateway.lifecycle_ledger as ledger

    _make_state_db(tmp_path, corrupt=False)
    _write_sentinel(tmp_path)
    monkeypatch.setattr(
        ledger, "check_state_db_integrity", lambda **kwargs: "check-incomplete: exceeded 1800s")

    with caplog.at_level(logging.DEBUG, logger="gateway.lifecycle_ledger"):
        evidence = record_startup(home=tmp_path, defer_integrity_check=False)

    assert evidence is not None
    assert evidence["state_db_integrity"] == "check-incomplete: exceeded 1800s"
    records = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert not [m for level, m in records if level >= logging.ERROR and "FAILED integrity check" in m]
    assert [m for level, m in records if level == logging.WARNING and "ABORTED at its deadline" in m]

