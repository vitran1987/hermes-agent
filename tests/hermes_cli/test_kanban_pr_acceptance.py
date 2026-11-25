"""Two lifecycle invariants, using real SQLite and a local GitHub HTTP contract."""
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_pr_acceptance_store as store
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


def _respond(state, path, query=None):
    """One GitHub, two transports: the gh shim below and the in-process acceptance fixture."""
    path = path.lstrip("/")
    sha, merge = state["head"], state["merge"]
    if path == "graphql":
        required = [{"context": "required", "app": {"databaseId": 1}}]
        # Extra required contexts, each with its own conclusion — a docs-only PR legitimately
        # reports several required contexts as `skipped` (ci-pr.yml's path filter).
        required += [{"context": extra["context"], "app": {"databaseId": 1}}
                     for extra in state.get("extra_required", [])]
        # A fork repository: no branch protection rule and no rulesets, so `required == []`.
        # `no_required` is True for every repo, or a tuple of "owner/name" slugs for just those.
        unprotected = state.get("no_required")
        names = unprotected if isinstance(unprotected, tuple) else ()
        if unprotected is True or any(f'"{repo_slug}"' in (query or "") for repo_slug in names):
            required = []
        value = {"data": {"repository": {"pullRequest": {
            "headRefOid": sha, "baseRefName": "main", "state": state["state"],
            "merged": state["merged"], "mergedAt": state["merged_at"],
            "mergeCommit": {"oid": merge} if state.get("merge") else None,
            "baseRef": {"branchProtectionRule": {"requiredStatusChecks": required}}}}}}
    elif "/rules/branches/" in path:
        value = [[]]                                   # no rulesets: no required set from here
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
        value = {"head": {"sha": sha, "ref": state.get("pr_head_ref", "")},
                 "title": state.get("pr_title", ""),
                 "base": {"ref": "main"},
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
        return _respond(state, endpoint, query)

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


# --- An UNDECLARED publication must not be a way around the gate (t_3813815d) ---

def test_local_only_card_whose_handoff_reports_a_pr_is_refused(acceptance):
    """t_3813815d completed `done` with PR #19284 still OPEN: the worker reported the PR in its
    completion metadata under `pr`, a key the gate never read, and nothing else. A publication
    report in the handoff is exactly as binding as `metadata.published_pr`."""
    with connect() as conn:
        tid = kb.create_task(conn, title="escaped", completion_contract="local-only")
        assert not kb.complete_task(conn, tid, result="done", metadata={
            "pr": {"number": 19284, "base": "dev", "head": "b844c1fb2d4",
                   "files": 1, "diff": "+123/-65", "mergeable": "MERGEABLE"}})
        task, receipt = kb.get_task(conn, tid), _receipt(conn, tid)
    assert task.status != "done"
    assert receipt["classification"] == "contract_missing"
    assert "#19284" in receipt["detail"]
    assert acceptance["requests"] == []   # the handoff already names the PR; no read needed


def test_local_only_card_citing_a_pr_in_a_comment_is_refused(acceptance):
    """Source 1 of the card: a PR URL in the card's own comment thread. Attributed by the task
    id in the PR title, so a lane card citing somebody else's PR is not caught by this arm."""
    with connect() as conn:
        tid = kb.create_task(conn, title="commented", completion_contract="local-only")
        kb.add_comment(conn, tid, "developer", f"Opened {PR} — ready for review.")
        acceptance["pr_title"] = f"fix(ci): the thing [{tid}]"
        assert not kb.complete_task(conn, tid, result="done")
        task, receipt = kb.get_task(conn, tid), _receipt(conn, tid)
    assert task.status != "done"
    assert receipt["classification"] == "contract_missing"
    assert PR in receipt["detail"]
    assert receipt["ok"] is False


def test_local_only_card_citing_another_cards_pr_is_not_refused(acceptance, monkeypatch):
    """Review and merge lane cards routinely cite somebody else's PR; blocking those would make
    every lane card unclosable. Attribution is the task id in the title/head ref — here it is a
    different card's, so the card closes and no publication is inferred."""
    from hermes_cli import kanban_pr_acceptance as pa

    calls = []
    real_api = pa._api

    def counting_api(endpoint, **kwargs):
        calls.append(endpoint)
        return real_api(endpoint, **kwargs)

    monkeypatch.setattr(pa, "_api", counting_api)
    with connect() as conn:
        tid = kb.create_task(conn, title="cites-a-sibling", completion_contract="local-only")
        kb.add_comment(conn, tid, "code-reviewer", f"Reviewed {PR} (belongs to t_deadbeef).")
        acceptance["pr_title"] = "fix(hermes): a different card's work [t_deadbeef]"
        assert kb.complete_task(conn, tid, result="done", summary="reviewed, no publication of mine")
        task = kb.get_task(conn, tid)
        assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'",
                            (tid,)).fetchone()[0] == 0
    assert task is not None and task.status == "done"
    assert len(calls) == 1      # the citation WAS attributed, then dropped — no refusal


