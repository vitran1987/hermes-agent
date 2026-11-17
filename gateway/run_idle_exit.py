"""Idle exit for an auto-started (unmanaged) gateway.

A client that finds no gateway for its home starts a detached ``gateway run --idle-exit``
(``hermes_cli.gateway_runtime_start``). Nothing else ever stops that process, so every throwaway home
(scripts, tests, one-shot ``chat -q``) used to leave a ~270 MB daemon behind. This watcher ends it once
it has served nobody and has nothing to do for ``gateway.unmanaged_idle_exit_seconds`` (default 600,
``0`` disables):

- no attached client (live ticketed WebSocket, outstanding ticket, or recent HTTP/WS activity);
- no messaging adapter configured, connected or reconnecting (any profile);
- no runnable cron job and no kanban task the dispatcher must act on for a profile it serves;
- no queued/started admission, live managed worker, in-flight agent/cron/API run, background
  process or async delegation, active heartbeat/loop, pending handoff or hosted-room task.

Every probe fails CLOSED: an unreadable source counts as work, so the gateway stays up. A gateway with
a supervisor (systemd/launchd/Desktop/external) or started without ``--idle-exit`` (installed services,
``hermes gateway run`` by hand, ``hermes gateway ensure`` from Desktop) never arms this. The exit
publishes ``draining`` with ``drain_reason: idle_exit`` before anything else, so a client that races it
waits for the process to go and starts a fresh one instead of failing.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger("gateway.run")

DEFAULT_IDLE_EXIT_SECONDS = 600.0
IDLE_EXIT_REASON = "idle_exit"
# Head of the ``exit_reason`` persisted with ``stopped``: a reconnecting client's ``--recover``
# (ui-tui/scripts/gateway_bootstrap.py) tells this self-ended exit from an operator stop by it.
IDLE_EXIT_STOP_PREFIX = "auto-started gateway idle"
# Kanban statuses the embedded dispatcher acts on without a human (promotion, spawn, reclaim).
_KANBAN_DUE_STATUSES = ("scheduled", "ready", "running", "review")


def idle_exit_seconds(runner) -> float:
    """``gateway.unmanaged_idle_exit_seconds`` from the launch home; 0 disables, junk keeps the default."""
    from gateway.run import _load_gateway_config
    section = (_load_gateway_config() or {}).get("gateway")
    value = section.get("unmanaged_idle_exit_seconds") if isinstance(section, dict) else None
    if value is None:
        return DEFAULT_IDLE_EXIT_SECONDS
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        logger.warning("gateway.unmanaged_idle_exit_seconds=%r is not a number; using %.0f", value,
                       DEFAULT_IDLE_EXIT_SECONDS)
        return DEFAULT_IDLE_EXIT_SECONDS
    return max(0.0, seconds)


def note_client_activity(runner) -> None:
    runner._idle_exit_last_activity = time.monotonic()


@contextlib.contextmanager
def attached_client(runner):
    """Count one live client connection for the idle check (and stamp activity on both ends)."""
    note_client_activity(runner)
    runner._idle_exit_clients = getattr(runner, "_idle_exit_clients", 0) + 1
    try:
        yield
    finally:
        runner._idle_exit_clients -= 1
        note_client_activity(runner)


def _messaging_busy(runner) -> str | None:
    from gateway.config import Platform
    live = list(getattr(runner, "adapters", None) or {})
    for adapters in (getattr(runner, "_profile_adapters", None) or {}).values():
        live.extend(adapters)
    for pending in (getattr(runner, "_profile_failed_platforms", None) or {}).values():
        live.extend(pending or {})
    live.extend(getattr(runner, "_failed_platforms", None) or {})
    config = getattr(runner, "config", None)
    if config is not None:
        live.extend(p for p, cfg in config.platforms.items() if getattr(cfg, "enabled", False))
    live = {getattr(p, 'value', str(p)) for p in live if p != Platform.LOCAL}  # LOCAL never holds the gateway
    return f"messaging {sorted(live)}" if live else None


def _served_homes(runner) -> list[Path]:
    from gateway.run import _cron_tick_profile_homes
    from gateway.session_authorities import all_authorities
    homes = {Path(a.profile_id) for a in all_authorities(runner)}
    homes.update(Path(h) for _name, h in _cron_tick_profile_homes(runner.config))
    return sorted(homes)


def _cron_busy(homes) -> str | None:
    from cron.jobs import is_job_runnable, is_terminal_job, load_jobs, use_cron_store
    from cron.scheduler import get_running_job_ids
    if get_running_job_ids():
        return "cron job running"
    for home in homes:
        if not (home / "cron" / "jobs.json").exists():
            continue
        with use_cron_store(home):
            jobs = load_jobs()
        if any(is_job_runnable(job) and not is_terminal_job(job) for job in jobs):
            return f"cron jobs scheduled in {home}"
    return None


def _served_profile_names(homes) -> frozenset[str]:
    """The profile ids behind ``homes`` — the only kanban assignees that can hold this gateway up.

    A home that is not a profile home (a throwaway home a script, a test or a one-shot
    ``chat -q`` started) names no profile: no card is attributable to it, so it holds nothing.
    """
    from hermes_cli.profiles import normalize_profile_name
    from hermes_constants import profile_name_for_home
    names = set()
    for home in homes:
        name = profile_name_for_home(home)
        if not name:
            continue
        try:
            names.add(normalize_profile_name(name))
        except ValueError:
            continue
    return frozenset(names)


def _own_claim_lock() -> str:
    """This process's kanban claim lock (``<host>:<pid>``, see ``kanban_db._claimer_id``)."""
    from hermes_cli.kanban_db import _claimer_id
    return _claimer_id()


