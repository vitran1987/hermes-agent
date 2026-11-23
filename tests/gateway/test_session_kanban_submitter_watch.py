"""The submitter watcher must never take the gateway interpreter down (kanban t_213dc719).

A worker that finishes its work goes away; its pid disappearing is not a reason to kill the
process that serves every other card. ``_watch_submitter`` used to answer it with ``os._exit(1)``,
so one worker finishing crashed the gateway and every other running worker with it (9
``exited UNCLEANLY`` lines in one day, 71% of that day's crashes booked as
"Kanban execution unknown; retained by owner").

The watcher records the attempt's run-scoped ``worker_lost`` note and stops; no ``worker_result``
is written, so the dead-worker sweep books that run's crash exactly as before.
"""
import os
import sqlite3
import threading
import time

from gateway import session_kanban as sk


def _run_row_db(path):
    """The two tables the watcher reads: the run row it polls and the event stream it writes."""
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, worker_pid INTEGER,'
                 ' worker_started_at INTEGER, claim_lock TEXT)')
    conn.execute('CREATE TABLE task_events (run_id INTEGER, kind TEXT)')
    conn.execute('INSERT INTO task_runs (id, task_id, worker_pid, worker_started_at, claim_lock)'
                 ' VALUES (7, ?, 424242, 1, ?)', ('t_dead', 'VI-WORKSPACE:1'))
    conn.commit()
    conn.close()
    return path


def test_watch_submitter_records_loss_without_exiting_the_gateway(tmp_path, monkeypatch):
    db = _run_row_db(tmp_path / 'kanban.db')
    monkeypatch.setattr(sk, '_TURNS_DONE', threading.Event())
    monkeypatch.setattr('hermes_cli.kanban_db_dispatch._worker_alive', lambda pid, started: False)
    losses, exits = [], []
    # raising=False: on the pre-fix code this attribute does not exist, and the test must fail on
    # the missing loss + the process exit, not on a monkeypatch error.
    monkeypatch.setattr(sk, '_record_worker_loss', raising=False,
                        value=lambda context, reason, *, pid=None, bound=False: losses.append(
                            (context['task_id'], context['run_id'], reason, pid)))
    monkeypatch.setattr(os, '_exit', lambda code: exits.append(code))

    sk._watch_submitter(db, 7)

    deadline = time.monotonic() + 10
    while not losses and time.monotonic() < deadline:
        time.sleep(0.05)
    assert exits == [], 'the watcher killed the shared gateway interpreter'
    assert losses == [('t_dead', 7, 'submitter_worker_gone', 424242)]
