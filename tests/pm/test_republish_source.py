"""pm.republish: re-snapshotting core source into a selected generation.

The gap this closes (kanban t_fb68fad3): ``Venv.expected_stamp`` hashes the dependency
inputs only -- uv.lock, the packaging files, the extras, the python target, the plugin
members -- and no core source bytes. A code-only core change therefore leaves the stamp
identical, ``_commit_selection`` keeps ``current=True`` and never calls ``Venv.apply()``,
which is the only caller of ``_copy_core_inputs``: the generation keeps the source it was
built from, and no PM verb could carry a reviewed core fix into the tree the managed venv
imports. ``republish_source`` is that verb; these tests pin its contract.

The selection is shared with the build (``pm.workspace.iter_core_inputs``), so a publish can
never carry a different file set than a rebuild -- ``test_selection_is_the_build_snapshot``
is the parity proof.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from pm import republish
from pm.package import InstallError
from pm.workspace import _copy_core_inputs, iter_core_inputs

PYPROJECT = """\
[project]
name = "corepkg"
version = "0.0.0"
readme = "README.md"
license-files = ["LICENSE"]
"""


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_source(root: Path) -> Path:
    """A miniature core checkout: two packages, a data dir, a root module, excluded noise."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        PYPROJECT
        + '[tool.setuptools.packages.find]\ninclude = ["alpha", "alpha.*", "beta", "beta.*"]\n',
        encoding="utf-8",
    )
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    (root / "LICENSE").write_text("license\n", encoding="utf-8")
    (root / "cli.py").write_text("print('core')\n", encoding="utf-8")
    (root / "alpha").mkdir()
    (root / "alpha" / "__init__.py").write_text("# alpha\n", encoding="utf-8")
    (root / "alpha" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "beta").mkdir()
    (root / "beta" / "__init__.py").write_text("# beta\n", encoding="utf-8")
    (root / "locales").mkdir()
    (root / "locales" / "en.json").write_text('{"a": "b"}\n', encoding="utf-8")
    # Excluded from the snapshot: build output, caches, egg-info, dot dirs, __pycache__.
    (root / "build").mkdir()
    (root / "build" / "junk.txt").write_text("junk\n", encoding="utf-8")
    (root / "alpha" / "__pycache__").mkdir()
    (root / "alpha" / "__pycache__" / "mod.cpython-314.pyc").write_bytes(b"\0pyc")
    (root / "corepkg.egg-info").mkdir()
    (root / "corepkg.egg-info" / "PKG-INFO").write_text("meta\n", encoding="utf-8")
    return root