def test_a_genuinely_local_card_still_completes_without_any_github_read(acceptance, monkeypatch):
    """The other side of the rule: a local-only card with no PR anywhere must not be blocked, and
    must not spend a GitHub call proving it."""
    from hermes_cli import kanban_pr_acceptance as pa

    calls = []
    monkeypatch.setattr(pa, "_api", lambda *a, **k: calls.append(a) or {})
    with connect() as conn:
        tid = kb.create_task(conn, title="local work", completion_contract="local-only")
        assert kb.complete_task(conn, tid, result="done", summary="rewrote a local config")
        task = kb.get_task(conn, tid)
        assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'",
                            (tid,)).fetchone()[0] == 0
    assert task is not None and task.status == "done"
    assert calls == []


@pytest.mark.parametrize("fault", ["auth", "infra"])
def test_unreadable_publication_evidence_refuses_instead_of_passing(acceptance, monkeypatch, fault):
    """Fail closed: an unreadable source must never be read as "nothing was published", or a
    completed card could pass simply by making its own evidence unreadable."""
    from hermes_cli import kanban_pr_acceptance as pa

    with connect() as conn:
        tid = kb.create_task(conn, title="unreadable", completion_contract="local-only")
        kb.add_comment(conn, tid, "developer", f"Opened {PR} — ready for review.")
        acceptance["pr_title"] = f"fix: the thing [{tid}]"

        def refused(endpoint, *, query=None, paginate=False, profile_home=None):
            if fault == "auth":
                raise pa._GateAuthError(f"HTTP 403 on {endpoint.split('?')[0]}")
            raise OSError("gh api timed out")

        monkeypatch.setattr(pa, "_api", refused)
        assert not kb.complete_task(conn, tid, result="done")
        task, receipt = kb.get_task(conn, tid), _receipt(conn, tid)
    assert task.status != "done"
    assert receipt["classification"] == "unverifiable"
    assert "could not be read" in receipt["detail"]


# --- A publication report is never discarded because its URL failed attribution (P1) ---
#
# Attribution credits only a task id in the PR title/head ref or the card's own branch; over the
# fork's PRs, 0 of 40 head refs carry a task id and only 20 of 40 titles do, so roughly half of all
# real publication reports are unattributable. Dropping the report there closed the card and skipped
# arms (b) and (c) — so *adding* `url` to an otherwise refused report flipped refuse -> close.

_UNATTRIBUTED_REPORTS = [
    pytest.param({"published_pr": PR}, id="published_pr"),
    pytest.param({"pr": {"number": 7, "head": "b844c1fb2d4", "files": 1}}, id="pr-dict"),
    pytest.param({"pr_url": PR}, id="pr_url-bare-string"),
    pytest.param({"pr": {"number": 7, "head": "b844c1fb2d4", "files": 1, "url": PR}},
                 id="pr-dict-with-url"),
    pytest.param({"pull_request": {"number": 7, "html_url": PR, "head": "b844c1fb2d4"}},
                 id="pull_request"),
]


