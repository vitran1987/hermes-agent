"""A lost ``worker.persist`` receipt is an OWNER fault, not a storage fault: it must classify
honestly and render its own catalog text in every locale (fix card t_9d90c842, C1).

Measured defect (diagnosis card t_091b2345): ``classify_persistence_error`` returned ``unknown`` for
``worker_receipt_timeout`` -- the message carries no ``locked``/``busy``/``disk`` marker -- so
``_persistence_explanation_key`` selected ``explainer.persistence.default``, whose text tells the
operator "the drive is out of room, or another Hermes process is holding the database". Both were
measured FALSE on this host (126 GB free, one holder per store, zero ``database is locked`` events);
the real cause was an owner gateway whose event loop stalled past the client's receipt budget.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import hermes_yaml as yaml

from agent.turn_explainers import _persistence_explanation_key
from hermes_state_errors import (
    PERSISTENCE_ERROR_CAUSES,
    WorkerReceiptTimeout,
    is_worker_receipt_timeout,
)

LOCALES_DIR = Path(__file__).resolve().parents[2] / "locales"
NEW_KEY = "explainer.persistence.owner_unresponsive"


def _locale_files():
    return sorted(p for p in LOCALES_DIR.glob("*.yaml") if not p.name.endswith(".tui.yaml"))


def _explainer_block(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return (yaml.safe_load(handle) or {}).get("explainer", {}).get("persistence", {})


def test_receipt_timeout_classifies_as_owner_unresponsive_by_type():
    from hermes_state import classify_persistence_error

    cause = classify_persistence_error(WorkerReceiptTimeout("worker_receipt_timeout"))
    assert cause == "owner_unresponsive"
    assert cause in PERSISTENCE_ERROR_CAUSES
    assert _persistence_explanation_key(cause) == NEW_KEY
    assert _persistence_explanation_key(cause) != "explainer.persistence.default"
    assert is_worker_receipt_timeout(WorkerReceiptTimeout("worker_receipt_timeout"))


def test_receipt_timeout_classifies_by_wrapped_message_too():
    """RPC-wrapped errors lose the type; the message must still classify (never ``unknown``)."""
    from hermes_state import classify_persistence_error

    for probe in (
        TimeoutError("worker_receipt_timeout"),
        "worker_receipt_timeout",
        "worker.persist failed: worker_receipt_timeout",
    ):
        assert classify_persistence_error(probe) == "owner_unresponsive", probe
        assert is_worker_receipt_timeout(probe), probe


def test_receipt_timeout_is_not_a_sqlite_lock():
    """The lock predicate stays False: a receipt miss is not contention, and the lease retry
    branch must not have to pretend it is one (it takes the same lane explicitly)."""
    from hermes_state_errors import is_sqlite_lock_error

    assert is_sqlite_lock_error(WorkerReceiptTimeout("worker_receipt_timeout")) is False


def test_default_explanation_unreachable_for_a_receipt_timeout():
    """The rendered turn-ending text is the new catalog entry, never the disk/lock fallback."""
    from hermes_state import classify_persistence_error
    from agent.turn_explainers import TurnExplainersMixin

    cause = classify_persistence_error(WorkerReceiptTimeout("worker_receipt_timeout"))
    assert _persistence_explanation_key(cause) == NEW_KEY
    text = TurnExplainersMixin._format_turn_completion_explanation(
        "session_persistence_failed", cause
    ).lower()
    assert text, "the new key rendered nothing: it fell through to no explanation at all"
    # The old ``default`` text claimed both of these; both were measured FALSE on this host.
    assert "out of room" not in text
    assert "holding the database" not in text
    assert "gateway" in text


def test_new_cause_key_present_in_every_locale():
    files = _locale_files()
    assert len(files) >= 17, [p.name for p in files]
    missing = []
    for path in files:
        value = _explainer_block(path).get("owner_unresponsive")
        if not isinstance(value, str) or not value.strip():
            missing.append(path.name)
            continue
        if "{profile_arg}" not in value:
            missing.append(f"{path.name} (no {{profile_arg}})")
    assert not missing, f"explainer.persistence.owner_unresponsive missing/empty in: {missing}"


def test_new_cause_text_is_not_the_default_text():
    files = _locale_files()
    defaults = {path.name: _explainer_block(path).get("default") for path in files}
    for path in files:
        assert _explainer_block(path)["owner_unresponsive"] != defaults[path.name], path.name
    english = _locale_files()[[p.name for p in _locale_files()].index("en.yaml")]
    en_text = _explainer_block(english)["owner_unresponsive"].lower()
    assert "healthy" in en_text
    assert "out of room" not in en_text
    assert "holding the database" not in en_text
