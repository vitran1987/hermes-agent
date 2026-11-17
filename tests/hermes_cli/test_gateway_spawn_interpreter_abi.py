"""The on-demand gateway daemon is spawned with an ABI-compatible interpreter.

``spawn_unmanaged_gateway`` used to build the child argv from the CALLER's
``sys.executable``. The child loads the committed dependency environment's
site-packages, and a compiled extension is ABI-tagged: a CPython 3.11 caller
handed a 3.14 tree resolves ``pydantic_core._pydantic_core`` to its stub
directory and dies with ``ImportError: cannot import name 'ArgsKwargs'``.

The daemon still boots -- ``GET /.well-known/agent.json`` answers 200 -- so the
break only shows on a session WebSocket upgrade, which refuses every worker
dial with HTTP 500 (incident 2026-11-17 12:00). These tests pin the two halves:
the spawn picks the environment's own interpreter, and a runtime that NO
candidate can import refuses the spawn loudly instead of starting an owner that
serves HTTP and refuses WebSockets.

Neither the interpreter selection nor the refusal may depend on this host's
Python version: every case drives the resolver through an injected probe.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import _gateway_interpreter as interpreter
from hermes_cli._gateway_interpreter import (
    GatewayInterpreterError,
    ProbeVerdict,
    resolve_gateway_interpreter,
)


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def environment(tmp_path, monkeypatch):
    """A synthetic dependency environment + a foreign caller, wired into ``pm.environments``.

    Layout is patched rather than built: ``venv_bin_dir`` differs per OS and these
    tests are about the selection contract, not about distro paths.
    """
    root = tmp_path / "repo"
    root.mkdir()
    env = tmp_path / "environment"
    own_python = _touch(tmp_path / "environment" / "bin" / "python")
    site = tmp_path / "environment" / "lib" / "site-packages"
    site.mkdir(parents=True)
    foreign = _touch(tmp_path / "shims" / "python3.11")

    monkeypatch.setattr("pm.environments.committed_venv", lambda _root: env)
    monkeypatch.setattr("pm.environments.venv_python", lambda _venv, **_kw: own_python)
    monkeypatch.setattr("pm.environments.site_packages", lambda _venv: site)
    monkeypatch.setattr(interpreter.sys, "executable", str(foreign))
    return SimpleNamespace(root=root, env=env, own_python=own_python, site=site, foreign=foreign)


def _probe(*, caller: ProbeVerdict, own: ProbeVerdict = ProbeVerdict.COMPATIBLE):
    """A probe that answers per interpreter, without running one."""
    seen: list[Path] = []

    def probe(python: Path, site_packages: Path):
        seen.append(Path(python))
        verdict = caller if Path(python) == seen[0] else own
        detail = "" if verdict is ProbeVerdict.COMPATIBLE else (
            "ImportError: cannot import name 'ArgsKwargs' from 'pydantic_core._pydantic_core' "
            "(unknown location)"
        )
        return verdict, detail

    probe.seen = seen
    return probe


def _spawn(monkeypatch, home: Path, **kwargs):
    """Drive the real spawn path, capturing argv instead of executing it."""
    from hermes_cli import gateway_runtime_start as start

    captured: dict = {}

    def popen(command, **popen_kwargs):
        captured["argv"] = list(command)
        captured["kwargs"] = popen_kwargs
        return SimpleNamespace(pid=4242)

    # A POSIX platform keeps the plain-Popen branch: the same assertions hold on every
    # host, and the Windows pythonw normalization is covered by test_gateway_windows.py.
    monkeypatch.setattr(start, "sys", SimpleNamespace(platform="linux", executable=sys.executable))
    monkeypatch.setattr(start, "subprocess",
                        SimpleNamespace(Popen=popen, DEVNULL=subprocess.DEVNULL))
    child = start.spawn_unmanaged_gateway(home, deadline=float("inf"), **kwargs)
    return child, captured


def test_spawn_swaps_a_foreign_caller_for_the_environments_own_interpreter(
    environment, tmp_path, monkeypatch
):
    """AC1/AC2: a caller of another ABI never becomes the daemon's interpreter."""
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setattr(interpreter, "probe_interpreter", _probe(caller=ProbeVerdict.INCOMPATIBLE))

    _child, captured = _spawn(monkeypatch, home)

    argv = captured["argv"]
    assert argv[0] == str(environment.own_python), argv
    assert argv[0] != str(environment.foreign), argv
    # The Hermes CLI tail is untouched: only the interpreter slot moved.
    assert argv[1:3] == ["-m", "hermes_cli.main"], argv
    assert argv[3:] == ["--profile", "default", "gateway", "run", "--quiet"], argv


def test_spawn_keeps_a_compatible_caller(environment, tmp_path, monkeypatch):
    """No opportunistic rewrite: a caller that loads the environment is left alone."""
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setattr(interpreter, "probe_interpreter", _probe(caller=ProbeVerdict.COMPATIBLE))

    _child, captured = _spawn(monkeypatch, home)

    assert captured["argv"][0] == str(environment.foreign), captured["argv"]


def test_spawn_keeps_the_caller_when_no_environment_is_committed(tmp_path, monkeypatch):
    """A source checkout injects no site-packages, so there is no ABI to mismatch."""
    home = tmp_path / "profile"
    home.mkdir()
    caller = _touch(tmp_path / "interpreters" / "python")
    monkeypatch.setattr("pm.environments.committed_venv", lambda _root: None)
    monkeypatch.setattr(interpreter.sys, "executable", str(caller))
    monkeypatch.setattr(interpreter, "probe_interpreter", _probe(caller=ProbeVerdict.INCOMPATIBLE))

    _child, captured = _spawn(monkeypatch, home)

    assert captured["argv"][0] == str(caller), captured["argv"]


