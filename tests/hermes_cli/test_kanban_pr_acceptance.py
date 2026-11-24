"""Two lifecycle invariants, using real SQLite and a local GitHub HTTP contract."""
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect

PR = "https://github.com/acme/repo/pull/7"


def _receipt(conn, task_id):
    """Newest acceptance receipt — the gate's verdict lives on the event log, not in the return."""
    return json.loads(conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance' ORDER BY id DESC",
        (task_id,)).fetchone()[0])


def _attempt(conn, github, **changes):
    github.update(changes)
    tid = kb.create_task(conn, title="post-merge", completion_contract="acme/repo")
    ok = kb.complete_task(conn, tid, result="done", metadata={"published_pr": PR})
    task = kb.get_task(conn, tid)
    assert task is not None
    return ok, tid, _receipt(conn, tid), task


def _state():
    """The GitHub contract under test: a merged PR whose post-merge pipeline completed."""
    return {"conclusion": "success", "head": "a" * 40, "requests": [],
            "merge": "c" * 40, "state": "MERGED", "merged": True,
            "merged_at": "2026-11-24T00:00:00Z", "tip": "d" * 40,
            "workflows": ["CI Full", "Deploy Staging Gate"],
            "ci_full": "success", "ci_status": "completed", "ci_jobs": ["success"],
            "staging": "success", "compare": "ahead"}


def _respond(state, path):
    """One GitHub, two transports: the gh shim below and the in-process acceptance fixture."""
    path = path.lstrip("/")
    sha, merge = state["head"], state["merge"]
    if path == "graphql":
        required = [{"context": "required", "app": {"databaseId": 1}}]
        # Extra required contexts, each with its own conclusion — a docs-only PR legitimately
        # reports several required contexts as `skipped` (ci-pr.yml's path filter).
        required += [{"context": extra["context"], "app": {"databaseId": 1}}
                     for extra in state.get("extra_required", [])]
        value = {"data": {"repository": {"pullRequest": {
            "headRefOid": sha, "baseRefName": "main", "state": state["state"],
            "merged": state["merged"], "mergedAt": state["merged_at"],
            "mergeCommit": {"oid": merge} if state.get("merge") else None,
            "baseRef": {"branchProtectionRule": {"requiredStatusChecks": required}}}}}}
    elif "/rules/branches/" in path:
        value = [[]]
    elif "/check-runs" in path:
        run = {"id": 42, "name": "required", "head_sha": sha,
               "app": {"id": 1}, "status": "in_progress" if state["conclusion"] == "pending" else "completed", "conclusion": state["conclusion"],
               "html_url": "https://github.com/acme/repo/actions/runs/42"}
        if state.get("stale"):
            run["head_sha"] = "b" * 40
        runs = [] if state.get("missing") else [run]
        runs += [{"id": 43 + i, "name": extra["context"], "head_sha": sha, "app": {"id": 1},
                  "status": "completed", "conclusion": extra["conclusion"],
                  "html_url": f"https://github.com/acme/repo/actions/runs/{43 + i}"}
                 for i, extra in enumerate(state.get("extra_required", []))]
        value = [{"total_count": 100 + len(runs), "check_runs": [
            {**run, "id": 1000 + i, "name": "optional", "conclusion": "skipped"}
            for i in range(100)]}, {"total_count": 100 + len(runs), "check_runs": runs}]
        if state.get("race"):
            state["race"]()
        if state.get("head_change"):
            state["head"] = "b" * 40
    elif "/statuses" in path:
        # Newest first, exactly like the API: the deploy job's terminal write.
        match = re.search(r"/commits/([0-9a-f]{40})/statuses", path)
        staging = (state.get("staging_by_sha") or {}).get(match[1], state["staging"]) if match else state["staging"]
        value = [[]] if staging is None else [[
            {"id": 900, "context": "deploy/staging", "state": staging,
             "description": state.get("staging_description", "app staging deploy success"),
             "target_url": "https://staging.example/app", "created_at": "2026-11-24T00:05:00Z"}]]
    elif "/actions/workflows" in path:
        value = [{"total_count": len(state["workflows"]),
                  "workflows": [{"name": name} for name in state["workflows"]]}]
    elif "/jobs" in path:
        value = [{"total_count": len(state["ci_jobs"]),
                  "jobs": [{"name": f"job{i}", "conclusion": job}
                           for i, job in enumerate(state["ci_jobs"])]}]
    elif "/actions/runs" in path:
        runs = [] if state.get("ci_run") is False else [{
            "id": 77, "name": "CI Full", "head_sha": merge, "status": state["ci_status"],
            "conclusion": state["ci_full"], "created_at": "2026-11-24T00:00:30Z",
            "html_url": "https://github.com/acme/repo/actions/runs/77"}]
        value = [{"total_count": len(runs), "workflow_runs": runs}]
    elif "/git/ref/heads/" in path:
        value = {"object": {"sha": state["tip"]}}
    elif "/compare/" in path:
        value = {"status": state["compare"]}
    elif "/pulls/" in path:
        value = {"head": {"sha": sha}, "base": {"ref": "main"},
                 "state": "closed" if state["merged"] else "open",
                 "merged": state["merged"]}
    else:
        raise KeyError(path)
    return value


