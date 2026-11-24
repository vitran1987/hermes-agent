"""Dispatcher subprocess: submit one claim to its profile owner; never run an agent."""
# Spawned as a bare ``sys.executable -m``: in a PM install that interpreter carries no dependencies
# until hermes_bootstrap selects the committed environment, so it must be the first import.
try:
    import hermes_bootstrap
except ModuleNotFoundError as exc:
    if exc.name != "hermes_bootstrap" and not "hermes_bootstrap".startswith(f"{exc.name}."):
        raise

import asyncio
from contextlib import closing
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import time
from typing import Optional

# A worker that loses its owner (a dropped socket, a restart, `hermes update` draining it or a new
# owner still starting) waits for it instead of exiting: the condition is the host's, not the card's,
# and a failed exit here spends the card's failure budget and lets the owner end the turn. Asking
# again is safe: ``kanban.run`` is idempotent per attempt and ``prompt.receipt`` is a read.
# ``OWNER_WAIT_S`` is the longest the owner may stay silent between answers before the worker
# gives up (the first ``kanban.run`` answer has its own wait, ``admission_answer_budget``).
OWNER_WAIT_S = 180.0
# ... EXCEPT while the owner says it is coming back: ``starting``/``draining`` is a restart in
# progress, not a failure, and a restart can legitimately outlast the silence budget. On 2026-11-17
# developer-bangioi's owner was dead for 453 s (12:00:38 watchdog exit -> 12:08:11 new owner) against
# this 180 s budget, so every worker gave up 186-248 s in, ~3 minutes before a replacement existed,
# and each exit was booked as a card crash. The revival budget must cover the worst measured owner
# downtime with a margin: a waiting worker is cheap, a card that spends its failure budget on the
# host's outage is not.
OWNER_REVIVAL_WAIT_S = 900.0
_TRANSIENT_OWNER_STATES = frozenset({'draining', 'starting'})
# In-band owner refusals that leave the attempt intact: a draining owner admits nothing new, and a
# retired authority's successor holds the admission the next connection reads.
_TRANSIENT_RPC_REASONS = frozenset({'runtime_draining', 'runtime_retired'})
# Control-socket answers a restarting owner gives (an ``inaccessible`` owner's reason, from
# discovery or the ticket fetch): a reset or half-written answer, or a replaced owner refusing a
# stale instance. Unsafe nodes or peers, a refused authorization, a bad pointer or a failed start
# stay final.
_RESTART_CONTROL_REASONS = frozenset({'invalid_control_peer', 'incomplete_control_response',
                                      'invalid_control_response'})
# Owner refusals that mean "not yet", not "this card is broken" (t_bbc0d14b). Measured on
# 2026-11-22: 24 worker exits in one hour, every one of them booked as a card crash and charged to
# the card's failure budget, from a gateway that was merely starting or that had lost a spawn race
# against the owner already running. A card whose work never started must not pay for the host.
#
# Kept deliberately narrow — only reasons that a LATER spawn can heal:
#   runtime_exited  the gateway this client launched died; the next client starts a fresh one
#   deadline        the boot window expired; the lane is retried by the supervisor
# NOT included, with the reason each is excluded:
#   profile_parked  the profile is parked on purpose; no owner is coming, so waiting 900 s hides it
#   inaccessible    not a reason code at all (it is the STATE) — the real control-socket refusals
#                   are `unsafe_control_peer` / `unsafe_control_permissions` / `windows_breakaway_
#                   unavailable`, all configuration defects whose retry would bury the real fix
_TRANSIENT_REFUSAL_REASONS = frozenset({'runtime_exited', 'deadline'})
# Past the board lock wait: the owner's session setup and receipt before and after the write.
ADMISSION_ANSWER_MARGIN_S = 60.0


def admission_answer_budget():
    """Seconds to wait for ``kanban.run``'s answer: the longest the owner's admission write can
    wait on the board lock (``write_txn`` retries ``BEGIN IMMEDIATE`` ``_BUSY_MAX_RETRIES`` times,
    each under the busy timeout) plus ``ADMISSION_ANSWER_MARGIN_S``."""
    from hermes_cli import kanban_db_connect
    lock_wait = (kanban_db_connect._BUSY_MAX_RETRIES + 1) * kanban_db_connect._resolve_busy_timeout_ms() / 1000
    return lock_wait + ADMISSION_ANSWER_MARGIN_S