@pytest.mark.parametrize("metadata", _UNATTRIBUTED_REPORTS)
def test_every_handoff_spelling_of_a_publication_report_is_refused(acceptance, metadata):
    """Every natural spelling of "I published this PR" refuses the completion. The PR here carries
    neither a task id in its title nor a matching head ref, so attribution cannot confirm it — which
    must not be read as "nothing was published"."""
    with connect() as conn:
        tid = kb.create_task(conn, title="undeclared", completion_contract="local-only")
        assert not kb.complete_task(conn, tid, result="done", metadata=metadata)
        task, receipt = kb.get_task(conn, tid), _receipt(conn, tid)
    assert task.status != "done"
    assert receipt["classification"] == "contract_missing"
    assert receipt["ok"] is False
    assert PR in receipt["detail"] or "#7" in receipt["detail"]


def test_an_unattributed_handoff_report_is_caught_by_the_comment_arm(acceptance):
    """A real URL that cannot be attributed, plus the card's own thread citing the same PR, must
    fall through to arm (b) instead of closing the card."""
    with connect() as conn:
        tid = kb.create_task(conn, title="commented", completion_contract="local-only")
        kb.add_comment(conn, tid, "developer", f"Opened {PR} — ready for review.")
        assert not kb.complete_task(conn, tid, result="done", metadata={"pr_url": PR})
        task, receipt = kb.get_task(conn, tid), _receipt(conn, tid)
    assert task.status != "done"
    assert receipt["classification"] == "contract_missing"
    assert "a card comment cites" in receipt["detail"]
    assert PR in receipt["detail"]


# --- Arm (c) must reach a repository through an HTTPS remote (P2) ---
#
# Hermes workspaces are cloned over HTTPS (`gh repo clone`, `actions/checkout`), and the old parse
# split on ":" so `https://` became `https///` and "https" counted as a path segment -> every HTTPS
# remote parsed to None, i.e. the last-resort arm could never fire on this host.

def _workspace_with_remote(tmp_path, remote):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin", remote], check=True)
    return workspace


@pytest.mark.parametrize("remote, expected", [
    ("https://github.com/owner/repo.git", "owner/repo"),
    ("https://github.com/owner/repo", "owner/repo"),
    ("git@github.com:o/r.git", "o/r"),
    ("ssh://git@github.com/o/r.git", "o/r"),
])
def test_workspace_remote_reads_every_remote_spelling(tmp_path, acceptance, remote, expected):
    workspace = _workspace_with_remote(tmp_path, remote)
    with connect() as conn:
        tid = kb.create_task(conn, title="ws", workspace_kind="scratch",
                             workspace_path=str(workspace))
        assert store._workspace_remote(conn, tid) == expected


def test_a_local_only_card_with_an_open_pr_on_its_branch_is_refused_through_an_https_remote(
        tmp_path, acceptance, monkeypatch):
    """Non-vacuity for arm (c): an HTTPS workspace remote plus a card branch and an OPEN PR on it
    must produce a refusal. On the old parse the remote returned None and this card closed."""
    from hermes_cli import kanban_pr_acceptance as pa

    workspace = _workspace_with_remote(tmp_path, "https://github.com/acme/repo.git")

    def fake_api(endpoint, *, query=None, paginate=False, profile_home=None):
        # The repository DOES require status checks, which is what makes this refusal binding:
        # an ungated repository would be transport (see the transport-rule tests below).
        if endpoint == "graphql":
            return {"data": {"repository": {"pullRequest": {
                "baseRefName": "main", "baseRef": {"branchProtectionRule": {
                    "requiredStatusChecks": [{"context": "required", "app": {"databaseId": 1}}]}}}}}}
        if "/rules/branches/" in endpoint:
            return [[]]
        assert endpoint.startswith("repos/acme/repo/pulls?state=open&head=acme:"), endpoint
        return [{"html_url": PR}]

    monkeypatch.setattr(pa, "_api", fake_api)
    with connect() as conn:
        tid = kb.create_task(conn, title="branch-pr", completion_contract="local-only",
                             workspace_kind="worktree", workspace_path=str(workspace),
                             branch_name="card-branch")
        assert not kb.complete_task(conn, tid, result="done")
        task, receipt = kb.get_task(conn, tid), _receipt(conn, tid)
    assert task.status != "done"
    assert receipt["classification"] == "contract_missing"
    assert "card-branch" in receipt["detail"]