def _kanban_busy(homes) -> str | None:
    """A due kanban card *this* gateway answers for, or ``None``.

    Scoped per owner, not per board. A board-global probe made one due card anywhere hold every
    ``--idle-exit`` gateway on the host, so no unmanaged daemon could ever retire while the board was
    busy for somebody else. A card holds this gateway when either

    - it is assigned to a profile this gateway serves (:func:`_served_homes`) — the work its
      dispatcher, ledger and adapters act on; or
    - this process's own dispatcher already claimed it (``claim_lock`` is ``<host>:<pid>``), whatever
      the assignee: the spawn is ours, so retiring now would strand it.

    A card for an unserved profile, and a card no profile can own (unassigned, or a label like a
    tier name the dispatcher parks as non-spawnable), holds nothing — the gateway that would act on
    it is a different one.
    """
    from hermes_cli import kanban_db
    from hermes_cli.profiles import normalize_profile_name
    served = _served_profile_names(homes)
    try:
        own_lock = _own_claim_lock()
    except Exception:
        own_lock = ""
    marks = ",".join("?" * len(_KANBAN_DUE_STATUSES))
    for meta in kanban_db.list_boards(include_archived=False):
        slug = meta.get("slug")
        path = kanban_db.kanban_db_path(board=slug)
        if not path.exists():
            continue
        with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)) as conn:
            for assignee, claim_lock in conn.execute(
                    f"SELECT assignee, claim_lock FROM tasks WHERE status IN ({marks})",
                    _KANBAN_DUE_STATUSES):
                if own_lock and claim_lock == own_lock:
                    return f"kanban work this gateway claimed on board {slug}"
                try:
                    canonical = normalize_profile_name(assignee)
                except ValueError:
                    continue
                if canonical in served:
                    return f"kanban work for {canonical} on board {slug}"
    return None


def _ledger_busy(runner) -> str | None:
    from gateway.session_authorities import all_authorities
    for authority in all_authorities(runner):
        db = authority.db
        if db._read_all("SELECT 1 FROM session_admissions WHERE status IN ('queued','started') LIMIT 1"):
            return f"admissions pending in {authority.profile_id}"
        if db._read_all("SELECT 1 FROM worker_executions WHERE status != 'terminal' LIMIT 1"):
            return f"managed worker live in {authority.profile_id}"
        service = getattr(authority, "hosted_room_service", None)
        if service is not None:
            status = service.status()
            if status.get("current_tasks") or status.get("leased_rooms"):
                return f"hosted-room work in {authority.profile_id}"
    return None


def _profile_start_busy(runner) -> str | None:
    """A profile hot-serve is still building has no authority to walk yet, but its queued admissions
    are recovered once it starts; exiting now would cancel that start. A filled or failed start
    leaves the registry's starting set, so the hold ends with it."""
    registry = getattr(runner, "session_authorities", None)
    starting = registry.starting_homes() if registry is not None else ()
    return f"profile starting in {starting[0]}" if starting else None


def _store_gates_busy(homes) -> str | None:
    from gateway.run_idle_gates import (
        profile_has_active_heartbeat, profile_has_active_loop, profile_has_pending_handoff)
    for home in homes:
        for label, gate in (("heartbeat", profile_has_active_heartbeat), ("loop", profile_has_active_loop),
                            ("handoff", profile_has_pending_handoff)):
            if gate(home):
                return f"active {label} in {home}"
    return None


def _attaching(runner) -> bool:
    tickets = getattr(runner, "session_ticket_store", None)
    return tickets is not None and tickets.outstanding()


def _client_busy(runner, window: float) -> str | None:
    clients = getattr(runner, "_idle_exit_clients", 0)
    if clients:
        return f"{clients} client(s) attached"
    if _attaching(runner):
        return "client attaching"
    quiet = time.monotonic() - getattr(runner, "_idle_exit_last_activity", 0.0)
    if quiet < window:
        return f"client activity {quiet:.0f}s ago"
    return None


