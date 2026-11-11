"""Goal-judge handoff gates: ``parse_failed`` must fail open like ``transport_failed``.

``_parse_judge_response`` is documented fail-open: an empty or non-JSON judge reply comes back as
``("continue", "judge returned empty response"/"judge reply was not JSON: ...", True, None, False)``
— a *broken judge*, not a human "not done". Both terminal-handoff gates (``tools.kanban_tools`` for
worker tool calls, ``hermes_cli.kanban`` for the CLI) read ``transport_failed`` and fail open, but
dropped ``parse_failed`` (index 3), so a broken judge was ruled a genuine "not done":
``kanban_complete`` / ``kanban_request_review`` were rejected on every attempt, the goal loop burned
its turn budget, and the card could only be closed by hand (observed on a live goal_mode card: 8/8
rejections, then ``gave_up``).

These tests pin the contract on the ONE gate function each layer owns — which is what covers BOTH
terminal handoffs, since ``kanban_complete`` and ``kanban_request_review`` share it
(``tools/kanban_tools.py`` calls ``_goal_gate`` at both sites; the CLI routes both through
``_goal_mode_handoff_rejection``): a broken judge never wedges a handoff, while a genuine verdict —
including a genuine verdict carried by unusable output — still gates.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from hermes_cli import kanban as kanban_cli
from tools import kanban_tools

# (verdict, reason, parse_failed, wait_directive, transport_failed)
_TRANSPORT_FAILED = ("continue", "judge error: BadRequestError", False, None, True)
_GENUINE_CONTINUE = ("continue", "goal not met yet", False, None, False)
_GENUINE_BLOCKED = ("blocked", "goal unachievable as scoped", False, None, False)
# ``_parse_judge_response`` fail-open shapes — broken judge, not a verdict.
_PARSE_FAILED_EMPTY = ("continue", "judge returned empty response", True, None, False)
_PARSE_FAILED_NOT_JSON = ("continue", "judge reply was not JSON: 'oops'", True, None, False)
# A real verdict carried by unusable output (legacy/non-``continue`` shape) stays a real verdict.
_GENUINE_BLOCKED_PARSE_FAILED = ("blocked", "unachievable, said in prose", True, None, False)


def _task(tid="task-1"):
    return SimpleNamespace(id=tid, title="goal title", body="goal body", goal_mode=True)


def _aux_client():
    return patch(
        "agent.auxiliary_client.get_text_auxiliary_client",
        return_value=(object(), "test-model"),
    )


@pytest.mark.parametrize("handoff", ["kanban_complete", "kanban_request_review"],
                         ids=["complete", "request-review"])
@pytest.mark.parametrize("shape", [_PARSE_FAILED_EMPTY, _PARSE_FAILED_NOT_JSON],
                         ids=["empty", "not-json"])
def test_tool_gate_parse_failure_fails_open(handoff, shape):
    """A broken judge must not reject either handoff — the goal_mode wedge.

    One gate function, two call-sites: the wedge hit ``kanban_complete`` on one live card (8/8
    rejections) and ``kanban_request_review`` on another, so both are pinned here.
    """
    with patch.object(kanban_tools, "_goal_judge_available", return_value=True), \
            patch.object(kanban_tools, "judge_goal", return_value=shape):
        kanban_tools._goal_gate(handoff, _task(), "task-1", "ev")  # no raise


@pytest.mark.parametrize("shape", [_PARSE_FAILED_EMPTY, _PARSE_FAILED_NOT_JSON],
                         ids=["empty", "not-json"])
def test_cli_gate_parse_failure_fails_open(shape):
    """Same contract on the CLI gate: a broken judge allows the lifecycle handoff."""
    with _aux_client(), patch("hermes_cli.goals.judge_goal", return_value=shape):
        assert kanban_cli._goal_mode_handoff_rejection(_task(), "evidence") == ("done", None)


def test_tool_gate_parse_failure_does_not_hide_genuine_blocked_verdict():
    """``parse_failed`` only relaxes the unusable-output path: a ``blocked`` verdict delivered inside
    bad output must still gate, so the gate is not loosened wholesale."""
    with patch.object(kanban_tools, "_goal_judge_available", return_value=True), \
            patch.object(kanban_tools, "judge_goal", return_value=_GENUINE_BLOCKED_PARSE_FAILED):
        with pytest.raises(kanban_tools._Reject):
            kanban_tools._goal_gate("kanban_complete", _task(), "task-1", "ev")


def test_gates_still_reject_and_allow_after_the_parse_failure_fix():
    """No over-loosening: a genuine verdict still gates BOTH handoffs, ``done`` still passes, and
    the transport-failure fail-open keeps working alongside the parse-failure fail-open."""
    with patch.object(kanban_tools, "_goal_judge_available", return_value=True):
        for handoff in ("kanban_complete", "kanban_request_review"):
            for shape in (_GENUINE_CONTINUE, _GENUINE_BLOCKED):
                with patch.object(kanban_tools, "judge_goal", return_value=shape):
                    with pytest.raises(kanban_tools._Reject):
                        kanban_tools._goal_gate(handoff, _task(), "task-1", "ev")
            with patch.object(kanban_tools, "judge_goal",
                              return_value=("done", "ok", False, None, False)):
                kanban_tools._goal_gate(handoff, _task(), "task-1", "ev")  # no raise
            with patch.object(kanban_tools, "judge_goal", return_value=_TRANSPORT_FAILED):
                kanban_tools._goal_gate(handoff, _task(), "task-1", "ev")  # no raise
    with _aux_client(), patch("hermes_cli.goals.judge_goal", return_value=_GENUINE_BLOCKED):
        assert kanban_cli._goal_mode_handoff_rejection(_task(), "ev") == (
            "blocked", "goal unachievable as scoped")