def test_an_undecidable_probe_never_refuses_the_spawn(environment, tmp_path, monkeypatch):
    """Tri-state: a timeout is a refusal to decide, not a defect (no false refusal)."""
    monkeypatch.setattr(interpreter, "probe_interpreter", _probe(caller=ProbeVerdict.UNKNOWN))

    chosen = resolve_gateway_interpreter(environment.root)

    assert chosen == environment.own_python


def test_no_compatible_interpreter_refuses_the_spawn_loudly(environment, tmp_path, monkeypatch):
    """AC3: the failure is loud, and it names the interpreter and the missing module."""
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setattr(interpreter, "probe_interpreter",
                        _probe(caller=ProbeVerdict.INCOMPATIBLE, own=ProbeVerdict.INCOMPATIBLE))

    from hermes_cli.gateway_runtime_service import RuntimeStartError

    with pytest.raises(RuntimeStartError) as refused:
        _spawn(monkeypatch, home)

    error = refused.value
    # The stable reason code stays a token; the detail carries the diagnosis.
    assert error.reason == "gateway_interpreter_incompatible"
    assert error.detail
    assert str(environment.foreign) in str(error)
    assert "ArgsKwargs" in str(error)
    assert "pydantic_core" in str(error)


def test_a_refused_spawn_does_not_start_a_daemon(environment, tmp_path, monkeypatch):
    """AC3: no process is created at all -- never a daemon that boots and then 500s."""
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setattr(interpreter, "probe_interpreter",
                        _probe(caller=ProbeVerdict.INCOMPATIBLE, own=ProbeVerdict.INCOMPATIBLE))
    from hermes_cli import gateway_runtime_start as start
    from hermes_cli.gateway_runtime_service import RuntimeStartError

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a refused spawn must not create a process")

    monkeypatch.setattr(start, "sys", SimpleNamespace(platform="linux", executable=sys.executable))
    monkeypatch.setattr(start, "subprocess", SimpleNamespace(Popen=forbidden, DEVNULL=-3))

    with pytest.raises(RuntimeStartError):
        start.spawn_unmanaged_gateway(home, deadline=float("inf"))


def test_a_missing_environment_interpreter_is_refused_not_substituted(
    environment, tmp_path, monkeypatch
):
    """An unusable environment record is an error, never a silent fall back to the caller."""
    monkeypatch.setattr("pm.environments.venv_python",
                        lambda _venv, **_kw: tmp_path / "gone" / "python")

    with pytest.raises(GatewayInterpreterError, match="no interpreter"):
        resolve_gateway_interpreter(environment.root)


def test_a_missing_site_packages_tree_is_refused(environment, tmp_path, monkeypatch):
    monkeypatch.setattr("pm.environments.site_packages",
                        lambda _venv: tmp_path / "gone" / "site-packages")

    with pytest.raises(GatewayInterpreterError, match="no site-packages"):
        resolve_gateway_interpreter(environment.root)


def test_the_own_interpreter_is_never_probed(environment, monkeypatch):
    """The environment's own interpreter is ABI-matched by construction: no subprocess cost."""
    monkeypatch.setattr(interpreter.sys, "executable", str(environment.own_python))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the environment's own interpreter must not be probed")

    monkeypatch.setattr(interpreter, "probe_interpreter", forbidden)

    assert resolve_gateway_interpreter(environment.root) == environment.own_python


# --- AC4: the incident's signature is HTTP up + session WebSocket 500 -------------------------
#
# ``/.well-known/agent.json`` is served by the A2A adapter and the session WebSocket by
# ``gateway.run_api``/``/api/ws``. Both are reached from the daemon the spawn starts, so the
# interpreter that can import both handler modules is the interpreter that serves both
# surfaces; the one that cannot is the daemon that answers HTTP and refuses every upgrade.

_SURFACES = ("gateway.run_api", "plugins.platforms.a2a.adapter")


@pytest.mark.skipif(sys.platform != "win32", reason="the ABI incident is the Windows venv pair")
def test_the_spawned_interpreter_serves_both_surfaces_while_the_caller_cannot():
    """AC1 + AC4, measured on a real committed environment (skipped where none is installed)."""
    from pm.environments import committed_venv, site_packages

    repo = Path(__file__).resolve().parents[2]
    try:
        environment = committed_venv(repo)
    except RuntimeError as exc:
        pytest.skip(f"unusable committed environment: {exc}")
    if environment is None:
        pytest.skip("no committed dependency environment on this host")

    site = site_packages(environment)
    chosen = resolve_gateway_interpreter(repo)
    verdict, detail = interpreter.probe_interpreter(chosen, site, modules=("pydantic_core", *_SURFACES))
    assert verdict is ProbeVerdict.COMPATIBLE, (chosen, detail)

    foreign = repo / "venv" / "Scripts" / "python.exe"  # the pre-PM 3.11 shim the incident ran
    if not foreign.is_file():
        pytest.skip("no foreign-ABI launcher beside the install")
    foreign_verdict, foreign_detail = interpreter.probe_interpreter(
        foreign, site, modules=("pydantic_core", *_SURFACES))
    if foreign_verdict is not ProbeVerdict.INCOMPATIBLE:
        pytest.skip(f"this host has no ABI-mismatched launcher: {foreign_verdict} {foreign_detail}")
    assert "pydantic_core" in foreign_detail
    # The same interpreter the resolver refuses is the one that would 500 every upgrade.
    assert chosen != foreign
