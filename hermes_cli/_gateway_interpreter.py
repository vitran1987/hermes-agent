"""Interpreter selection for the on-demand (detached) gateway daemon.

``spawn_unmanaged_gateway`` built its child argv from the CALLER's
``sys.executable``. The child then loads the committed dependency environment's
site-packages, and a compiled extension is ABI-tagged (``cpXY``): CPython 3.11
handed a 3.14 tree resolves ``pydantic_core._pydantic_core`` to its stub
directory and dies with ``cannot import name 'ArgsKwargs'``. The daemon still
starts -- its A2A HTTP surface answers ``GET /.well-known/agent.json`` -- so the
break only shows on a session WebSocket upgrade, which then refuses every
worker dial with HTTP 500 (incident 2026-11-17 12:00: nine cards lost in seven
minutes because one owner could not serve its workers).

The dependency environment's own interpreter is ABI-consistent by construction:
it was built beside the site-packages it will load. This module selects it and
probes the caller before trusting it. A failure that every candidate shares
raises (:mod:`hermes_cli.gateway_runtime_service`'s ``RuntimeStartError``) --
a daemon that cannot import its own runtime must not come up looking healthy
(PROCESS-KEY-PRINCIPLES.md §4, no silent fallback).

The verdict is tri-state -- ``compatible`` / ``incompatible`` / ``unknown`` --
and only a VERIFIED incompatibility moves or refuses an interpreter. That is
the same contract as the worker-argv ABI swap (``fix/cron-windows-venv-abi-pair``):
an unreadable probe is a refusal to decide, not a defect, and an unverified
rewrite of a working interpreter is its own outage class.
"""
from __future__ import annotations

import os
import subprocess
import sys
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Sequence

#: Packages whose compiled extensions are ABI-locked to the interpreter.
#: ``pydantic_core`` is the canary that failed in production (it ships
#: ``_pydantic_core.cpXY-win_amd64.pyd``); ``openai`` is the first import the
#: daemon performs at boot and the one the WebSocket handler path rides.
PROBE_MODULES: tuple[str, ...] = ("pydantic_core", "openai")

#: Generous on purpose: a cold import of both packages on a busy Windows host
#: (antivirus first-scan of the freshly built environment) is seconds, not
#: milliseconds. A timeout is ``unknown`` -- it never refuses a spawn.
PROBE_TIMEOUT_SECONDS = 120.0


class ProbeVerdict(str, Enum):
    """Whether an interpreter can load the environment's compiled extensions."""

    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    #: The probe could not decide (timeout, unreadable interpreter). Never a defect.
    UNKNOWN = "unknown"


class GatewayInterpreterError(RuntimeError):
    """No candidate interpreter can import the dependency environment's runtime."""


def probe_interpreter(
    python: Path,
    site_packages: Path,
    *,
    modules: Sequence[str] = PROBE_MODULES,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> tuple[ProbeVerdict, str]:
    """Import *modules* the way the daemon will: env site-packages AHEAD of the interpreter's own.

    The shadowing is the whole point. A 3.11 launcher imports its own
    ``cp311`` ``pydantic_core`` happily when probed bare, and the same
    interpreter dies once the environment's 3.14 tree is on ``PYTHONPATH`` --
    which is what production does to every detached child. Probing without the
    overlay would pass the exact interpreter this is meant to reject.

    Returns ``(verdict, detail)``; *detail* is the interpreter's own last error
    line, so a refusal can name the module that could not be imported.
    """
    source = "import " + ", ".join(modules)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(site_packages), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    try:
        done = subprocess.run(
            [str(python), "-c", source],
            capture_output=True, text=True, env=env, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ProbeVerdict.UNKNOWN, f"{type(exc).__name__}: {exc}"
    if done.returncode == 0:
        return ProbeVerdict.COMPATIBLE, ""
    tail = (done.stderr or done.stdout or "").strip().splitlines()
    detail = tail[-1] if tail else f"exit {done.returncode}"
    if "ImportError" in detail or "ModuleNotFoundError" in detail:
        return ProbeVerdict.INCOMPATIBLE, detail
    # A non-import failure (a broken install, a policy block) says nothing about the ABI.
    return ProbeVerdict.UNKNOWN, detail


def _first_existing(paths: Iterable[Path]) -> list[Path]:
    """Distinct, existing interpreters in the order given."""
    seen: set[str] = set()
    ordered: list[Path] = []
    for path in paths:
        try:
            key = str(path.resolve())
        except OSError:
            continue
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        ordered.append(path)
    return ordered


def resolve_gateway_interpreter(
    project_root: Path,
    *,
    caller: Path | None = None,
    probe: Callable[[Path, Path], tuple[ProbeVerdict, str]] | None = None,
) -> Path:
    """The interpreter the on-demand gateway daemon must be spawned with.

    *caller* is the interpreter that would have been used before this module
    existed (``sys.executable``); *probe* is injected so tests need neither a
    foreign CPython nor this host's version. It is resolved at CALL time (not
    as a default argument) so a caller -- or a test -- that replaces the
    module-level probe is honoured; a probe captured at import time silently
    ignores every replacement, which reads as a passing test of nothing.

    No committed environment (a source checkout that PM never built) injects
    nothing, so the caller is kept: there is no ABI to mismatch. That is the
    only path on which ``sys.executable`` is trusted.
    """
    from pm.environments import committed_venv, site_packages, venv_python

    probe = probe or probe_interpreter

    root = Path(project_root).resolve()
    launcher = Path(caller) if caller is not None else Path(sys.executable)
    try:
        environment = committed_venv(root)
    except RuntimeError as exc:  # a recorded-but-unusable environment is loud, never a default
        raise GatewayInterpreterError(str(exc)) from exc
    if environment is None:
        return launcher
    own_python = venv_python(environment)
    if not own_python.is_file():
        raise GatewayInterpreterError(
            f"dependency environment has no interpreter: {own_python}"
        )
    site = site_packages(environment)
    if not site.is_dir():
        raise GatewayInterpreterError(
            f"dependency environment has no site-packages: {site}"
        )
    if launcher.resolve() == own_python.resolve():
        return launcher  # the environment's own interpreter: ABI-matched by construction
    verdict, detail = probe(launcher, site)
    if verdict is ProbeVerdict.COMPATIBLE:
        return launcher
    if verdict is ProbeVerdict.UNKNOWN:
        # An unreadable probe is not evidence of a broken caller; the environment's
        # own interpreter is still the safer bet, and it needs no second probe.
        return own_python
    own_verdict, own_detail = probe(own_python, site)
    if own_verdict is not ProbeVerdict.INCOMPATIBLE:
        return own_python
    raise GatewayInterpreterError(
        f"no ABI-compatible interpreter for {site}: "
        f"{launcher} -> {detail}; {own_python} -> {own_detail}"
    )
