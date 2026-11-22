"""The Windows ``.cmd`` launcher carries exactly one CRLF per line, not two.

``mint_launcher`` builds a two-line payload holding a literal ``"\\r\\n"`` and
hands it to ``Path.write_text`` with the default ``newline=None``, so the text
layer translated every ``"\\n"`` in that payload to ``os.linesep``. On Windows
the CRLF the caller had already written therefore landed on disk as
``"\\r\\r\\n"`` — the same doubling class ``hermes_cli/profiles.py`` had for the
profile wrapper (fork PR #32) and ``utils/files.py::_atomic_write`` had for the
atomic seam (fork PR #31). ``hermes.cmd`` and ``hermes-acp.cmd`` on this host
both carry it (measured read-only: ``40 65 63 68 6f 20 6f 66 66 0d 0d 0a``).

``cmd.exe`` tolerates the doubled ``\\r``, so this is not a parse bug today: the
bytes are the launcher's contract, and ``_launcher_python`` already had to grow
a "skip a blank line" rule to read its own output. The fix keeps that reader
rule (launchers written before the fix are on disk and must keep parsing) and
removes the doubling at the writer.

The contract is Windows-only — the POSIX arm writes ``"\\n"`` and never
translates — so the arms are gated on ``windows`` rather than run twice.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.platforms("windows")

# The writer's own promise, restated: "@echo off" then '"<python>" -I -c "<code>" %*'.
_LINE_COUNT = 2


def _mint_cmd(tmp_path: Path, monkeypatch) -> Path:
    """Mint the ``.cmd`` fallback for a bare fixture root and return its path."""
    from hermes_cli import _launchers

    # Guard the premise of the module: the code under test must be the checkout
    # this file lives in, not a site-packages install of the same tree.
    assert (
        Path(_launchers.__file__)
        .resolve()
        .is_relative_to(Path(__file__).resolve().parents[2])
    ), f"imported {_launchers.__file__}, expected the checkout under test"

    # Force the command-file arm: with distlib present the same call publishes a
    # native ``.exe`` and the ``.cmd`` writer never runs.
    monkeypatch.setattr(_launchers, "_load_script_maker", lambda: None)
    monkeypatch.setattr(_launchers, "_store_script_maker", lambda python_exe: None)

    root = tmp_path / "repo"
    root.mkdir()
    out = tmp_path / "bin"
    out.mkdir()
    launcher = _launchers.mint_launcher("hermes", root, out, Path(sys.executable), None)
    assert launcher is not None, "mint_launcher published nothing"
    assert launcher.name == "hermes.cmd"
    return launcher


def _lines(data: bytes) -> list[bytes]:
    """Split *data* on its own CRLF pairs, asserting nothing else terminates a line."""
    parts = data.split(b"\r\n")
    assert parts[-1] == b"", f"payload does not end with a CRLF: {data[:80]!r}…"
    lines = parts[:-1]
    assert len(lines) == _LINE_COUNT, f"expected {_LINE_COUNT} lines, got {lines!r}"
    assert not any(b"\r" in line or b"\n" in line for line in lines), (
        f"a line carries a stray CR/LF of its own: {data[:120]!r}…"
    )
    return lines


def test_minted_cmd_holds_exactly_one_crlf_per_line(tmp_path, monkeypatch) -> None:
    """Two lines, each terminated by a single CR LF — the bytes a launcher is."""
    data = _mint_cmd(tmp_path, monkeypatch).read_bytes()

    assert b"\r\r" not in data, f"doubled CR in the launcher bytes: {data[:120]!r}…"
    assert data.count(b"\n") == _LINE_COUNT, (
        f"expected {_LINE_COUNT} line ends, got {data!r}"
    )
    lines = _lines(data)

    assert lines[0] == b"@echo off"
    assert lines[1].startswith(b'"') and b'" -I -c "' in lines[1]
    assert lines[1].endswith(b'" %*')


def test_minted_cmd_rewrite_does_not_accumulate_crs(tmp_path, monkeypatch) -> None:
    """Re-minting over an existing launcher (the overwrite path) keeps the shape."""
    from hermes_cli import _launchers

    launcher = _mint_cmd(tmp_path, monkeypatch)
    first = launcher.read_bytes()
    again = _launchers.mint_launcher(
        "hermes", tmp_path / "repo", tmp_path / "bin", Path(sys.executable), None
    )

    assert again == launcher
    assert again is not None
    assert again.read_bytes() == first
    assert b"\r\r" not in first


def test_reader_still_parses_a_doubled_launcher_written_before_the_fix(
    tmp_path,
) -> None:
    """Users already have doubled launchers on disk; the reader must keep reading them."""
    from hermes_cli import _launchers

    python_exe = Path(sys.executable)
    # Byte-for-byte what the pre-fix writer produced on Windows: the payload's
    # own CRLF plus the extra CR the text layer translated in.
    legacy = tmp_path / "legacy.cmd"
    legacy.write_bytes(
        b"@echo off\r\r\n"
        + f'"{python_exe}" -I -c "import base64; exec(base64.b64decode(\'eA==\'))" %*\r\r\n'.encode()
    )

    assert b"\r\r\n" in legacy.read_bytes()
    assert _launchers._launcher_python(legacy) == python_exe


def test_reader_parses_the_minted_launcher(tmp_path, monkeypatch) -> None:
    """The reader reads the fixed shape too — one rule, both files on disk."""
    from hermes_cli import _launchers

    launcher = _mint_cmd(tmp_path, monkeypatch)

    assert _launchers._launcher_python(launcher) == Path(sys.executable)


def test_cmd_exe_launches_a_single_crlf_launcher(tmp_path) -> None:
    """Line structure is what cmd.exe parses — prove it executes, do not assume it."""
    python_exe = Path(sys.executable)
    code = "print('launcher-ran')"
    body = f'@echo off\r\n"{python_exe}" -I -c "{code}" %*\r\n'
    launcher = tmp_path / "probe.cmd"
    # The fixed writer's contract: verbatim bytes, no os.linesep translation.
    launcher.write_text(body, encoding="utf-8", newline="")

    assert b"\r\r" not in launcher.read_bytes()
    finished = subprocess.run(
        ["cmd.exe", "/c", str(launcher)],
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )

    assert finished.returncode == 0, finished.stderr
    assert "launcher-ran" in finished.stdout


def test_host_is_windows() -> None:
    """The translation is Windows-only; a POSIX run of this module proves nothing."""
    assert sys.platform == "win32"