def test_a_workspace_remote_that_is_a_local_path_publishes_nothing(tmp_path, acceptance, monkeypatch):
    """The other side of the parse: a workspace whose origin is a local path names no repository, so
    nothing is read and the card still closes."""
    from hermes_cli import kanban_pr_acceptance as pa

    calls = []
    monkeypatch.setattr(pa, "_api", lambda *a, **k: calls.append(a) or {})
    workspace = _workspace_with_remote(tmp_path, str(tmp_path / "upstream"))
    with connect() as conn:
        tid = kb.create_task(conn, title="local clone", completion_contract="local-only",
                             workspace_kind="worktree", workspace_path=str(workspace),
                             branch_name="local-branch")
        assert kb.complete_task(conn, tid, result="done", summary="local branch only")
        task = kb.get_task(conn, tid)
    assert task is not None and task.status == "done"
    assert calls == []

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


# --- Refuse only while evidence still EXISTS to be fetched (PM rule 2026-11-25, t_81d713e5) ---
#
# A `local-only` card that cites a PR in a repository with NO required status checks can never
# produce the merge/CI/staging verdict the gate verifies: refusing it is not fail-closed, it is
# always-closed, and it stranded the whole fork-PR population (t_72542ef2 fork PR #44, t_1b59107a
# fork PR #46: protection 404 - rulesets 0 - runners 0 - required []). The rule is narrow: a
# citation in a GATED repository still refuses, and an unreadable repository still refuses, so the
# only completions it opens are ones where no evidence could ever have existed.


def _cite(conn, tid, url):
    kb.add_comment(conn, tid, "developer", f"Opened {url} — ready for review.")


