"""``describe_suppression`` must name the caps that defer a tick.

A tick can spawn nothing for a perfectly healthy reason: every ready card's
assignee sits at ``kanban.max_in_progress_per_profile``, or the host is already
at ``kanban.max_spawn`` / ``kanban.max_in_progress``. The embedded gateway
dispatcher (``gateway/kanban_watchers.py``) and the CLI daemon
(``hermes_cli/kanban_ops.py::_on_tick``) both append
``describe_suppression(...)`` to the "kanban dispatcher stuck" warning as
``Last tick held back: ...``, and only when that string is non-empty.

The lane loop collected ``skipped_per_profile_capped`` and the budget check
stopped before the lanes ran with no bucket filled — neither reached the line,
so the warning fired bare and a normal cap deferral was indistinguishable from
a broken profile (venv / PATH / credentials).
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

from hermes_cli.kanban_db_dispatch import DispatchResult, describe_suppression


def test_per_profile_cap_is_named() -> None:
    """The exact shape from the incident: one card deferred at cap."""
    res = DispatchResult(skipped_per_profile_capped=[("t_x", "developer-bangioi", 3)])
    line = describe_suppression([res])
    assert "per_profile_capped=1" in line
    # The third tuple member is the running count that deferred the card.
    assert "per_profile_capped_max=3" in line


def test_per_profile_cap_counts_every_deferred_card() -> None:
    first = DispatchResult(
        skipped_per_profile_capped=[("t_a", "pm-bangioi", 2), ("t_b", "pm-bangioi", 2)]
    )
    second = DispatchResult(skipped_per_profile_capped=[("t_c", "developer-bangioi", 4)])
    line = describe_suppression([first, None, second])
    assert "per_profile_capped=3" in line
    assert "per_profile_capped_max=4" in line


def test_a_tick_that_holds_nothing_renders_nothing() -> None:
    """Unchanged contract: no hold, no payload, the caller prints no suffix."""
    assert describe_suppression([DispatchResult()]) == ""
    assert describe_suppression([]) == ""


def test_existing_holds_still_render_alongside_the_cap() -> None:
    res = DispatchResult(
        skipped_per_profile_capped=[("t_x", "developer-bangioi", 1)],
        respawn_guarded=[("t_y", "active_pr")],
        rate_limited=["t_z"],
        skipped_locked=True,
    )
    line = describe_suppression([res])
    for part in ("per_profile_capped=1", "active_pr=1", "rate_limited=1", "skipped_locked=1"):
        assert part in line


def test_host_cap_tick_is_named() -> None:
    """``max_in_progress`` returns before the lanes run; that cap is a hold too."""
    line = describe_suppression([DispatchResult(tick_suppressed="max_in_progress")])
    assert "tick_capped=max_in_progress" in line


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    """Fresh HERMES_HOME with a kanban DB and one profile to assign work to."""
    test_home = tempfile.mkdtemp(prefix="kanban_suppression_test_")
    for prof in ("alpha", "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
        with open(os.path.join(test_home, "profiles", prof, "config.yaml"), "w") as fh:
            fh.write("{}\n")  # identity marker: a bare dir is not a profile
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    # The module purge above discards the conftest's memory-sample neutralizer;
    # re-apply it so the host's live memory pressure cannot cap this tick.
    monkeypatch.setattr(kbd, "_system_memory_sample", dict)
    yield kb


def _fake_spawn(*args, **kwargs):
    return 12345


def test_a_real_capped_tick_reaches_the_warning_line(isolated_kanban_home) -> None:
    """End to end: dispatch with a cap, then the payload the warning prints."""
    kb = isolated_kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        for i in range(3):
            kb.create_task(conn, title=f"a{i}", assignee="alpha")

    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=True, max_in_progress_per_profile=1,
        )
    assert len(res.spawned) == 1
    assert len(res.skipped_per_profile_capped) == 2

    line = kbd.describe_suppression([res])
    assert "per_profile_capped=2" in line
    assert "per_profile_capped_max=1" in line
    # What both call sites print: the suffix is non-empty, so the warning names
    # the cap instead of reading as an unexplained zero-spawn tick.
    assert f" Last tick held back: {line}." != ""
