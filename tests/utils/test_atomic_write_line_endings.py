"""Atomic writers emit the payload they were handed, not a platform-normalised copy.

``_atomic_write`` opened its temp file as text with the default ``newline=None``, so CPython
translated every ``"\\n"`` to ``os.linesep`` on write. On Windows that made **every** config
write — ``hermes config set``, ``save_config()``, ``write_config_document()``, a
``_config_version`` migration during ``hermes update``, ``sync-hermes-profiles.ps1`` — rewrite
the whole file as CRLF, whatever the caller asked for and whatever the checkout's
``* text eol=lf`` pin said (47 of 48 live profile configs inside 6 s during the 2026-11-20
update). A payload that already contained ``"\\r\\n"`` was translated a second time into
``"\\r\\r\\n"``, so the translation was never merely cosmetic.

The contract under test is host-independent — "write these bytes" — but only Windows executes
the translation, so these arms run on Windows.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from utils import atomic_json_write, atomic_roundtrip_yaml_save, atomic_write_text


pytestmark = pytest.mark.platforms("windows")


def test_atomic_write_text_keeps_lf(tmp_path: Path) -> None:
    """An LF payload reaches the file as LF, not as CRLF."""
    target = tmp_path / "config.yaml"

    atomic_write_text(target, "model:\n  default: test-model\n")

    assert target.read_bytes() == b"model:\n  default: test-model\n"


def test_atomic_write_text_does_not_double_a_crlf_payload(tmp_path: Path) -> None:
    """Existing CRLF content is written once — translation used to produce CR CR LF."""
    target = tmp_path / "notes.md"

    atomic_write_text(target, "a\r\nb\r\n")

    assert target.read_bytes() == b"a\r\nb\r\n"


def test_atomic_json_write_keeps_lf(tmp_path: Path) -> None:
    """JSON state files stay LF (a CRLF rewrite changes bytes for no reader's benefit)."""
    target = tmp_path / "state.json"

    atomic_json_write(target, {"a": 1, "b": 2})

    body = target.read_bytes()
    assert b"\r" not in body
    assert json.loads(body) == {"a": 1, "b": 2}


def test_atomic_roundtrip_yaml_save_keeps_lf(tmp_path: Path) -> None:
    """The config-save path behind ``hermes config set`` must leave an LF file."""
    target = tmp_path / "config.yaml"
    target.write_bytes(b"model:\n  default: test-model\n")

    atomic_roundtrip_yaml_save(target, {"model": {"default": "other-model"}})

    body = target.read_bytes()
    assert b"\r" not in body
    assert b"default: other-model" in body