@pytest.fixture
def github(tmp_path, monkeypatch):
    state = _state()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            try:
                value = _respond(state, self.path)
            except KeyError:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport sys,urllib.request\n"
                  f"u='http://127.0.0.1:{server.server_port}/'+sys.argv[2]\n"
                  "print(urllib.request.urlopen(u).read().decode())\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def acceptance(tmp_path, monkeypatch):
    """The same GitHub contract bound in-process: no gh spawn, so the merge/CI/staging rules
    are provable on ANY host (the linux-marked tests above cover the subprocess path)."""
    from hermes_cli import kanban_pr_acceptance as pa
    state = _state()

    def fake_api(endpoint, *, query=None, paginate=False, profile_home=None):
        return _respond(state, endpoint)

    monkeypatch.setattr(pa, "_api", fake_api)
    if hasattr(pa, "_policy"):  # absent on the pre-directive kernel: the pins below must be red there
        monkeypatch.setattr(pa, "_policy", lambda: {"ci_workflow": "CI Full",
                                                    "deploy_workflow_prefix": "Deploy Staging",
                                                    "deploy_status_context": "deploy/staging"})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    return state


@pytest.mark.platforms("linux")
def test_pr_completion_requires_current_required_evidence(github):
    with connect() as conn:
        # `skipped` is NOT in this list: a path-filtered REQUIRED context is satisfied (see the
        # skipped-context tests below). Everything else terminal-but-not-success still refuses.
        for conclusion in ("failure", "pending", "cancelled", "timed_out", "action_required", "neutral", None, "success"):
            github.update(conclusion=conclusion, head="a" * 40)
            tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
            ok = kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert ok is (conclusion == "success")
            task = kb.get_task(conn, tid)
            assert (task.status == "done") is ok
            receipts = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
            assert receipts and receipts[-1]["head_sha"] == "a" * 40
            if not ok:
                assert task.status in {"running", "ready", "blocked", "review"}
                assert "retry" in receipts[-1]["recovery"]
                assert receipts[-1]["checks"][0]["id"] == 42
        for fault in ("missing", "stale", "head_change"):
            github.update(conclusion="success", head="a" * 40)
            github[fault] = True
            tid = kb.create_task(conn, title=fault, completion_contract="acme/repo")
            assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).status != "done"
            github.pop(fault)
        # Omission and a sibling repository cannot downgrade the stored declaration.
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, summary="local green")
        assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/other/repo/pull/7"})
        before = len(github["requests"])
        local = kb.create_task(conn, title="local", completion_contract="local-only")
        assert kb.complete_task(conn, local, summary="https://github.com/acme/repo/pull/7 is background context")
        assert len(github["requests"]) == before