class OwnerUnavailable(ValueError):
    """The profile owner could not be reached, and the card is not at fault.

    Raised only after a failure that passed ``_restartable`` — an owner that was
    starting/draining past its revival budget, or a transport that stayed lost — so the reason is
    always a host condition. ``_submit`` maps it to ``KANBAN_TRANSIENT_OWNER_EXIT_CODE`` and the
    dispatcher books the RUN ``blocked``, releases the card and does NOT count a failure
    (t_bbc0d14b). A plain ``GatewayClientError`` would be booked as a crash and spend the card's
    failure budget.

    ``ValueError`` is the same base ``GatewayClientError`` uses, so existing callers that treat an
    owner refusal as a value error keep working; it is NOT imported from ``gateway_client``
    because this module delays that import to ``run()`` (the interpreter's bootstrap comes first).
    """


@dataclass
class _Claim:
    """What the owner has said about this attempt, carried across connections."""

    answered: float  # monotonic time of the owner's last answer
    session_id: Optional[str] = None
    admission_id: Optional[str] = None
    revival_noted: bool = False  # the "waiting for a replacement" line was printed

    def silent_for(self):
        return time.monotonic() - self.answered


def _note_owner_revival(exc, claim):
    """Say once, on stderr, that this worker is waiting out the owner's restart.

    Without it the only trace of a long wait is the eventual failure line, which reads like a
    worker fault — the 2026-11-17 incident was diagnosed from crash payloads that could not tell
    the two apart.
    """
    if claim.revival_noted or _owner_budget(exc) <= OWNER_WAIT_S:
        return
    claim.revival_noted = True
    print(f'Kanban owner restarting ({exc}); waiting up to {_owner_budget(exc):.0f}s for the '
          'replacement to answer (the worker is not at fault)', file=sys.stderr, flush=True)


async def run(params, board_db):
    from agent.retry_utils import jittered_backoff
    from hermes_cli.gateway_client import connect_gateway, GatewayClientError, outside_restart_wait
    from websockets.exceptions import WebSocketException
    if os.environ.get('HERMES_TUI_GATEWAY_URL'):
        raise GatewayClientError('Kanban requires the assigned local profile owner')
    # A lost socket is not a lost attempt, and exiting here would let ``_watch_submitter`` end the
    # owner's in-flight turn, so the next connection resumes whatever the last one reached. The
    # wait is bounded from the owner's last answer: an owner that keeps answering keeps the
    # worker, and one that stays silent for ``OWNER_WAIT_S`` fails the worker honestly.
    claim = _Claim(answered=time.monotonic())
    failures = 0
    while True:
        seen = claim.answered
        try:
            # Never into a restart's after-turn wait: it cut this socket so the turn's submitter
            # waits for the NEXT owner, and a worker polling its receipt inside the wait keeps the
            # turn it waits on alive, holding `hermes update` until restart_after_turn_timeout.
            with outside_restart_wait():
                async with connect_gateway() as client:
                    # kanban.run subscribes this socket to the card's session, but only receipts
                    # matter here: discard the turn's events, or they fill the client's bounded
                    # queue and its reader stops, with the next receipt behind it.
                    drain = asyncio.create_task(_discard(client.events))
                    try:
                        receipt = await _await_receipt(client, params, board_db, claim)
                    finally:
                        drain.cancel()
        # A restart also surfaces as a refused or reset dial, a failed handshake, or an answer
        # that never came.
        except (GatewayClientError, WebSocketException, OSError, TimeoutError) as exc:
            if not _restartable(exc):
                raise
            _note_owner_revival(exc, claim)
            if claim.silent_for() >= _owner_budget(exc):
                # WebSocket errors can embed remote bodies; only our own messages are quoted.
                status = getattr(getattr(exc, 'response', None), 'status_code', None)
                detail = exc if isinstance(exc, GatewayClientError) else type(exc).__name__ + (
                    f' (HTTP {status})' if status else '')
                # Every exception that reaches here passed ``_restartable``: the owner was
                # restarting (draining/starting) or the transport was lost, never a verdict on
                # this card. Report it as a host outage — the dispatcher books the RUN
                # ``blocked`` and releases the card without spending its failure budget
                # (t_bbc0d14b: 24 such exits in one hour were booked as card crashes).
                raise OwnerUnavailable(
                    f'Kanban owner unavailable for {claim.silent_for():.0f}s: {detail}') from exc
        else:
            return _exit_code(receipt, params, board_db)
        failures = 1 if claim.answered != seen else failures + 1
        # Jittered (1-2 s first, up to 16 s): workers of one dead owner do not all redial, and so
        # start a replacement, in the same instant.
        await asyncio.sleep(jittered_backoff(failures, base_delay=1.0, max_delay=8.0, jitter_ratio=1.0))