def make_generation(source: Path, workspace: Path) -> Path:
    """A built generation workspace: the core snapshot plus what the generation itself owns."""
    _copy_core_inputs(source, workspace)
    # The build overwrites the root manifest with the generated one (member graph, no cutoff).
    (workspace / "pyproject.toml").write_text(
        PYPROJECT + '[tool.uv.workspace]\nmembers = []\n', encoding="utf-8"
    )
    (workspace / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (workspace / "core-uv.lock").write_text("version = 1\n", encoding="utf-8")
    # A plugin member snapshot and runtime state live in the workspace but are not build inputs.
    (workspace / "plugin-sources" / "plug").mkdir(parents=True)
    (workspace / "plugin-sources" / "plug" / "pyproject.toml").write_text("member\n", encoding="utf-8")
    (workspace / "home").mkdir()
    (workspace / "home" / "state.json").write_text("{}\n", encoding="utf-8")
    return workspace


@pytest.fixture
def layout(tmp_path):
    source = make_source(tmp_path / "checkout")
    workspace = make_generation(source, tmp_path / "generation" / "workspace")
    return source, workspace, tmp_path / "install" / "core"


def test_publishes_a_changed_module_and_restores_the_source_bytes(layout):
    source, workspace, project = layout
    live = workspace / "alpha" / "mod.py"
    live.write_text("VALUE = 0  # stale generation copy\n", encoding="utf-8")

    plan = republish.republish_source(project, source=source, workspace=workspace)

    assert plan.updated == ["alpha/mod.py"]
    assert plan.created == []
    assert live.read_bytes() == (source / "alpha" / "mod.py").read_bytes()
    assert sha(live) == sha(source / "alpha" / "mod.py")
    assert plan.journal and Path(plan.journal, "journal.json").is_file()


def test_check_reports_without_writing(layout):
    source, workspace, project = layout
    live = workspace / "alpha" / "mod.py"
    live.write_text("VALUE = 0\n", encoding="utf-8")
    before = live.read_bytes()

    plan = republish.republish_source(project, source=source, workspace=workspace, check=True)

    assert plan.pending is True
    assert plan.updated == ["alpha/mod.py"]
    assert live.read_bytes() == before
    assert plan.journal is None


def test_generation_owned_files_and_runtime_state_are_never_written(layout):
    source, workspace, project = layout
    owned = {name: (workspace / name).read_bytes()
             for name in ("pyproject.toml", "uv.lock", "core-uv.lock")}
    member = (workspace / "plugin-sources" / "plug" / "pyproject.toml").read_bytes()
    state = (workspace / "home" / "state.json").read_bytes()
    # Source differs in every one of these too -- the publish must still leave them alone.
    (source / "pyproject.toml").write_text(PYPROJECT + "\n# a source edit\n", encoding="utf-8")

    plan = republish.republish_source(project, source=source, workspace=workspace)

    for name, data in owned.items():
        assert (workspace / name).read_bytes() == data, name
    assert "pyproject.toml" in plan.skipped
    assert (workspace / "plugin-sources" / "plug" / "pyproject.toml").read_bytes() == member
    assert (workspace / "home" / "state.json").read_bytes() == state


def test_a_second_run_is_a_no_op_and_touches_no_mtime(layout):
    source, workspace, project = layout
    (workspace / "alpha" / "mod.py").write_text("VALUE = 0\n", encoding="utf-8")
    republish.republish_source(project, source=source, workspace=workspace)
    stamps = {p: p.stat().st_mtime_ns for p in workspace.rglob("*.py")}

    plan = republish.republish_source(project, source=source, workspace=workspace)

    assert plan.updated == [] and plan.created == [] and plan.pruned == []
    assert {p: p.stat().st_mtime_ns for p in workspace.rglob("*.py")} == stamps


def test_new_source_module_is_created(layout):
    source, workspace, project = layout
    (source / "alpha" / "extra.py").write_text("NEW = True\n", encoding="utf-8")

    plan = republish.republish_source(project, source=source, workspace=workspace)

    assert plan.created == ["alpha/extra.py"]
    assert (workspace / "alpha" / "extra.py").read_bytes() == (source / "alpha" / "extra.py").read_bytes()


def test_stale_files_are_reported_then_pruned_on_request(layout):
    source, workspace, project = layout
    (workspace / "alpha" / "gone.py").write_text("OLD = True\n", encoding="utf-8")

    plan = republish.republish_source(project, source=source, workspace=workspace)
    assert plan.stale == ["alpha/gone.py"]
    assert (workspace / "alpha" / "gone.py").is_file(), "a source deletion is not an automatic delete"

    pruned = republish.republish_source(project, source=source, workspace=workspace, prune=True)
    assert pruned.pruned == ["alpha/gone.py"]
    assert not (workspace / "alpha" / "gone.py").exists()
    # Never pruned: the generation's own state.
    assert (workspace / "plugin-sources" / "plug" / "pyproject.toml").is_file()
    assert (workspace / "home" / "state.json").is_file()


def test_only_restricts_the_publish_set(layout):
    source, workspace, project = layout
    (workspace / "alpha" / "mod.py").write_text("stale a\n", encoding="utf-8")
    (workspace / "beta" / "__init__.py").write_text("stale b\n", encoding="utf-8")

    plan = republish.republish_source(project, source=source, workspace=workspace,
                                      only=("alpha/mod.py",))

    assert plan.updated == ["alpha/mod.py"]
    assert (workspace / "beta" / "__init__.py").read_text(encoding="utf-8") == "stale b\n"


def test_an_only_path_the_build_never_copies_is_an_error(layout):
    source, workspace, project = layout
    with pytest.raises(InstallError) as failure:
        republish.republish_source(project, source=source, workspace=workspace, only=("nope.py",))
    assert "--only matched no build input" in str(failure.value)


def test_a_failed_publish_rolls_every_written_file_back(layout, monkeypatch):
    source, workspace, project = layout
    first = workspace / "alpha" / "mod.py"
    second = workspace / "beta" / "__init__.py"
    first.write_text("stale a\n", encoding="utf-8")
    second.write_text("stale b\n", encoding="utf-8")
    originals = {first: first.read_bytes(), second: second.read_bytes()}
    real = republish._publish_file
    calls = {"n": 0}

    def flaky(source_file, destination):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full (injected)")
        return real(source_file, destination)

    monkeypatch.setattr(republish, "_publish_file", flaky)

    with pytest.raises(OSError):
        republish.republish_source(project, source=source, workspace=workspace)

    for path, original in originals.items():
        assert path.read_bytes() == original, path


def test_the_journal_is_written_under_the_install_state_and_rolls_back(tmp_path, monkeypatch):
    source = make_source(tmp_path / "checkout")
    workspace = make_generation(source, tmp_path / ".hermes" / "installs" / "k" / "environments" / "e" / "workspace")
    project = tmp_path / "checkout"
    first = workspace / "alpha" / "mod.py"
    first.write_text("stale a\n", encoding="utf-8")
    second = workspace / "beta" / "__init__.py"
    second.write_text("stale b\n", encoding="utf-8")
    real = republish._publish_file
    calls = {"n": 0}

    def flaky(source_file, destination):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full (injected)")
        return real(source_file, destination)

    monkeypatch.setattr(republish, "_publish_file", flaky)
    monkeypatch.setattr("pm.environments.install_state_dir",
                        lambda root: tmp_path / ".hermes" / "installs" / "k")

    with pytest.raises(OSError):
        republish.republish_source(project, source=source, workspace=workspace)

    journals = list((tmp_path / ".hermes" / "installs" / "k" / "republish").rglob("journal.json"))
    assert len(journals) == 1
    payload = json.loads(journals[0].read_text(encoding="utf-8"))
    assert payload["outcome"] == "failed"
    assert "injected" in payload["error"]
    assert first.read_text(encoding="utf-8") == "stale a\n"


def test_uncommitted_source_bytes_are_refused_unless_allowed(layout):
    source, workspace, project = layout
    (workspace / "alpha" / "mod.py").write_text("stale\n", encoding="utf-8")
    git = ["git", "-C", str(source), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    (source / "alpha" / "mod.py").write_text("VALUE = 2  # hand edited, never reviewed\n", encoding="utf-8")

    with pytest.raises(InstallError) as failure:
        republish.republish_source(project, source=source, workspace=workspace)
    assert "uncommitted" in str(failure.value)
    assert (workspace / "alpha" / "mod.py").read_text(encoding="utf-8") == "stale\n"

    plan = republish.republish_source(project, source=source, workspace=workspace, allow_dirty=True)
    assert plan.dirty == ["alpha/mod.py"]
    assert (workspace / "alpha" / "mod.py").read_text(encoding="utf-8").startswith("VALUE = 2")


def test_selection_is_the_build_snapshot(tmp_path):
    """The publish set and the build set are the same by construction -- prove it."""
    source = make_source(tmp_path / "checkout")
    built = tmp_path / "built"
    _copy_core_inputs(source, built)
    walked = {relative.as_posix(): path for path, relative in iter_core_inputs(source)}

    copied = {}
    for path in built.rglob("*"):
        if path.is_file():
            copied[path.relative_to(built).as_posix()] = path
    assert set(copied) == set(walked)
    for relative, path in walked.items():
        assert sha(copied[relative]) == sha(path), relative


def test_a_workspace_inside_the_source_is_refused(layout):
    source, _, project = layout
    with pytest.raises(InstallError) as failure:
        republish.republish_source(project, source=source, workspace=source / "alpha")
    assert "into itself" in str(failure.value)