@pytest.mark.platforms("linux")
def test_acceptance_receipts_and_terminal_write_share_run_ownership(github):
    with connect() as conn:
        for conclusion in ("success", "failure"):
            tid = kb.create_task(conn, title="race", completion_contract="acme/repo")
            owner = kb.claim_task(conn, tid)
            run_id = owner.current_run_id
            def reclaim():
                with connect() as rival:
                    assert kb.block_task(rival, tid, reason="Reassigned during acceptance")
                    assert kb.unblock_task(rival, tid)
                    github["replacement"] = kb.claim_task(rival, tid).current_run_id
            github.update(conclusion=conclusion, race=reclaim)
            assert not kb.complete_task(conn, tid, result="done", expected_run_id=run_id,
                metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).current_run_id == github["replacement"]
            assert github["replacement"] != run_id
            assert kb.get_task(conn, tid).status != "done"
            assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0] == 0
            github.pop("race")


# --- #122689: acceptance must read the repo as the ASSIGNEE profile's gh login ---

@pytest.mark.platforms("posix")
def test_acceptance_runs_gh_as_the_assignee_profile(tmp_path, monkeypatch):
    """The gh child env carries the assignee's own GH credentials (its .env),
    never the ambient/launch residue, and an invisible repo is classified
    `auth` naming the repository — not a retryable `infra` failure."""
    from pathlib import Path

    launch_home = tmp_path / "home"
    launch_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    assignee_home = launch_home / "profiles" / "b"
    assignee_home.mkdir(parents=True)
    (assignee_home / ".env").write_text("GH_TOKEN=b-token\n", encoding="utf-8")
    # Ambient residue that must NOT decide the login.
    monkeypatch.setenv("GH_TOKEN", "launch-token")
    monkeypatch.setenv("GH_CONFIG_DIR", "/nonexistent/launch/gh")

    env_dump = tmp_path / "gh_env.json"
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport json, os\n"
                  f"json.dump(dict(os.environ), open({str(env_dump)!r}, 'w'))\n"
                  "print(json.dumps({'data': {'repository': None}}))\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-b", completion_contract="acme/repo", assignee="b")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        assert kb.get_task(conn, tid).status != "done"
        receipts = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
        assert receipts[-1]["classification"] == "auth"
        assert "acme/repo" in receipts[-1]["detail"]
    captured = json.loads(env_dump.read_text())
    assert captured["GH_TOKEN"] == "b-token"
    assert captured.get("GH_CONFIG_DIR") != "/nonexistent/launch/gh"
    assert "credentials" in (kb.get_task(conn, tid).last_failure_error or "")


@pytest.mark.platforms("posix")
def test_assignee_without_own_gh_login_never_falls_through_to_ambient_login(tmp_path, monkeypatch):
    """An assignee profile with no GH_TOKEN/GH_CONFIG_DIR of its own must not inherit the
    launch user's ~/.config/gh (HOME/XDG_CONFIG_HOME stay the launch process's): gh is pinned
    to a profile-owned config dir, its 'not logged in' exit is classified `auth` naming the profile."""
    launch_home = tmp_path / "home"
    launch_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    assignee_home = launch_home / "profiles" / "b"
    assignee_home.mkdir(parents=True)
    (assignee_home / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("GH_TOKEN", "launch-token")
    monkeypatch.setenv("GH_CONFIG_DIR", "/nonexistent/launch/gh")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "launch-xdg"))

    env_dump = tmp_path / "gh_env.json"
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    # Real gh: GH_CONFIG_DIR wins; a config dir without hosts.yml means "not logged in" (exit 4).
    gh.write_text(f"#!{sys.executable}\nimport json, os, pathlib, sys\n"
                  f"pathlib.Path({str(env_dump)!r}).write_text(json.dumps(dict(os.environ)), encoding='utf-8')\n"
                  "if 'GH_CONFIG_DIR' in os.environ and not os.path.exists(os.environ['GH_CONFIG_DIR']):\n"
                  "    sys.exit(4)\n"
                  "print(json.dumps({'data': {'repository': None}}))\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-b", completion_contract="acme/repo", assignee="b")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["classification"] == "auth"
    assert "'b'" in receipt["detail"] and "no login" in receipt["detail"]
    captured = json.loads(env_dump.read_text(encoding="utf-8-sig"))
    assert captured["GH_CONFIG_DIR"] == str(assignee_home / "gh")
    assert "GH_TOKEN" not in captured and "GITHUB_TOKEN" not in captured


