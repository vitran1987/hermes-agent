"""The Windows profile wrapper carries exactly one CRLF per line, not two.

``create_wrapper_script`` wrote its payload with a literal ``"\\r\\n"`` while
``Path.write_text`` was called with the default ``newline=None``, so CPython
translated every ``"\\n"`` in that payload to ``os.linesep``. On Windows the
CRLF a caller had already written therefore became ``"\\r\\r\\n"`` — the same
doubling class ``utils/files.py::_atomic_write`` fixed for the atomic seam
(fork PR #31), at the one call site that genuinely wants CRLF.

``cmd.exe`` tolerates the doubled ``\\r`` today, so this is not cosmetic: the
wrapper bytes are what ``_is_our_wrapper`` and every downstream tool inspect,
and an EOL-doubling regression here is invisible until something parses the
line structure.

The contract is Windows-only — POSIX writes plain ``\\n`` and never translates,
so the arms are gated on ``windows`` rather than run twice and proved once.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.platforms("windows")


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    """Isolate ``Path.home()`` / ``HERMES_HOME`` so the wrapper lands in *tmp_path*."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    return tmp_path


def _wrapper_bytes(tmp_path: Path, alias: str, profile: str | None = None) -> bytes:
    from hermes_cli.profiles import create_wrapper_script

    # Guard the premise of the whole module: the code under test must be the
    # checkout this file lives in, not a site-packages install of the same tree.
    import hermes_cli.profiles as _profiles

    assert (
        Path(_profiles.__file__)
        .resolve()
        .is_relative_to(Path(__file__).resolve().parents[2])
    ), f"imported {_profiles.__file__}, expected the checkout under test"

    wrapper = (
        create_wrapper_script(alias, target=profile)
        if profile
        else create_wrapper_script(alias)
    )
    assert wrapper is not None
    assert wrapper.exists()
    assert wrapper.name == f"{alias}.bat"
    return wrapper.read_bytes()


def test_windows_wrapper_bytes_are_exactly_one_crlf_per_line(profile_env) -> None:
    """The wrapper is two lines, each terminated by a single CR LF."""
    body = _wrapper_bytes(profile_env, "mybot")

    assert body == b"@echo off\r\nhermes -p mybot %*\r\n"


def test_windows_wrapper_does_not_double_the_cr(profile_env) -> None:
    """A doubled CR (``\\r\\r\\n``) is the defect this module pins; it must be absent."""
    body = _wrapper_bytes(profile_env, "mybot")

    assert b"\r\r\n" not in body, f"doubled CRLF in wrapper bytes: {body!r}"
    assert body.count(b"\r") == 2, f"expected one CR per line, got {body!r}"
    assert body.count(b"\n") == 2, f"expected two lines, got {body!r}"


def test_windows_wrapper_with_target_profile_keeps_line_structure(profile_env) -> None:
    """An alias pointing at a differently-named profile is written the same way."""
    body = _wrapper_bytes(profile_env, "myalias", profile="coder")

    assert body == b"@echo off\r\nhermes -p coder %*\r\n"


def test_windows_wrapper_line_structure_survives_a_rewrite(profile_env) -> None:
    """Re-creating an existing wrapper (the overwrite path) does not accumulate CRs."""
    first = _wrapper_bytes(profile_env, "mybot")
    second = _wrapper_bytes(profile_env, "mybot")

    assert first == second == b"@echo off\r\nhermes -p mybot %*\r\n"


def test_host_is_windows() -> None:
    """The translation is Windows-only; a POSIX run of this module proves nothing."""
    assert sys.platform == "win32"
