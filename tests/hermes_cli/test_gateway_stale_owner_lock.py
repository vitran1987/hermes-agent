"""A runtime lock whose record names a DEAD owner is a leftover, not a start in progress.

2026-11-17: the ``code-reviewer-bangioi`` gateway was killed by the loop watchdog at 15:20:26 and
the supervisor kept ``gateway.lock`` — the record still named the dead PID. ``missing_owner_state``
read ``starting`` for as long as the file survived (453 s measured at 12:00), so
``ensure_gateway_runtime`` waited out its startup deadline instead of starting a replacement, and
every Kanban worker of that profile exited ``Kanban owner silent for 191s: Gateway starting:
deadline`` — a host-side outage spent as the card's failure budget (37 cards/24 h).

The distinction that matters: an UNPUBLISHED reservation (lock held, no record yet — a boot that
has not written its PID) is a genuine start and must keep reading ``starting`` so no owner is raced.
"""

from __future__ import annotations

import json
import os

import pytest


@pytest.fixture
def held_lock(monkeypatch):
    """The home's runtime lock is held (by a supervisor or a booting owner), cross-platform."""
    import gateway.status as status
    monkeypatch.setattr(status, "_is_gateway_runtime_lock_active_strict", lambda lock: True)


def _home_with_lock(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    lock = home / "gateway.lock"
    return home, lock


def test_a_held_lock_naming_a_dead_pid_reads_absent(tmp_path, held_lock):
    from hermes_cli.gateway_runtime_discovery import missing_owner_state

    home, lock = _home_with_lock(tmp_path)
    lock.write_text(json.dumps({"pid": 999_999_999, "home": str(home)}), encoding="utf-8")

    assert missing_owner_state(home) == "absent"


def test_a_held_lock_naming_a_live_pid_is_still_starting(tmp_path, held_lock):
    from hermes_cli.gateway_runtime_discovery import missing_owner_state

    home, lock = _home_with_lock(tmp_path)
    lock.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")

    assert missing_owner_state(home) == "starting"


def test_an_unpublished_reservation_is_still_starting(tmp_path, held_lock):
    """Lock held with no PID record: a boot mid-reservation. It must not be raced."""
    from hermes_cli.gateway_runtime_discovery import missing_owner_state

    home, lock = _home_with_lock(tmp_path)
    lock.write_text("", encoding="utf-8")

    assert missing_owner_state(home) == "starting"


def test_an_unheld_lock_reads_absent(tmp_path):
    from hermes_cli.gateway_runtime_discovery import missing_owner_state

    home, lock = _home_with_lock(tmp_path)
    lock.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")

    assert missing_owner_state(home) == "absent"