def test_a_local_only_card_citing_a_fork_pr_completes_and_records_the_url(acceptance):
    """Acceptance #1 of the PM's fork rule: fork PR (required == []) cited in a comment => the card
    completes, and the URL is preserved as the trace instead of vanishing."""
    with connect() as conn:
        tid = kb.create_task(conn, title="fork transport", completion_contract="local-only")
        _cite(conn, tid, PR)
        acceptance["pr_title"] = f"fix(hermes): ported fix [{tid}]"
        acceptance["no_required"] = True
        assert kb.complete_task(conn, tid, result="done", metadata={"note": "local work"})
        task, receipt = kb.get_task(conn, tid), _receipt(conn, tid)
        run_metadata = json.loads(conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id=? ORDER BY id DESC", (tid,)).fetchone()[0])
    assert task is not None and task.status == "done"
    assert receipt["ok"] is True and receipt["classification"] == "transport_only"
    assert receipt["pr_url"] == PR and PR in receipt["detail"]
    # The trace: the URL is on the receipt AND back-filled into the handoff metadata.
    assert run_metadata.get("published_pr_transport") == PR


def test_a_local_only_card_citing_a_pr_in_a_gated_repository_still_refuses(acceptance):
    """Acceptance #2: the SAME shape, but the repository requires status checks => still refused.
    This is the non-vacuity proof that the fork rule did not become a blanket waiver."""
    with connect() as conn:
        tid = kb.create_task(conn, title="gated citation", completion_contract="local-only")
        _cite(conn, tid, PR)
        acceptance["pr_title"] = f"fix(bangioi): the thing [{tid}]"   # required == [required]
        assert not kb.complete_task(conn, tid, result="done")
        task, receipt = kb.get_task(conn, tid), _receipt(conn, tid)
    assert task is not None and task.status != "done"
    assert receipt["ok"] is False and receipt["classification"] == "contract_missing"
    assert PR in receipt["detail"]


def test_a_genuinely_local_card_with_no_pr_anywhere_still_completes(acceptance, monkeypatch):
    """Acceptance #3: no citation, no handoff report, no branch PR => unchanged, and no GitHub read
    is spent deciding it (the transport rule must not make local cards pay for a network probe)."""
    from hermes_cli import kanban_pr_acceptance as pa

    calls = []
    monkeypatch.setattr(pa, "_api", lambda *a, **k: calls.append(a) or {})
    with connect() as conn:
        tid = kb.create_task(conn, title="truly local", completion_contract="local-only")
        assert kb.complete_task(conn, tid, result="done", summary="rewrote a local config")
        task = kb.get_task(conn, tid)
        receipts = conn.execute(
            "SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'",
            (tid,)).fetchone()[0]
    assert task is not None and task.status == "done"
    assert calls == [] and receipts == 0


def test_a_card_citing_one_gated_pr_and_one_fork_pr_still_refuses(acceptance, monkeypatch):
    """The rule is judged per candidate: seeing a fork PR must not launder a gated one in the same
    thread. Every publication a card shows is evaluated before the card may close."""
    other = "https://github.com/acme/gated/pull/9"

    def probe(url, assignee):
        # Only the SECOND citation is gated: the first is transport, so a card that stopped at the
        # first candidate would close — the whole point of judging every candidate.
        return url == other

    with connect() as conn:
        tid = kb.create_task(conn, title="mixed", completion_contract="local-only")
        _cite(conn, tid, PR)
        kb.add_comment(conn, tid, "developer", f"also {other}")
        acceptance["pr_title"] = f"fix: the thing [{tid}]"
        acceptance["no_required"] = True
        monkeypatch.setattr(store, "_evidence_exists", probe)
        assert not kb.complete_task(conn, tid, result="done")
        task, receipt = kb.get_task(conn, tid), _receipt(conn, tid)
    assert task is not None and task.status != "done"
    assert receipt["classification"] == "contract_missing"
    assert other in receipt["detail"]


def test_an_unreadable_repository_never_counts_as_unprotected(acceptance, monkeypatch):
    """Fail closed at the transport decision itself: if the required-context probe cannot read the
    repository, the completion is refused as unverifiable — never closed as transport."""
    from hermes_cli import kanban_pr_acceptance as pa

    real_api = pa._api

    def refuse_only_the_probe(endpoint, **kwargs):
        if endpoint == "graphql":
            raise pa._GateAuthError("HTTP 403 on graphql")
        return real_api(endpoint, **kwargs)

    with connect() as conn:
        tid = kb.create_task(conn, title="probe unreadable", completion_contract="local-only")
        _cite(conn, tid, PR)
        acceptance["pr_title"] = f"fix: the thing [{tid}]"
        monkeypatch.setattr(pa, "_api", refuse_only_the_probe)
        assert not kb.complete_task(conn, tid, result="done")
        task, receipt = kb.get_task(conn, tid), _receipt(conn, tid)
    assert task is not None and task.status != "done"
    assert receipt["classification"] == "unverifiable"
    assert "could not be read" in receipt["detail"]


def test_evidence_exists_reads_both_required_context_sources(acceptance, monkeypatch):
    """The probe must agree with `collect_acceptance`: branch protection AND rulesets. A repository
    that gates only through a ruleset is still gated, so a citation there must refuse."""
    from hermes_cli import kanban_pr_acceptance as pa

    def ruleset_only_api(endpoint, *, query=None, paginate=False, profile_home=None):
        if endpoint == "graphql":
            return {"data": {"repository": {"pullRequest": {
                "baseRefName": "main",
                "baseRef": {"branchProtectionRule": None}}}}}     # no classic protection
        assert "/rules/branches/" in endpoint, endpoint
        return [[{"type": "required_status_checks",
                  "parameters": {"required_status_checks": [{"context": "ci", "integration_id": 5}]}}]]

    monkeypatch.setattr(pa, "_api", ruleset_only_api)
    with connect():
        assert store._evidence_exists(PR, None) is True

    def nothing_api(endpoint, *, query=None, paginate=False, profile_home=None):
        if endpoint == "graphql":
            return {"data": {"repository": {"pullRequest": {
                "baseRefName": "main", "baseRef": {"branchProtectionRule": None}}}}}
        return [[]]

    monkeypatch.setattr(pa, "_api", nothing_api)
    with connect():
        assert store._evidence_exists(PR, None) is False


def test_a_probe_against_a_repository_the_login_cannot_see_raises(acceptance, monkeypatch):
    """A null `repository` in the GraphQL answer is an identity problem, not "requires nothing"."""
    from hermes_cli import kanban_pr_acceptance as pa

    monkeypatch.setattr(pa, "_api", lambda endpoint, **kwargs: {"data": {"repository": None}})
    with connect():
        with pytest.raises(pa._GateAuthError):
            store._evidence_exists(PR, None)