def _restartable(exc):
    """False when *exc* is the owner's own final answer; True for a restarting owner or a lost transport."""
    from hermes_cli.gateway_client import GatewayRPCError, GatewayUnavailableError
    if isinstance(exc, GatewayRPCError):
        return str(exc) in _TRANSIENT_RPC_REASONS
    if isinstance(exc, GatewayUnavailableError):
        return (exc.state in _TRANSIENT_OWNER_STATES
                or exc.reason_code in _RESTART_CONTROL_REASONS
                or _is_transient_refusal(exc))
    return True


def _is_transient_refusal(exc):
    """*exc* says the owner is not reachable YET (starting, duplicate-instance race, control socket
    refusing a stale instance) rather than that this card is broken.

    This is the difference between re-queuing a card and burning its failure budget: before
    t_bbc0d14b only ``draining``/``starting`` retried, so a ``Gateway inaccessible`` /
    ``runtime_exited`` / ``profile_parked`` / ``deadline`` refusal exited the worker at once and
    was booked ``crashed``.
    """
    from hermes_cli.gateway_client import GatewayUnavailableError
    if not isinstance(exc, GatewayUnavailableError):
        return False
    if exc.state in _TRANSIENT_OWNER_STATES:
        return True
    return exc.reason_code in _TRANSIENT_REFUSAL_REASONS


def _owner_budget(exc):
    """Seconds this worker may stay silent before giving up on the owner.

    A ``starting``/``draining`` owner is a restart in progress: someone is bringing a replacement
    up, so the worker waits the revival budget instead of the silence budget. The same applies to
    a transient refusal (``inaccessible`` / ``runtime_exited`` / ``profile_parked`` / ``deadline``):
    a gateway boot measured at ~4 minutes must not be counted against a 180 s silence budget.
    Every other restartable failure keeps ``OWNER_WAIT_S``.
    """
    if _is_transient_refusal(exc):
        return OWNER_REVIVAL_WAIT_S
    return OWNER_WAIT_S


async def _discard(events):
    while True:
        await events.get()


async def _await_receipt(client, params, board_db, claim):
    """Submit the claim (or, on a later connection, re-read the admission it got) and return its
    settled receipt, recording each owner answer on *claim*."""
    if claim.admission_id is None:
        db = str(await asyncio.to_thread(Path(board_db).resolve))
        # The owner answers only after it checks the claim and writes its ``owner_admitted`` marker
        # in one board write transaction. The board (the cross-profile dispatch bus) can make that
        # wait out its busy timeout, so the 30 s default left the owner admitting an attempt whose
        # submitter had exited (the later-bound worker's submitter-liveness watcher ended it, and the
        # admission was left ``unknown``). The wait is still finite: once the marker commits,
        # ``enforce_max_runtime`` and TTL reclaim skip the run (``owner_reclaim_paused``), so an owner
        # that stalls after admitting is bounded by this wait and ``OWNER_WAIT_S``.
        accepted = await client.rpc('kanban.run', _timeout=admission_answer_budget(), **dict(params, db=db))
        receipt = accepted['receipt']
        claim.session_id, claim.admission_id = accepted['session_id'], receipt['admission_id']
        print(f'Session: {claim.session_id}', file=sys.stderr)
    else:
        receipt = await _receipt(client, claim)
    claim.answered = time.monotonic()
    while receipt['status'] not in {'terminal', 'unknown'}:
        await asyncio.sleep(.2)
        receipt = await _receipt(client, claim)
        claim.answered = time.monotonic()
    return receipt


def _exit_code(receipt, params, board_db):
    from hermes_cli.gateway_client import GatewayClientError
    if receipt['status'] == 'unknown':
        # ``unknown`` is the owner saying "I could not settle this admission", not "the card
        # failed". The board is the authority: ask it before booking a crash (t_bbc0d14b).
        reconciled = _reconcile_unknown(params, board_db)
        if reconciled is not None:
            return reconciled
        raise GatewayClientError('Kanban execution unknown; retained by owner')
    from gateway.session_kanban import worker_result
    result = worker_result(board_db, params)
    # The attempt's reply ends the log, as a one-shot worker's did: crash diagnostics quote it.
    if result.get('last_output'):
        print(result['last_output'])
    return result.get('exit_code', 1)


# The board write the owner needed lost its busy timeout (measured 2026-11-23 13:03: three workers
# completed inside 16 s and were booked ``crashed`` with "Kanban execution unknown"). The worker
# re-reads the board with escalating lock waits — the contention that produced ``unknown`` is
# exactly what a longer wait absorbs, so the work is often already settled.
_RECONCILE_BUSY_TIMEOUTS_S = (5.0, 15.0, 30.0)


