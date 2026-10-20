"""Windows cron scripts overlay the committed generation, never the stale in-tree venv.

``cron/scheduler_script.py::_windows_cron_python_invocation`` answers "which interpreter and
which site-packages?" for a cron script child. On a PM-managed install it read the dependency
tree through ``pm.environments.selected_venv``, which falls back to ``base_venv`` and answers
the leftover pre-PM ``<root>/venv`` when no generation is recorded. That tree is built for
whichever interpreter created it, so overlaying it on the managed store Python loads a cp311
``pydantic_core`` on 3.14 and every script dies with
``ModuleNotFoundError: No module named 'pydantic_core._pydantic_core'`` — the cron sibling of
the gateway crash in #122183/#123650, which no PR covered.

Invariant: with a generation committed, that generation is the tree the child gets — never the
in-tree venv.

When the store Python's ABI does NOT match the committed env's compiled extensions (a different
``cpXY`` is in the env's site-packages than the store Python would resolve), the store Python
would die on first import. Substitute the env's own interpreter — it was built alongside the
env's site-packages, so the ABI is guaranteed to match. See #1817 / PM root-cause 2026-10-20.

The kanban-side sibling is covered here too: ``hermes_cli._worker_argv_prep.
_swap_worker_argv_for_env_abi`` makes the same substitution for worker argvs — the env's
python for a module argv, the env's own ``hermes`` executable for a CLI argv — and only on a
verified ``incompatible`` verdict (the tri-state ``pm.environments.env_supports_interpreter``).
"""

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.platforms("windows")


def _write_pyvenv_cfg(venv: Path, *, version: str) -> Path:
    """A minimal ``pyvenv.cfg`` so the helper can read the venv's Python version.

    ``version`` is the ``X.Y`` string used both as the cpython-X.Y directory suffix and
    as the ``version_info`` line that ``venv_python_version`` actually parses.
    """
    major, _, minor = version.partition(".")
    (venv / "pyvenv.cfg").write_text(
        f"home = C:\\\\Users\\\\dummy\\\\uv\\\\python\\\\\\\\cpython-{version}-windows-x86_64-none\n"
        f"implementation = CPython\n"
        f"version_info = {major}.{minor}\n",
        encoding="utf-8",
    )
    return venv


