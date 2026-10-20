"""Worker argv preparation: interpreter selection for kanban worker spawns.

The dispatcher hands every worker an argv whose ``cmd[0]`` is an interpreter
(module form: ``[python, "-m", <module>, ...]``) or a resolved Hermes executable
(path form: ``[hermes.exe, <Hermes CLI args>...]``). When the committed
dependency environment was built for a different CPython, the worker inherits
site-packages it cannot load — compiled extensions are ABI-tagged ``cpXY`` — and
dies on its first compiled import. ``_swap_worker_argv_for_env_abi`` substitutes
the env's own executable (ABI-consistent by construction); the argv-resolution
helpers below serve ``kanban_db_dispatch._resolve_hermes_argv``'s path form.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _module_hermes_argv() -> list[str]:
    """Interpreter-bound Hermes CLI invocation (``hermes_cli.main`` is the
    console-script target — there is no top-level ``hermes`` package)."""
    return [sys.executable, "-m", "hermes_cli.main"]


def _absolute_hermes_path(path: str) -> str:
    """Return an absolute filesystem path for a resolved Hermes shim."""
    expanded = os.path.expanduser(path)
    return expanded if os.path.isabs(expanded) else os.path.abspath(expanded)


def _is_windows_batch_shim(path: str) -> bool:
    """Return true for Windows shell/batch shims that should not be argv[0]."""
    return path.lower().endswith((".cmd", ".bat"))


def _hermes_path_argv(path: str) -> list[str]:
    """argv for a resolved Hermes executable path. Windows batch shims
    (``.cmd``/``.bat``) are unsafe as argv[0] because the argument vector
    includes task-derived values; prefer the module form."""
    if sys.platform == "win32" and _is_windows_batch_shim(path):
        return _module_hermes_argv()
    return [_absolute_hermes_path(path)]


def _swap_worker_argv_for_env_abi(cmd: list[str], repo_root: Path) -> None:
    """Replace ``cmd[0]`` when the chosen interpreter cannot load the env's compiled extensions.

    A kanban worker inherits its dependencies from the committed environment:
    module-form workers bootstrap it via the worker client, path-form workers
    ride the dispatcher's own env. When the argv's interpreter was built for a
    different CPython than the env (``cp311`` store python reading
    ``cp314``-only ``pydantic_core``), every compiled import dies with
    ``ModuleNotFoundError``. Substitute the env's own executable — it was built
    alongside the env's site-packages, so the ABI always matches.

    The replacement follows the argv form: module form takes the env's python
    — same slot, same role; path form takes the env's own ``hermes`` executable
    — the Hermes CLI tail must stay attached to a Hermes executable, because a
    bare CPython dies on the first CLI option (``Unknown option: -p``).

    Only a verified mismatch fires the swap (the tri-state
    ``env_supports_interpreter`` verdict): ``unknown`` (either side has no
    readable version — a bare store python has no ``pyvenv.cfg`` at either
    walk-up level) or ``compatible`` leaves the argv the dispatcher handed us
    untouched. Falls back silently to the original cmd when the env or its
    executable is missing — the missing-tree error then surfaces at
    file-system level instead of a hidden swap.
    """
    from pm.environments import (
        AbiCompatibility,
        committed_venv,
        env_supports_interpreter,
        venv_hermes,
        venv_python,
    )

    module_form = cmd[1:3] == ["-m", "hermes_cli.main"]
    if not module_form and len(cmd) < 4:
        return  # path form needs at least [<exe>, "-p", <profile>, ...]
    environment = committed_venv(repo_root)
    if environment is None:
        return
    interpreter = Path(cmd[0])
    if not interpreter.is_file():
        return
    verdict = env_supports_interpreter(environment, interpreter)
    if verdict is not AbiCompatibility.incompatible:
        return
    replacement = venv_python(environment) if module_form else venv_hermes(environment)
    if replacement.is_file():
        cmd[0] = str(replacement)