def _read_settlement(board_db, params, busy_timeout_s):
    """``("result", payload)`` / ``("ended", status)`` for this exact attempt, else ``None``.

    ``result``: the ``worker_result`` event the dispatcher reads — the attempt's own answer.
    ``ended``: the run was closed by another path (the board SWEPT it), so the card already owns
    the outcome and re-reporting it would book a phantom failure.
    """
    import sqlite3
    from gateway.session_kanban import worker_result
    result = worker_result(board_db, params, busy_timeout=busy_timeout_s)
    if result:
        return ('result', dict(result))
    with closing(sqlite3.connect(Path(board_db).resolve().as_uri() + '?mode=ro', uri=True,
                                 timeout=busy_timeout_s)) as conn:
        conn.execute(f'PRAGMA busy_timeout = {int(busy_timeout_s * 1000)}')
        row = conn.execute(
            "SELECT ended_at, outcome FROM task_runs WHERE id = ? AND task_id = ?",
            (params['run_id'], params['task_id']),
        ).fetchone()
    if row and row[0] is not None:
        return ('ended', str(row[1] or 'ended'))
    return None


def _reconcile_unknown(params, board_db):
    """Exit code when the board settles this attempt, else ``None`` (the caller fails closed).

    Fail-closed is unchanged: a board that cannot be read, or an attempt nothing settled, still
    raises. What changes is that a SETTLED attempt is no longer reported as a crash.
    """
    last_error = None
    for attempt, busy_timeout in enumerate(_RECONCILE_BUSY_TIMEOUTS_S, start=1):
        try:
            settled = _read_settlement(board_db, params, busy_timeout)
        except Exception as exc:  # sqlite3.Error, OSError, a missing board file
            last_error = exc
            continue
        if settled is None:
            continue
        kind, value = settled
        if kind == 'result' and isinstance(value, dict):
            if value.get('last_output'):
                print(value['last_output'])
            print(f'RECONCILED: attempt {params["run_id"]} of {params["task_id"]} settled on the '
                  f'board after {attempt} read(s) — not a crash (the owner reported it unknown)',
                  file=sys.stderr, flush=True)
            return int(value.get('exit_code', 0))
        print(f'RECONCILED: attempt {params["run_id"]} of {params["task_id"]} was already closed '
              f'by the board (outcome {value}) — not a crash', file=sys.stderr, flush=True)
        return 0
    if last_error is not None:
        print(f'Kanban board unreadable while reconciling an unknown admission: {last_error}',
              file=sys.stderr, flush=True)
    return None


async def _receipt(client, claim):
    return await client.rpc('prompt.receipt', session_id=claim.session_id, admission_id=claim.admission_id)


def main():
    # The dead-worker sweep books this process's exit from the trailer in its log (it never
    # reaps us), so every exit path writes it — the same contract as a quiet one-shot worker.
    from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER
    rc = 1
    try:
        rc = _submit()
    finally:
        print(f'\n{KANBAN_WORKER_EXIT_TRAILER}{int(rc)}', file=sys.stderr, flush=True)
    return rc


def _submit():
    try:
        params = {'board': os.environ['HERMES_KANBAN_BOARD'], 'task_id': os.environ['HERMES_KANBAN_TASK'],
            'run_id': int(os.environ['HERMES_KANBAN_RUN_ID']), 'claim_lock': os.environ['HERMES_KANBAN_CLAIM_LOCK']}
        board_db = os.environ['HERMES_KANBAN_DB']
        # Owner startup must never inherit this worker's task/tool/goal identity.
        for key in list(os.environ):
            # The busy timeout is a host tuning knob, not task identity: the answer budget reads it.
            if key.startswith('HERMES_KANBAN_') and key not in ('HERMES_KANBAN_HOME', 'HERMES_KANBAN_BUSY_TIMEOUT_MS'):
                os.environ.pop(key)
        for key in ('HERMES_SESSION_SOURCE', 'HERMES_TENANT', 'TERMINAL_CWD'):
            os.environ.pop(key, None)
        return asyncio.run(run(params, board_db))
    except OwnerUnavailable as exc:
        # A host condition, not a card fault: the owner was starting/draining or the transport
        # stayed lost for the whole revival budget. Exit with the transient sentinel so the
        # dispatcher books the RUN ``blocked`` and re-queues the card WITHOUT spending its
        # failure budget. Booking this as ``crashed`` (t_bbc0d14b: 24 exits in one hour) is what
        # ground the board to 2 running / 49 blocked.
        from hermes_cli.kanban_db import KANBAN_TRANSIENT_OWNER_EXIT_CODE
        print(f'Kanban owner unavailable (host condition, not a card fault): {exc}', file=sys.stderr)
        return KANBAN_TRANSIENT_OWNER_EXIT_CODE
    except (KeyError, ValueError, OSError) as exc:
        print(f'Kanban owner refused: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