def test_assigned_card_with_unresolvable_profile_is_auth_not_ambient(tmp_path, monkeypatch):
    """A card assigned to a profile that no longer exists must not run gh as the completing
    process's ambient login: classification `auth` naming the profile, gh never invoked."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))  # any gh spawn would fail as infra
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-ghost", completion_contract="acme/repo", assignee="ghost")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["classification"] == "auth"
    assert "'ghost'" in receipt["detail"] and "cannot be resolved" in receipt["detail"]


# --- CEO directive 2026-11-24: "done" means MERGED + CI Full + Deploy Staging, not "checks green" ---

def test_open_pr_with_green_checks_is_not_merged_and_cannot_complete(acceptance):
    """A green PR that is still OPEN used to pass and left its publication orphaned
    (t_0e6376ca, PR #19268). Merge is the deliverable, so OPEN must be refused."""
    with connect() as conn:
        ok, _, receipt, task = _attempt(conn, acceptance, state="OPEN", merged=False, merged_at=None)
        assert not ok and task.status != "done"
    assert receipt["classification"] == "not_merged"
    assert receipt["ok"] is False
    assert "has not been merged" in receipt["detail"]


@pytest.mark.parametrize("change, expected", [
    ({"staging": None}, "missing"),
    ({"staging": "pending"}, "pending"),
    ({"staging": "failure"}, "failure"),
    ({"ci_run": False}, "missing"),
    ({"ci_full": None, "ci_status": "in_progress"}, "pending"),
    ({"ci_full": "failure"}, "failure"),
    # conclusion=success with every job skipped is NOT evidence (measured 2026-11-24).
    ({"ci_jobs": ["skipped", "skipped"]}, "missing"),
])
def test_merged_pr_needs_ci_full_and_staging_on_the_merge_commit(acceptance, change, expected):
    with connect() as conn:
        ok, _, receipt, task = _attempt(conn, acceptance, **change)
        assert not ok and task.status != "done"
    assert receipt["classification"] == expected
    assert "merge commit" in receipt["detail"]


def test_merged_pr_with_ci_full_and_staging_success_completes(acceptance):
    with connect() as conn:
        ok, _, receipt, task = _attempt(conn, acceptance)
        assert ok and task.status == "done"
    assert receipt["classification"] == "success" and receipt["ok"] is True
    assert receipt["merged_at"] == "2026-11-24T00:00:00Z"
    assert receipt["merge_commit_sha"] == "c" * 40
    assert receipt["post_merge"]["ci_full"]["state"] == "success"
    assert receipt["post_merge"]["ci_full"]["jobs"]["success"] == 1
    assert receipt["post_merge"]["staging"]["state"] == "success"


def test_staging_success_on_a_containing_base_tip_counts(acceptance):
    """The staging concurrency group is latest-wins: a cancelled deploy can leave the merge
    commit at `pending` while a descendant tip carries the terminal result for the same tree."""
    with connect() as conn:
        ok, _, receipt, _ = _attempt(conn, acceptance, staging_by_sha={"c" * 40: None})
        assert ok
    staging = receipt["post_merge"]["staging"]
    assert staging["state"] == "success" and staging["sha"] == "d" * 40
    assert staging["contains_merge_commit"] is True


def test_staging_on_a_diverged_tip_is_not_evidence(acceptance):
    with connect() as conn:
        ok, _, receipt, _ = _attempt(conn, acceptance, staging_by_sha={"c" * 40: None}, compare="diverged")
        assert not ok
    assert receipt["classification"] == "missing"


def test_a_repository_without_the_pipelines_records_not_applicable(acceptance):
    """A repo that runs neither CI Full nor a staging deploy is not frozen by this rule —
    the gap is recorded in the receipt instead of being reported as green."""
    with connect() as conn:
        ok, _, receipt, _ = _attempt(conn, acceptance, workflows=["CI"])
        assert ok
    assert receipt["post_merge"]["ci_full"]["state"] == "not_applicable"
    assert receipt["post_merge"]["staging"]["state"] == "not_applicable"


@pytest.mark.parametrize("contract", ["local-only", None])
def test_local_only_card_that_hands_over_a_pr_is_refused(acceptance, contract):
    """`local-only` + published_pr was the silent skip: nothing was verified and the PR was
    abandoned. The refusal must happen before any GitHub read."""
    with connect() as conn:
        tid = kb.create_task(conn, title="local", completion_contract=contract)
        assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": PR})
        assert kb.get_task(conn, tid).status != "done"
        receipt = _receipt(conn, tid)
    assert acceptance["requests"] == []          # the contract is the defect; no network read
    assert receipt["classification"] == "contract_missing"
    assert PR in receipt["detail"]


# --- a path-filtered REQUIRED context reports `skipped`, and GitHub counts it satisfied ---
# t_e73a6f56 (PR #18774), t_e1aadc97 (PR #19247), t_ec077972 (PR #19290): every docs-only PR on
# `dev` was uncompletable because `_classify` fell through to `infra` for `skipped`, so the card
# was refused forever even though GitHub itself had allowed the merge. `ci-pr.yml` computes
# run_typecheck=false for a docs-only change, so the required contexts legitimately skip.


def test_path_filtered_required_contexts_are_satisfied(acceptance):
    """Three required contexts skipped by the changed-groups filter + a merged PR is a green
    card: GitHub evaluated the required set as satisfied (that is why the PR could merge)."""
    with connect() as conn:
        ok, _, receipt, task = _attempt(conn, acceptance, conclusion="skipped", extra_required=[
            {"context": "typecheck", "conclusion": "skipped"},
            {"context": "unit-test", "conclusion": "skipped"}])
        assert ok and task.status == "done"
    assert receipt["classification"] == "success" and receipt["ok"] is True
    # The raw conclusion stays on the record: the receipt says WHY it was satisfied.
    assert {c["name"]: c["conclusion"] for c in receipt["checks"]} == {
        "required": "skipped", "typecheck": "skipped", "unit-test": "skipped"}


def test_a_skipped_required_context_never_masks_a_failed_sibling(acceptance):
    """The skip is only forgiven as ITS OWN conclusion. The verdict is the first non-success, so
    one failing required context still refuses the completion (the fix opens nothing)."""
    with connect() as conn:
        ok, _, receipt, task = _attempt(conn, acceptance, conclusion="skipped", extra_required=[
            {"context": "unit-test", "conclusion": "failure"}])
        assert not ok and task.status != "done"
    assert receipt["classification"] == "failure"


def test_a_skipped_required_context_does_not_excuse_an_unmerged_pr(acceptance):
    """Green-by-skip still requires the merge: an OPEN PR is refused before any check is read."""
    with connect() as conn:
        ok, _, receipt, task = _attempt(conn, acceptance, conclusion="skipped",
                                        state="OPEN", merged=False, merged_at=None)
        assert not ok and task.status != "done"
    assert receipt["classification"] == "not_merged"
