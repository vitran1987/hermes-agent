"""Re-snapshot core source into the selected generation's workspace.

Why this exists
---------------
``pm.packages.Venv.expected_stamp`` hashes the *dependency* inputs -- uv.lock, the packaging
files, the enabled extras, the python target and the plugin members -- and **no core source
bytes**. A code-only core change (same version, same lock, same extras, same plugins) therefore
leaves the stamp identical: ``pm.install._sync_locked`` computes ``current=True``,
``_commit_selection`` skips ``Venv.apply()``, and ``Venv.apply()`` is the only caller of
``pm.workspace._copy_core_inputs`` -- the only code that re-snapshots core source into
``installs/<key>/environments/<env>/workspace/**``, the tree the managed venv's editable finder
imports. ``hermes pm repair`` does not help either: it replays the recorded workspace
(``lock_and_sync(replay=...)`` copies the stale tree onto itself).

So a reviewed core fix on the same version could only reach the running generation by a hand
write into ``installs/*/workspace/**`` -- the one operation the ``hermes-core-immutable``
decision matrix labels ``INSTALLED-GENERATION-EDIT``. This module is the sanctioned writer:
``hermes pm republish-source`` mirrors the build inputs (``pm.workspace.iter_core_inputs``, the
exact set a rebuild copies, so the two cannot drift) into the selected generation, one
atomically replaced file at a time, under the install's runtime lock, with a journal that rolls
the whole publish back and a verification pass that re-hashes what it wrote.

What it never touches
---------------------
``GENERATION_OWNED`` root files (the generated ``pyproject.toml`` with the workspace members,
the resolved ``uv.lock``, ``core-uv.lock``), plugin member snapshots under ``plugin-sources/``,
``__pycache__``/``*.egg-info``/store entries under the workspace, and any file the source no
longer has (those are reported as stale; ``--prune`` removes them, and that is the only
deletion).

Measured cost (2026-11-26, this Windows host, 7077-file / 83.9 MiB selection)
----------------------------------------------------------------------------
* comparison by (size, mtime) when nothing changed: **1.1 s**;
* a full ``--check`` plan against the live install (9 changed files, 29 new, hashed to
  decide): **4.3 s**;
* one-file publish end to end, CLI included (plan + lock + journal + re-hash verify):
  **5.2 s**; a no-op re-run costs the same order (4-15 s, on-access-scanner noise);
* worst case (empty workspace, every file copied): **111 s**;
* the stamp-based alternative (hash the same set inside ``expected_stamp``, which runs on every
  ``venv_is_current`` probe) measured **~530 s** for a full content hash, and even a stat-only
  digest (~1.1 s) would tax every activation.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from pm.package import InstallError
from pm.workspace import GENERATION_OWNED, _core_input_ignored, core_input_dirs, iter_core_inputs

_CHUNK = 1 << 20
# Directories a generation creates inside its workspace that a source refresh must never treat
# as "no longer in source" (they are not build inputs at all).
_NEVER_PRUNED = frozenset({"plugin-sources", "__pycache__"})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _same_bytes(left: Path, right: Path) -> bool:
    """Content equality, cheap first: size, then a chunked compare (never both files in memory)."""
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        with open(left, "rb") as a, open(right, "rb") as b:
            while True:
                chunk_a = a.read(_CHUNK)
                chunk_b = b.read(_CHUNK)
                if chunk_a != chunk_b:
                    return False
                if not chunk_a:
                    return True
    except OSError:
        return False


@dataclass
class Plan:
    """What a republish would do (``check``) or did."""

    source: Path
    workspace: Path
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: int = 0
    stale: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    dirty: list[str] = field(default_factory=list)
    provenance_checked: bool = False
    journal: str | None = None
    duration_ms: int = 0

    @property
    def pending(self) -> bool:
        """True when the workspace does not already carry the source snapshot."""
        return bool(self.created or self.updated or self.stale or self.pruned)

    def as_dict(self) -> dict:
        return {
            "source": str(self.source),
            "workspace": str(self.workspace),
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "stale": self.stale,
            "pruned": self.pruned,
            "skipped_generation_owned": self.skipped,
            "dirty_source": self.dirty,
            "provenance_checked": self.provenance_checked,
            "journal": self.journal,
            "duration_ms": self.duration_ms,
            "pending": self.pending,
        }


def _matches(relative: Path, only: tuple[str, ...]) -> bool:
    if not only:
        return True
    posix = relative.as_posix()
    return any(posix == entry or posix.startswith(entry.rstrip("/") + "/") for entry in only)


def _dirty_source(source: Path, relatives: set[str]) -> tuple[list[str], bool]:
    """The planned paths that are uncommitted in *source*, and whether provenance could be checked.

    Only reviewed bytes may be published, so an uncommitted touched path is a refusal. A source
    that is not a git work tree (or whose ``git status`` fails) is reported as *unchecked* --
    never as clean: the caller decides, and the output says so.
    """
    if not (source / ".git").exists():
        return [], False
    try:
        completed = subprocess.run(
            ["git", "-C", str(source), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            capture_output=True, timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return [], False
    if completed.returncode != 0:
        return [], False
    records = completed.stdout.split(b"\0")
    dirty: set[str] = set()
    skip_next = False
    for record in records:
        if skip_next:
            skip_next = False  # porcelain -z spells a rename's source path in its own record
            continue
        if len(record) < 4:
            continue
        status, path = record[:2], record[3:].decode("utf-8", "replace")
        dirty.add(path)
        if b"R" in status or b"C" in status:
            skip_next = True
    return sorted(relatives & dirty), True


def _atomic_write(destination: Path, data: bytes) -> None:
    """Replace one file atomically, then bump its mtime so no stale ``.pyc`` can satisfy it."""
    directory = destination.parent
    directory.mkdir(parents=True, exist_ok=True)
    staging = directory / f".{destination.name}.republish-{uuid.uuid4().hex}"
    try:
        with open(staging, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, destination)
        os.utime(destination, None)
    finally:
        staging.unlink(missing_ok=True)


def _publish_file(source_file: Path, destination: Path) -> None:
    """The write seam: one source file into the workspace (tests replace this to force a failure)."""
    _atomic_write(destination, source_file.read_bytes())


def _selected_workspace(project: Path) -> Path:
    from pm.environments import selected_venv

    try:
        venv = selected_venv(project)
    except (OSError, RuntimeError, ValueError) as failure:
        raise InstallError("venv", f"no selected generation for {project}: {failure}",
                           "run `hermes pm install` first") from failure
    workspace = venv.parent / "workspace"
    if not (workspace / "pyproject.toml").is_file():
        raise InstallError("venv", f"selected generation has no workspace beside {venv}",
                           "run `hermes pm install` first")
    return workspace


def _journal_dir(project: Path) -> Path:
    from pm.environments import install_state_dir

    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    return install_state_dir(project) / "republish" / f"{stamp}-{uuid.uuid4().hex[:8]}"


def republish_source(
    project_root: Path,
    *,
    source: Path | None = None,
    workspace: Path | None = None,
    only: tuple[str, ...] = (),
    prune: bool = False,
    verify: bool = False,
    check: bool = False,
    allow_dirty: bool = False,
) -> Plan:
    """Publish core *source* bytes into *workspace* (default: the selected generation's).

    ``check`` reports the plan and writes nothing. ``verify`` also content-compares files whose
    (size, mtime) match -- the paranoia pass, ~12 min on this host -- on top of the default
    stat-first comparison. ``only`` restricts the publish to named paths (a delivery card names
    its file list). ``allow_dirty`` publishes paths that are uncommitted in the source checkout;
    without it a dirty touched path is a refusal, because traceability is the point.
    :class:`Plan` carries everything a caller needs to report: ``provenance_checked`` is False
    when the source is not a git work tree, so "no dirty paths" is never silently read as
    "these bytes were reviewed".
    """
    started = time.perf_counter()
    project = Path(project_root).resolve()
    source_root = Path(source).resolve() if source is not None else project
    if not (source_root / "pyproject.toml").is_file():
        raise InstallError("venv", f"{source_root} is not a core project root (no pyproject.toml)")
    target = Path(workspace).resolve() if workspace is not None else _selected_workspace(project)
    if target == source_root or target.is_relative_to(source_root) or source_root.is_relative_to(target):
        raise InstallError("venv", f"refusing to republish {source_root} into itself ({target})")

    plan = Plan(source=source_root, workspace=target)
    entries: list[tuple[Path, Path]] = []
    for path, relative in iter_core_inputs(source_root):
        if relative.parent == Path(".") and relative.name in GENERATION_OWNED:
            plan.skipped.append(relative.as_posix())
            continue
        if relative.parts and relative.parts[0] in _NEVER_PRUNED:
            plan.skipped.append(relative.as_posix())
            continue
        if not _matches(relative, only):
            continue
        entries.append((path, relative))
    if only:
        matched = {relative.as_posix() for _, relative in entries}
        missing = [entry for entry in only
                   if not any(m == entry or m.startswith(entry.rstrip("/") + "/") for m in matched)]
        if missing:
            raise InstallError("venv", f"--only matched no build input: {missing}",
                               "name a path the build snapshot copies (see `hermes pm republish-source --check`)")

    for path, relative in entries:
        destination = target / relative
        if destination.is_symlink():
            raise InstallError("venv", f"workspace entry is a symlink, refusing to write through it: {relative}")
        if not destination.is_file():
            plan.created.append(relative.as_posix())
            continue
        try:
            same_stat = (path.stat().st_size == destination.stat().st_size
                         and path.stat().st_mtime_ns == destination.stat().st_mtime_ns)
        except OSError:
            same_stat = False
        if same_stat and not verify:
            plan.unchanged += 1
            continue
        if _same_bytes(path, destination):
            # Same bytes, different mtime is not a change: republishing would only churn mtimes.
            plan.unchanged += 1
            continue
        plan.updated.append(relative.as_posix())

    planned = {relative.as_posix() for _, relative in entries}
    for directory in core_input_dirs(source_root):
        root = target / directory.name
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            ignored = set(_core_input_ignored(dirpath, dirnames + filenames))
            dirnames[:] = [name for name in dirnames if name not in ignored]
            for name in filenames:
                if name in ignored:
                    continue
                relative = (Path(dirpath) / name).relative_to(target).as_posix()
                if relative in planned:
                    continue
                if not _matches(Path(relative), only):
                    continue
                plan.stale.append(relative)

    touched = {relative.as_posix() for _, relative in entries
               if relative.as_posix() in set(plan.created) | set(plan.updated)}
    plan.dirty, plan.provenance_checked = _dirty_source(source_root, touched)

    if check:
        plan.duration_ms = int((time.perf_counter() - started) * 1000)
        return plan
    if plan.dirty and not allow_dirty:
        raise InstallError("venv", f"{len(plan.dirty)} touched path(s) are uncommitted in {source_root}: "
                           f"{plan.dirty[:5]}{'…' if len(plan.dirty) > 5 else ''}",
                           "commit and review them, or pass --allow-dirty to publish hand-edited bytes")
    if not (plan.created or plan.updated or (prune and plan.stale)):
        plan.duration_ms = int((time.perf_counter() - started) * 1000)
        return plan

    from hermes_cli.runtime_state import runtime_lock

    with runtime_lock(project, timeout=None) as held:
        if not held:  # timeout=None waits, so this is belt-and-braces for a future default
            raise InstallError("venv", f"dependency lock still held for {project}")
        if workspace is None and _selected_workspace(project) != target:
            raise InstallError("venv", "the selected generation changed while waiting for the lock; retry",
                               "run `hermes pm republish-source` again")
        _apply(project, plan, entries, prune=prune)
    plan.duration_ms = int((time.perf_counter() - started) * 1000)
    return plan


def _apply(project: Path, plan: Plan, entries: list[tuple[Path, Path]], *, prune: bool) -> None:
    """Write the plan under the held lock: journal, atomic per-file replace, verify, roll back."""
    target = plan.workspace
    journal = _journal_dir(project)
    backups = journal / "backup"
    published = set(plan.updated)
    created = set(plan.created)
    records: list[dict] = []
    todo = [(path, relative) for path, relative in entries if relative.as_posix() in published | created]
    try:
        for path, relative in todo:
            destination = target / relative
            record = {"path": relative.as_posix(), "created": relative.as_posix() in created,
                      "after_sha256": None, "before_sha256": None}
            if destination.is_file():
                saved = backups / relative
                saved.parent.mkdir(parents=True, exist_ok=True)
                saved.write_bytes(destination.read_bytes())
                record["before_sha256"] = _sha256(saved)
            records.append(record)
            _publish_file(path, destination)
            record["after_sha256"] = _sha256(destination)
        if prune:
            for relative in plan.stale:
                destination = target / relative
                if destination.is_file():
                    saved = backups / relative
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    saved.write_bytes(destination.read_bytes())
                    records.append({"path": relative, "created": False, "deleted": True,
                                    "before_sha256": _sha256(saved), "after_sha256": None})
                    destination.unlink()
                    plan.pruned.append(relative)
    except BaseException as failure:
        _rollback(target, journal, records)
        _write_journal(journal, plan, records, outcome="failed", error=str(failure))
        raise
    _write_journal(journal, plan, records, outcome="ok")
    plan.journal = str(journal)
    for path, relative in todo:
        if not _same_bytes(path, target / relative):
            _rollback(target, journal, records)
            raise InstallError("venv", f"published bytes do not match the source: {relative.as_posix()}",
                               f"the journal at {journal} has the restorable state")


def _write_journal(journal: Path, plan: Plan, records: list[dict], *, outcome: str,
                   error: str | None = None) -> None:
    journal.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "outcome": outcome,
        "error": error,
        "source": str(plan.source),
        "workspace": str(plan.workspace),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": records,
        "stale": plan.stale,
        "pruned": plan.pruned,
    }
    (journal / "journal.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _rollback(target: Path, journal: Path, records: list[dict]) -> None:
    """Restore every touched file from the journal, newest first, and say what could not be."""
    backups = journal / "backup"
    for record in reversed(records):
        destination = target / record["path"]
        saved = backups / record["path"]
        if record.get("deleted"):
            if saved.is_file():
                _atomic_write(destination, saved.read_bytes())
            continue
        if record["created"]:
            destination.unlink(missing_ok=True)
            continue
        if saved.is_file():
            _atomic_write(destination, saved.read_bytes())