def _write_venv(venv: Path, *, python_version: str = "3.14") -> Path:
    """A fake Windows venv with ``pyvenv.cfg`` and the ``Scripts/python.exe`` shim a cron child is handed.

    ``python_version`` must match between store and committed venvs for the original
    "store wins" path to fire; mismatched versions force the new ABI fallback.
    """
    (venv / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)
    (venv / "Scripts").mkdir(parents=True, exist_ok=True)
    (venv / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    _write_pyvenv_cfg(venv, version=python_version)
    return venv


def _site_packages_in(env_overlay: dict) -> Path | None:
    for entry in env_overlay.get("PYTHONPATH", "").split(os.pathsep):
        if Path(entry).name == "site-packages":
            return Path(entry)
    return None


def test_managed_install_uses_the_committed_generation_not_the_in_tree_venv(
    tmp_path, monkeypatch,
):
    """The committed generation wins; the leftover pre-PM venv never reaches the child."""
    from cron import scheduler_script as sched_script

    stale = _write_venv(tmp_path / "repo" / "venv", python_version="3.11")
    committed = _write_venv(tmp_path / "installs" / "gen" / "venv", python_version="3.14")
    store = tmp_path / "store" / "python.exe"
    store.parent.mkdir(parents=True)
    store.write_text("", encoding="utf-8")
    # Store python shares the committed env's Python (both 3.14 here) — the original
    # "store wins, committed site-packages" path is the path under test. The new ABI
    # guard is exercised in the dedicated ``test_managed_install_falls_back_...`` below.
    _write_pyvenv_cfg(store.parent, version="3.14")
    child = _write_venv(tmp_path / "child", python_version="3.14")

    monkeypatch.setattr("hermes_cli._launchers.resolve_store_python", lambda _root: store)
    monkeypatch.setattr("pm.environments.committed_venv", lambda _root: committed)
    # Base read selected_venv; pointing it at the stale venv is what makes this red there.
    monkeypatch.setattr("pm.environments.selected_venv", lambda _root: stale)

    interpreter, env_overlay = sched_script._windows_cron_python_invocation(
        str(child / "Scripts" / "python.exe")
    )

    # Store python wins; committed env's site-packages overlay; the in-tree stale venv
    # never contributes its interpreter OR its site-packages to the cron child.
    assert interpreter == str(store)
    overlay = _site_packages_in(env_overlay)
    assert overlay is not None, "the committed generation must supply a site-packages entry"
    assert overlay == committed / "Lib" / "site-packages"
    assert not overlay.is_relative_to(stale)


def test_managed_install_falls_back_to_env_python_on_abi_mismatch(
    tmp_path, monkeypatch,
):
    """When the store python is a different ABI than the env's compiled extensions, use the env's own python.

    Regression for the production-spawn crash: ``hermes-agent/venv`` ships cp311-only (or
    cp311+cp314 with the cp314 ext unused); the install's committed env ``f597...`` is
    3.14-only. Without this fallback, ``hermes-agent/venv/Scripts/python.exe`` + 3.14
    PYTHONPATH dies on the first ``import pydantic_core`` with
    ``ModuleNotFoundError: No module named 'pydantic_core._pydantic_core'``.
    """
    from cron import scheduler_script as sched_script

    stale = _write_venv(tmp_path / "repo" / "venv", python_version="3.11")
    # Committed env is 3.14 — its site-packages only carry cp314 .pyd files.
    committed = _write_venv(tmp_path / "installs" / "gen" / "venv", python_version="3.14")
    store = tmp_path / "store" / "python.exe"
    store.parent.mkdir(parents=True)
    store.write_text("", encoding="utf-8")
    # Store python is 3.11 — the exact mismatch the production install hit.
    _write_pyvenv_cfg(store.parent, version="3.11")
    child = _write_venv(tmp_path / "child", python_version="3.11")

    monkeypatch.setattr("hermes_cli._launchers.resolve_store_python", lambda _root: store)
    monkeypatch.setattr("pm.environments.committed_venv", lambda _root: committed)
    monkeypatch.setattr("pm.environments.selected_venv", lambda _root: stale)

    interpreter, env_overlay = sched_script._windows_cron_python_invocation(
        str(child / "Scripts" / "python.exe")
    )

    # The store python cannot load the env's site-packages (ABI). Fall back to the env's
    # own interpreter — built alongside the env's site-packages, always ABI-consistent.
    assert interpreter == str(committed / "Scripts" / "python.exe")
    overlay = _site_packages_in(env_overlay)
    assert overlay is not None
    assert overlay == committed / "Lib" / "site-packages"


def test_swap_worker_argv_path_form_swaps_to_env_hermes_exe(tmp_path, monkeypatch):
    """A CLI argv keeps its Hermes executable — a mismatched pair swaps to the env's hermes.

    Path-form argv is ``[<hermes.exe>, "-p", <profile>, ...]``; rewriting cmd[0] to a bare
    CPython hands the whole Hermes CLI tail to the interpreter, which dies with
    ``Unknown option: -p``. Only a verified ABI mismatch (3.11 interpreter venv vs 3.14
    env) fires the swap, and it must land on the env's own ``Scripts/hermes.exe`` — never
    the env's python.
    """
    from hermes_cli._worker_argv_prep import _swap_worker_argv_for_env_abi

    committed = _write_venv(tmp_path / "installs" / "gen" / "venv", python_version="3.14")
    (committed / "Scripts" / "hermes.exe").write_text("", encoding="utf-8")
    # The dispatcher's hermes.exe sits in a 3.11 venv: argv[0]'s walk-up finds
    # pyvenv.cfg=3.11 against the env's 3.14 → incompatible, so the swap fires.
    interpreter_venv = _write_venv(tmp_path / "interp", python_version="3.11")
    dispatch_hermes = interpreter_venv / "Scripts" / "hermes.exe"
    dispatch_hermes.write_text("", encoding="utf-8")

    monkeypatch.setattr("pm.environments.committed_venv", lambda _root: committed)
    monkeypatch.setattr(
        "pm.environments.venv_python", lambda _venv: committed / "Scripts" / "python.exe")
    monkeypatch.setattr(
        "pm.environments.venv_hermes", lambda _venv: committed / "Scripts" / "hermes.exe")

    cmd = [str(dispatch_hermes), "-p", "code-reviewer-bangioi", "--cli",
           "--accept-hooks", "chat", "-q", "hi"]
    _swap_worker_argv_for_env_abi(cmd, tmp_path / "repo")

    assert cmd[0] == str(committed / "Scripts" / "hermes.exe")
    assert cmd[0] != str(committed / "Scripts" / "python.exe")  # not the env's python
    assert cmd[0] != str(dispatch_hermes)  # not the original
    assert cmd[1:] == ["-p", "code-reviewer-bangioi", "--cli",
                       "--accept-hooks", "chat", "-q", "hi"]


def test_swap_worker_argv_module_form_swaps_to_env_python(tmp_path, monkeypatch):
    """Module-form argv keeps the interpreter slot — a mismatched interpreter becomes the env's python."""
    from hermes_cli._worker_argv_prep import _swap_worker_argv_for_env_abi

    committed = _write_venv(tmp_path / "installs" / "gen" / "venv", python_version="3.14")
    (committed / "Scripts" / "hermes.exe").write_text("", encoding="utf-8")
    # Store python is 3.11 — the exact mismatch the production install hit.
    store_venv = _write_venv(tmp_path / "store", python_version="3.11")
    store_python = store_venv / "Scripts" / "python.exe"

    monkeypatch.setattr("pm.environments.committed_venv", lambda _root: committed)
    monkeypatch.setattr(
        "pm.environments.venv_python", lambda _venv: committed / "Scripts" / "python.exe")
    monkeypatch.setattr(
        "pm.environments.venv_hermes", lambda _venv: committed / "Scripts" / "hermes.exe")

    cmd = [str(store_python), "-m", "hermes_cli.main", "-p",
           "code-reviewer-bangioi", "--cli", "chat"]
    _swap_worker_argv_for_env_abi(cmd, tmp_path / "repo")

    assert cmd[0] == str(committed / "Scripts" / "python.exe")
    assert cmd[1:] == ["-m", "hermes_cli.main", "-p",
                       "code-reviewer-bangioi", "--cli", "chat"]


def test_swap_worker_argv_skips_unknown_abi(tmp_path, monkeypatch):
    """A bare store python (no pyvenv.cfg at any walk-up level) yields ``unknown`` — no swap.

    The conservative path on an unverifiable pair is to keep the argv the dispatcher
    handed us, not to rewrite a working interpreter into an untested one.
    """
    from hermes_cli._worker_argv_prep import _swap_worker_argv_for_env_abi

    committed = _write_venv(tmp_path / "installs" / "gen" / "venv", python_version="3.14")
    (committed / "Scripts" / "hermes.exe").write_text("", encoding="utf-8")
    bare_store_python = tmp_path / "uv" / "python" / "cpython-3.11-windows-x86_64-none" / "python.exe"
    bare_store_python.parent.mkdir(parents=True)
    bare_store_python.write_text("", encoding="utf-8")

    # Replacements exist on disk: a wrong swap would be observable as a change.
    monkeypatch.setattr("pm.environments.committed_venv", lambda _root: committed)
    monkeypatch.setattr(
        "pm.environments.venv_python", lambda _venv: committed / "Scripts" / "python.exe")
    monkeypatch.setattr(
        "pm.environments.venv_hermes", lambda _venv: committed / "Scripts" / "hermes.exe")

    original = [str(bare_store_python), "-m", "hermes_cli.main", "-p",
                "code-reviewer-bangioi", "--cli", "chat"]
    cmd = list(original)
    _swap_worker_argv_for_env_abi(cmd, tmp_path / "repo")

    assert cmd == original


def test_swap_worker_argv_skips_when_already_compatible(tmp_path, monkeypatch):
    """Same version on both sides — the dispatcher's argv is left alone."""
    from hermes_cli._worker_argv_prep import _swap_worker_argv_for_env_abi

    committed = _write_venv(tmp_path / "installs" / "gen" / "venv", python_version="3.14")
    (committed / "Scripts" / "hermes.exe").write_text("", encoding="utf-8")
    matching_venv = _write_venv(tmp_path / "matching", python_version="3.14")
    matching_python = matching_venv / "Scripts" / "python.exe"

    monkeypatch.setattr("pm.environments.committed_venv", lambda _root: committed)
    monkeypatch.setattr(
        "pm.environments.venv_python", lambda _venv: committed / "Scripts" / "python.exe")
    monkeypatch.setattr(
        "pm.environments.venv_hermes", lambda _venv: committed / "Scripts" / "hermes.exe")

    original = [str(matching_python), "-m", "hermes_cli.main", "-p",
                "code-reviewer-bangioi", "--cli", "chat"]
    cmd = list(original)
    _swap_worker_argv_for_env_abi(cmd, tmp_path / "repo")

    assert cmd == original


def test_env_supports_interpreter_is_tri_state(tmp_path, monkeypatch):
    """The verdict has three states — ``unknown`` is a refusal to decide, not a mismatch."""
    from pm.environments import AbiCompatibility, env_supports_interpreter

    committed = _write_venv(tmp_path / "installs" / "gen" / "venv", python_version="3.14")

    # Same-version pair: ABI is consistent.
    matching = _write_venv(tmp_path / "matching" / "venv", python_version="3.14")
    matching_python = matching / "Scripts" / "python.exe"
    assert env_supports_interpreter(
        committed, matching_python) is AbiCompatibility.compatible

    # Versions differ: ABI mismatched.
    different = _write_venv(tmp_path / "different" / "venv", python_version="3.11")
    different_python = different / "Scripts" / "python.exe"
    assert env_supports_interpreter(
        committed, different_python) is AbiCompatibility.incompatible

    # Bare interpreter (no pyvenv.cfg at any walk-up candidate): unknown —
    # NOT the old bool collapse of unknown to False (incompatible).
    bare_interpreter = tmp_path / "uv" / "python" / "cpython-3.11-windows-x86_64-none" / "python.exe"
    bare_interpreter.parent.mkdir(parents=True)
    bare_interpreter.write_text("", encoding="utf-8")
    assert env_supports_interpreter(
        committed, bare_interpreter) is AbiCompatibility.unknown

    # Venv'd interpreter, missing-tree env: unknown as well.
    missing_tree = tmp_path / "missing"
    missing_tree.mkdir(parents=True)
    assert env_supports_interpreter(
        missing_tree, matching_python) is AbiCompatibility.unknown
