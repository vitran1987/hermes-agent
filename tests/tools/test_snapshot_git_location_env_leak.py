"""Git location env (GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE/GIT_COMMON_DIR) leaking
through the shared bash session snapshot.

Regression coverage for the measured cross-task leak (t_8ce7a1b8): one long-lived
backend serves many sessions through ONE snapshot file, and ``export -p`` dumped
the parent session's *worktree* GIT location pointers into it. Every later
worker session ``source``d the snapshot and saw a FOREIGN checkout — ``git
rev-parse`` resolved the wrong repo, pre-push guards false-BLOCKed, and workers
looped on git commands that could never succeed.

The fix reuses the core's authoritative ``_GIT_LOCATION_ENV`` tuple
(hermes_cli/_early_recovery.py) and unsets those names inside the dump subshell
BEFORE ``export -p`` (tools/environments/base_session_env.py), exactly like the
per-session bridged vars — except these are the user's own exports, so the unset
is import-free at shell level and safe on backends that never set them.
"""

import os
import subprocess

import pytest

from tools.environments.base_session_env import _export_dump_excluding_session_vars

GIT_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")


def test_unit_dump_unset_list_carries_the_core_git_location_tuple():
    """The snapshot dump must unset every name in the core's _GIT_LOCATION_ENV
    tuple, and derive from it (single definition site — a future fifth name in
    the tuple lands in the unset list without a second edit here)."""
    from hermes_cli._early_recovery import _GIT_LOCATION_ENV

    snippet = _export_dump_excluding_session_vars("/tmp/snap", ["FOO_BAR"])
    for name in _GIT_LOCATION_ENV:
        assert name in snippet, f"{name} missing from the pre-export unset list"
    # The unset happens in the subshell BEFORE export -p (never a grep -vE —
    # multiline values survive line-based filtering, #71296).
    assert snippet.index(_GIT_LOCATION_ENV[0]) < snippet.index("export -p")
    # Python-side contract for tests that grep the regex/dump shape: the tuple
    # is exactly the four git location pointers the leak measurement saw.
    assert set(_GIT_LOCATION_ENV) == set(GIT_LOCATION_VARS)


@pytest.mark.platforms("posix")  # POSIX bash snapshot path
def test_export_dump_drops_git_location_vars_and_keeps_user_exports():
    """Run the real dump under bash: a polluted worktree GIT_DIR must NOT
    survive ``export -p`` into the snapshot, while ordinary user exports do."""
    exports = "; ".join(
        [f'export {n}="/foreign/worktree/.git"' for n in GIT_LOCATION_VARS]
        + ['export MYVAR="keep"', 'export GIT_PAGER="cat"']
    )
    out = subprocess.run(
        ["bash", "-c", f"{exports}; {_export_dump_excluding_session_vars('/dev/stdout')}"],
        capture_output=True, text=True, check=True).stdout
    leaked = [n for n in GIT_LOCATION_VARS if f"declare -x {n}=" in out]
    assert not leaked, f"git location pointers persisted into the snapshot: {leaked}"
    # A clean fresh session keeps its own (absent) git location env: git resolves
    # by cwd, and `git rev-parse --show-toplevel` stays sane.
    assert 'declare -x MYVAR="keep"' in out
    assert 'declare -x GIT_PAGER="cat"' in out


@pytest.mark.platforms("posix")  # POSIX bash snapshot path
def test_fresh_session_after_polluted_parent_snapshot_resolves_its_own_repo(tmp_path):
    """End-to-end through LocalEnvironment: parent session works inside a real
    worktree (GIT_DIR-style pointers live in its env), a snapshot is taken, then
    a LATER session on the same backend must resolve its OWN cwd — not the
    parent's checkout."""
    from tools.environments.local import LocalEnvironment

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    worktree = tmp_path / "wt"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "wtbr", str(worktree)],
                   check=True, capture_output=True)

    env = LocalEnvironment(cwd=str(worktree), timeout=30)
    try:
        env.init_session()  # snapshot captured with the worktree live in env
        snap = env._snapshot_path
        if os.path.exists(snap):
            with open(snap) as f:
                assert "GIT_DIR=" not in f.read(), "worktree GIT_DIR leaked into the snapshot"
        # The snapshot's own view: location vars absent, git resolves by cwd.
        out = env.execute("echo \"[${GIT_DIR+set}]\"; git rev-parse --show-toplevel")
        assert "[set]" not in out["output"], f"snapshot re-asserts a foreign GIT_DIR: {out!r}"
        assert str(worktree).replace("\\", "/") in out["output"].replace("\\", "/"), (
            f"fresh session lost its own toplevel: {out!r}")
    finally:
        env.cleanup()