def busy_reason(runner, *, window: float, count_clients: bool = True) -> str | None:
    """Why the gateway must stay up, or None when it may exit. Fails closed on any error.

    ``count_clients=False``: a restart is pending, and an attached or returning client is exactly who
    needs the fresh gateway; every duty nothing would restart (adapters, cron, admissions) still counts.
    A client mid-attach (unredeemed ticket) still holds it: its first connect would be refused with
    no rediscovery, while an attached client waits out the drain and starts the fresh gateway.
    """
    try:
        if count_clients and (reason := _client_busy(runner, window)):
            return reason
        if not count_clients and _attaching(runner):
            return "client attaching"
        if runner._draining or not runner._running:
            return "already stopping"
        if runner._active_work_count():
            return "agent work in flight"
        if runner._scale_to_zero_has_live_background_work():
            return "background work in flight"
        homes = _served_homes(runner)
        return (_messaging_busy(runner) or _ledger_busy(runner) or _profile_start_busy(runner)
                or _cron_busy(homes) or _kanban_busy(homes) or _store_gates_busy(homes))
    except Exception as exc:
        logger.debug("idle-exit probe failed; staying up", exc_info=True)
        return f"idle probe failed ({type(exc).__name__})"


def _begin_idle_exit(runner, window: float, restart: str | None = None) -> None:
    """Withdraw admission synchronously (no await between the last idle check and here), then stop."""
    runner._draining = True
    runner.session_runtime_descriptor.update(state="draining", capabilities=[], drain_reason=IDLE_EXIT_REASON)
    runner._exit_reason = (f"{IDLE_EXIT_STOP_PREFIX}; restarting to load {restart}" if restart else
                           f"{IDLE_EXIT_STOP_PREFIX} for {window:.0f}s (gateway.unmanaged_idle_exit_seconds)")
    logger.info("Exiting: %s; the next client starts a fresh gateway", runner._exit_reason)
    runner._idle_exit_stop_task = asyncio.ensure_future(runner.stop())


async def _sleep_unless_restart_pending(interval: float) -> None:
    """Sleep ``interval``, waking within a second of a pending restart: a user retrying right after
    the failed turn must reach a fresh gateway, not this one. Always sleeps at least one slice, so a
    pending restart held up by other work is re-probed once a second, never in a spin."""
    from pm.environments_adopt import restart_pending
    deadline = time.monotonic() + interval
    while True:
        await asyncio.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        if restart_pending() or time.monotonic() >= deadline:
            return


async def _retired_for_restart(runner, window: float) -> bool:
    """Retire as soon as nothing but clients holds the gateway when a dependency install this process
    ran can only load in a new one (``pm.environments_adopt.restart_pending``). Its clients wait out the
    ``idle_exit`` drain and start a fresh gateway, which boots the new generation."""
    from pm.environments_adopt import restart_pending
    if not (restart := restart_pending()):
        return False
    if await asyncio.to_thread(busy_reason, runner, window=window, count_clients=False) is not None:
        return False
    if busy_reason(runner, window=window, count_clients=False) is not None:
        return False
    _begin_idle_exit(runner, window, restart=restart)
    return True


async def unmanaged_idle_exit_watcher(runner) -> None:
    window = idle_exit_seconds(runner)
    if window <= 0:
        logger.info("Auto-started gateway idle exit disabled (gateway.unmanaged_idle_exit_seconds=0)")
        return
    logger.info("Auto-started gateway: exits after %.0fs with no clients, adapters, cron/kanban work or "
                "admissions (gateway.unmanaged_idle_exit_seconds)", window)
    interval = min(30.0, max(0.5, window / 4))
    idle_since = None
    while runner._running and not runner._draining:
        await _sleep_unless_restart_pending(interval)
        window = idle_exit_seconds(runner)
        if window <= 0:
            idle_since = None
            continue
        if await _retired_for_restart(runner, window):
            return
        # The store/cron/kanban reads touch disk; keep them off the loop, then re-check the
        # loop-only facts (clients, tickets, draining) synchronously right before acting.
        reason = await asyncio.to_thread(busy_reason, runner, window=window)
        if reason is not None:
            idle_since = None
            continue
        idle_since = idle_since or time.monotonic()
        if time.monotonic() - idle_since < interval and window > interval:
            continue  # two consecutive idle readings, so one racing write cannot slip between them
        if busy_reason(runner, window=window) is None:
            _begin_idle_exit(runner, window)
            return


def arm_unmanaged_idle_exit(runner) -> bool:
    """Start the watcher for an ``--idle-exit`` launch that no supervisor owns. Returns whether it armed."""
    from gateway.control_socket import _detect_supervisor
    supervisor = _detect_supervisor()
    if supervisor != "manual":
        logger.info("Auto-started gateway idle exit not armed: supervised by %s", supervisor)
        return False
    note_client_activity(runner)
    runner._spawn_supervised(lambda: unmanaged_idle_exit_watcher(runner), "unmanaged_idle_exit_watcher")
    return True
