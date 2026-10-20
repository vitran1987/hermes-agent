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


def test_env_supports_interpreter_refuses_unknown_versions(
    tmp_path,
):
    """When the interpreter has no version metadata, ``env_supports_interpreter`` returns False.

    A base interpreter (no venv, no pyvenv.cfg) cannot be verified — refuse the pairing
    rather than assume it matches. Callers then fall back to a self-consistent interpreter.
    """
    from pm.environments import env_supports_interpreter

    committed = _write_venv(tmp_path / "installs" / "gen" / "venv", python_version="3.14")
    # A file with no pyvenv.cfg at any walk-up candidate.
    base_interpreter = tmp_path / "uv" / "python" / "cpython-3.11-windows-x86_64-none" / "python.exe"
    base_interpreter.parent.mkdir(parents=True)
    base_interpreter.write_text("", encoding="utf-8")
    assert not env_supports_interpreter(committed, base_interpreter)

    # Both env has matching version: ABI is consistent.
    matching = _write_venv(tmp_path / "matching" / "venv", python_version="3.14")
    matching_python = matching / "Scripts" / "python.exe"
    assert env_supports_interpreter(committed, matching_python)

    # Versions differ: ABI mismatched.
    different = _write_venv(tmp_path / "different" / "venv", python_version="3.11")
    different_python = different / "Scripts" / "python.exe"
    assert not env_supports_interpreter(committed, different_python)
