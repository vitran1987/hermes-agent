"""Exact-head GitHub acceptance for explicitly declared PR tasks.

Network work happens outside SQLite transactions. The lifecycle owner persists
receipts only after rechecking the captured run/status/contract under its lock.

``gh`` runs as the card's assignee profile (``profile_home``), not the ambient
login: :func:`_gh_env` resolves that profile's own credentials/config for the
subprocess — a multi-profile host's default ``gh`` login cannot read another
org's private repos (#122689).

"Done" is not "the PR's checks are green" (CEO directive 2026-11-24): a card may
close only when its PR is **MERGED** *and* the merge commit itself carries the
CI Full run and the staging deploy. A PR left OPEN cannot pass any more, and the
post-merge evidence is read at run/job/commit-status level — a workflow-level
``conclusion`` is not evidence on its own (a run whose jobs were all skipped
still reports ``success``).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PR = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)")

# Post-merge pipeline evidence (CEO directive 2026-11-24). The names are the BanGioi
# pipeline's; a repository that runs a differently named one overrides them under
# ``kanban.acceptance`` in config.yaml. A name the repository does not carry is recorded
# as ``not_applicable`` in the receipt — the gate never invents a pipeline, and never
# reports a missing one as green.
_ACCEPTANCE_DEFAULTS = {
    "ci_workflow": "CI Full",
    "deploy_workflow_prefix": "Deploy Staging",
    "deploy_status_context": "deploy/staging",
}


def _policy() -> dict:
    from hermes_cli.config import load_config
    policy = dict(_ACCEPTANCE_DEFAULTS)
    policy.update(((load_config() or {}).get("kanban") or {}).get("acceptance") or {})
    return policy


def validate_contract(value: str | None) -> str:
    if value is None or value == "local-only":
        return "local-only"
    if not isinstance(value, str) or not (_REPO.fullmatch(value) or _PR.fullmatch(value)):
        raise ValueError("completion_contract must be local-only, OWNER/REPO, or an exact GitHub PR URL")
    return value


def _api(endpoint: str, *, query: str | None = None, paginate: bool = False,
         profile_home: str | None = None):
    command = ["gh", "api", endpoint, "--hostname", "github.com"]
    if query is not None:
        command += ["-f", "query=" + query]
    if paginate:
        command += ["--paginate", "--slurp"]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=30,
                                check=True, env=_gh_env(profile_home))
    except subprocess.CalledProcessError as exc:
        # 401/403/404 = the login cannot see this repository (wrong profile identity
        # or missing grant), not a transient API failure. Persist only the status
        # code + endpoint, never gh's stderr (credentials/host details).
        denied = re.search(r"HTTP (40[134])", exc.stderr or "")
        if denied:
            raise _GateAuthError(f"HTTP {denied[1]} on {endpoint.split('?')[0]}") from None
        if exc.returncode == 4:  # gh's authentication-required exit: this profile has no login
            raise _GateAuthError(f"gh has no login for {endpoint.split('?')[0]}") from None
        raise
    value = json.loads(result.stdout)
    if isinstance(value, dict) and value.get("errors"):
        raise ValueError("GitHub returned incomplete GraphQL evidence")
    return value


class _GateAuthError(RuntimeError):
    """gh was refused at HTTP 401/403/404 (or GraphQL returned no repository):
    this profile's login cannot see the repo — an identity problem to fix, not
    an infrastructure blip to retry."""


def _gh_env(profile_home: str | None) -> dict[str, str] | None:
    """Child env for ``gh``: the card's profile identity when one is resolvable.

    The completion boundary runs in the worker (assignee), the CLI, or a
    reviewer/dispatcher turn, so an ambient ``gh`` login is whichever process
    happened to call it (#122689). ``served_profile_child_env(inherit_credentials=True)``
    is the seam for "this child acts for that profile": it scrubs the launch
    profile's credential residue and overlays the target profile's own
    ``GH_TOKEN``/``GH_CONFIG_DIR`` (its ``.env`` + external secret sources).
    ``None`` keeps the ambient env — unassigned cards behave exactly as before.
    """
    if not profile_home:
        return None
    from tools.environments.local import _is_routed_home, hermes_subprocess_env, served_profile_child_env
    base = hermes_subprocess_env(inherit_credentials=True)
    routed = _is_routed_home(profile_home)
    if routed:
        # gh's config dir decides which login `gh api` uses, yet it is a path, not a
        # credential, so no scrub list sees it; the target's own value is overlaid from its .env.
        base.pop("GH_CONFIG_DIR", None)
    env = served_profile_child_env(base=base, target_home=profile_home, inherit_credentials=True)
    if routed and not (env.keys() & {"GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR"}):
        # HOME/XDG_CONFIG_HOME are still the launch process's: without a login of its own the
        # child would fall through to ~/.config/gh/hosts.yml — the ambient login. Pin gh's config
        # to a profile-owned dir so it fails "not logged in" (exit 4 -> auth) instead.
        env["GH_CONFIG_DIR"] = str(Path(profile_home) / "gh")
    return env


def _is_merged(pr: dict) -> bool:
    """Merged means the change reached the base branch, not merely that the PR is green.

    GraphQL reports a merged PR as ``state: MERGED``; ``CLOSED`` + ``merged`` covers a
    read taken around a branch deletion. An OPEN PR is never merged, however green."""
    return pr.get("state") == "MERGED" or (pr.get("state") == "CLOSED" and pr.get("merged") is True)


def _pages(value) -> list:
    """``gh api --paginate --slurp`` returns one element per page; a plain read returns the object."""
    return value if isinstance(value, list) else [value]


def _workflow_names(repo: str, profile_home: str | None) -> set[str]:
    pages = _api(f"repos/{repo}/actions/workflows?per_page=100", paginate=True, profile_home=profile_home)
    return {w["name"] for page in _pages(pages)
            for w in (page or {}).get("workflows") or [] if w.get("name")}


def _workflow_evidence(repo: str, workflow: str, sha: str, profile_home: str | None) -> dict:
    """Newest run of ``workflow`` at ``sha`` — read at JOB level, not at conclusion level.

    Measured 2026-11-24: a run whose jobs were all ``skipped`` still reports
    ``conclusion: success``, so the conclusion alone would green-light a merge whose CI
    never executed. A completed run only counts when at least one job succeeded and no
    job failed.
    """
    pages = _api(f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100",
                 paginate=True, profile_home=profile_home)
    runs = [r for page in _pages(pages) for r in (page or {}).get("workflow_runs") or []
            if r.get("name") == workflow and r.get("head_sha") == sha]
    if not runs:
        return {"state": "missing", "detail": f"no {workflow!r} run exists at the merge commit"}
    run = max(runs, key=lambda r: (r.get("created_at") or "", r.get("id") or 0))
    evidence = {"state": "pending", "run_id": run.get("id"), "url": run.get("html_url"),
                "status": run.get("status"), "conclusion": run.get("conclusion")}
    if run.get("status") != "completed":
        return evidence
    if run.get("conclusion") != "success":
        return {**evidence, "state": "failure"}
    pages = _api(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100",
                 paginate=True, profile_home=profile_home)
    conclusions = [j.get("conclusion") for page in _pages(pages) for j in (page or {}).get("jobs") or []]
    evidence["jobs"] = {"total": len(conclusions),
                        "success": conclusions.count("success"),
                        "failed": sum(1 for c in conclusions if c in {"failure", "cancelled", "timed_out"})}
    if evidence["jobs"]["success"] and not evidence["jobs"]["failed"]:
        return {**evidence, "state": "success"}
    return {**evidence, "state": "missing",
            "detail": f"{workflow} concluded success but no job succeeded (all skipped)"}


def _staging_status(repo: str, sha: str, context: str, profile_home: str | None) -> dict | None:
    pages = _api(f"repos/{repo}/commits/{sha}/statuses?per_page=100",
                 paginate=True, profile_home=profile_home)
    records = [s for page in _pages(pages) for s in (page if isinstance(page, list) else [page])]
    statuses = [s for s in records if isinstance(s, dict) and s.get("context") == context]
    return max(statuses, key=lambda s: s.get("id") or 0) if statuses else None


def _staging_evidence(repo: str, branch: str, merge_sha: str, context: str,
                      profile_home: str | None) -> dict:
    """The staging deploy of the merge commit, read from the pipeline's own commit status.

    ``deploy/staging`` is written by the deploy job on success and by the failure-notify
    job on any failure (deploy OR smoke test), so its newest state is a job-level receipt.
    A later base-branch tip that CONTAINS the merge commit is accepted too: the staging
    concurrency group is latest-wins, so a cancelled run can leave the merge commit at
    ``pending`` while a descendant tip carries the terminal result for the same tree.
    """
    status = _staging_status(repo, merge_sha, context, profile_home)
    evidence = {"sha": merge_sha}
    if not (status and status.get("state") == "success"):
        tip = _api(f"repos/{repo}/git/ref/heads/{quote(branch, safe='')}",
                   profile_home=profile_home)["object"]["sha"]
        if tip and tip != merge_sha:
            compare = _api(f"repos/{repo}/compare/{merge_sha}...{tip}", profile_home=profile_home)
            if compare.get("status") in {"ahead", "identical"}:
                tip_status = _staging_status(repo, tip, context, profile_home)
                if tip_status and tip_status.get("state") == "success":
                    status, evidence = tip_status, {"sha": tip, "contains_merge_commit": True}
    if status is None:
        return {**evidence, "state": "missing",
                "detail": f"no {context!r} status on the merge commit or on {branch!r}"}
    # "success" is the pipeline's terminal verdict for a real deploy AND for an explicit
    # skip ("skipped: no project changed"); the outcome is recorded, never widened.
    return {**evidence, "state": status.get("state"), "description": status.get("description") or "",
            "target_url": status.get("target_url"), "created_at": status.get("created_at"),
            "outcome": {"success": "success", "pending": "pending", "failure": "failure",
                        "error": "failure"}.get(status.get("state") or "", "infra")}


def _post_merge_evidence(repo: str, branch: str, merge_sha: str,
                         profile_home: str | None, receipt: dict) -> list[str]:
    """CI Full + staging evidence for the MERGE COMMIT; returns one outcome per signal."""
    policy = _policy()
    workflows = _workflow_names(repo, profile_home)
    evidence, outcomes = {}, []
    ci_workflow = policy["ci_workflow"]
    if ci_workflow in workflows:
        evidence["ci_full"] = _workflow_evidence(repo, ci_workflow, merge_sha, profile_home)
        outcomes.append(evidence["ci_full"]["state"])
    else:
        evidence["ci_full"] = {"state": "not_applicable",
                               "detail": f"repository carries no {ci_workflow!r} workflow"}
    deploy_prefix = policy["deploy_workflow_prefix"]
    if any(name.startswith(deploy_prefix) for name in workflows):
        staging = _staging_evidence(repo, branch, merge_sha, policy["deploy_status_context"], profile_home)
        evidence["staging"] = staging
        outcomes.append(staging.get("outcome") or staging["state"])
    else:
        evidence["staging"] = {"state": "not_applicable",
                               "detail": f"repository carries no {deploy_prefix}* workflow"}
    receipt["post_merge"] = evidence
    return outcomes


def _assignee_profile_home(assignee: str | None) -> str | None:
    """Home whose ``gh`` login must read the contract repo — the assignee's, resolved
    exactly as the dispatcher resolves the worker's home — or None (unassigned) so the
    ambient login is used. An assigned card whose profile cannot be resolved is an
    identity failure (``auth``), never a silent fall-through to the ambient login."""
    if not assignee:
        return None
    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env
    try:
        return resolve_profile_env(normalize_profile_name(assignee))
    except (FileNotFoundError, ValueError):
        raise _GateAuthError(f"assignee profile {assignee!r} cannot be resolved") from None


def collect_acceptance(contract: str, published_pr: str | None,
                       assignee: str | None = None) -> dict:
    receipt = {"ok": False, "classification": "missing", "head_sha": None,
               "pr_url": published_pr, "checks": [], "merge_commit_sha": None, "merged_at": None,
               "recovery": "Fix required failures, rerun infrastructure checks or wait, then retry completion. "
                           "Use kanban_block if human input is needed; receipts remain on the task event log."}
    try:
        profile_home = _assignee_profile_home(assignee)
        declared = _PR.fullmatch(contract)
        url = contract if declared else published_pr
        match = _PR.fullmatch(url or "")
        if not match or (not declared and match[1] != contract) or (declared and published_pr and published_pr != contract):
            receipt["detail"] = "Supply metadata.published_pr matching the persisted completion contract."
            return receipt
        repo, number = match[1], int(match[2])
        receipt["pr_url"] = url
        owner, name = repo.split("/")
        query = f'{{repository(owner:{json.dumps(owner)},name:{json.dumps(name)}){{pullRequest(number:{number:d}){{headRefOid baseRefName state merged mergedAt mergeCommit{{oid}}\n            baseRef{{branchProtectionRule{{requiredStatusChecks{{context app{{databaseId}}}}}}}}}}}}}}'
        repository = _api("graphql", query=query, profile_home=profile_home)["data"]["repository"]
        if repository is None:
            # A private repo the login cannot read resolves to null, not an error.
            raise _GateAuthError(f"HTTP 404 on graphql {repo}")
        pr = repository["pullRequest"]
        sha, branch = pr["headRefOid"], pr["baseRefName"]
        receipt["head_sha"] = sha
        receipt["merged_at"] = pr.get("mergedAt")
        receipt["merge_commit_sha"] = (pr.get("mergeCommit") or {}).get("oid")
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("current head is unavailable")
        # A card is done when its PR is MERGED, not when its checks are green: an OPEN PR
        # used to pass here and left the publication orphaned (t_0e6376ca, PR #19268).
        if not _is_merged(pr):
            receipt.update(classification="not_merged",
                           detail=f"The PR is {pr.get('state')} and has not been merged: a card may close only "
                                  f"once the PR is MERGED into {branch}. Hand it to the merge lane, then retry completion.")
            return receipt
        merge_sha = receipt["merge_commit_sha"]
        if not (isinstance(merge_sha, str) and re.fullmatch(r"[0-9a-f]{40}", merge_sha)):
            raise ValueError("merged PR has no merge commit to verify CI/staging against")
        protection = (pr.get("baseRef") or {}).get("branchProtectionRule") or {}
        required = {(r["context"], (r.get("app") or {}).get("databaseId")) for r in protection.get("requiredStatusChecks", [])}
        rules = _api(f"repos/{repo}/rules/branches/{quote(branch, safe='')}?per_page=100",
                     paginate=True, profile_home=profile_home)
        for page in rules:
            for rule in page:
                if rule["type"] == "required_status_checks":
                    required.update((r["context"], r.get("integration_id"))
                                    for r in rule["parameters"]["required_status_checks"])
        receipt["required"] = [{"context": c, "app_id": a} for c, a in sorted(required, key=str)]
        if not required:
            receipt["detail"] = "No repository-required checks are configured; explicitly use a local-only contract for non-CI tasks."
            return receipt
        pages = _api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100&filter=latest",
                     paginate=True, profile_home=profile_home)
        runs = [run for page in pages for run in page["check_runs"]]
        if len({r["id"] for r in runs}) != pages[0]["total_count"]:
            raise ValueError("Incomplete check-run pagination")
        statuses = [{**s, "sha": sha} for page in _api(f"repos/{repo}/commits/{sha}/statuses?per_page=100",
                                                       paginate=True, profile_home=profile_home) for s in page]
        outcomes = []
        for context, app_id in sorted(required, key=str):
            matching = [r for r in runs if r["name"] == context and
                        (app_id in (None, -1) or r["app"]["id"] == app_id)]
            # A legacy status can satisfy an unpinned context, but never a check pinned to an app.
            legacy = [s for s in statuses if s["context"] == context] if app_id in (None, -1) else []
            selected = matching + ([max(legacy, key=lambda s: s["id"])] if legacy else [])
            if not selected:
                outcomes.append("missing")
                receipt["checks"].append({"name": context, "classification": "missing", "head_sha": sha})
            for check in selected:
                is_run = "conclusion" in check
                outcome = check.get("conclusion") if is_run else check["state"]
                classification = _classify(check, sha, outcome, is_run)
                outcomes.append(classification)
                receipt["checks"].append({"name": context, "id": check["id"],
                    "url": check.get("html_url") or check.get("target_url"),
                    "head_sha": check.get("head_sha", check.get("sha")),
                    "classification": classification, "conclusion": outcome})
        # Re-read after all pages: old-head successes are never transferable.
        current = _api(f"repos/{repo}/pulls/{number}", profile_home=profile_home)
        if (current["head"]["sha"] != sha or current["base"]["ref"] != branch
                or not current.get("merged")):
            receipt.update(classification="stale", detail="PR head/base changed while collecting evidence; retry.")
            return receipt
        post_merge = _post_merge_evidence(repo, branch, merge_sha, profile_home, receipt)
        receipt["classification"] = next((x for x in outcomes + post_merge if x != "success"),
                                         "missing" if not outcomes else "success")
        if receipt["classification"] != "success" and not receipt.get("detail"):
            incomplete = [key for key, value in receipt["post_merge"].items()
                          if value.get("outcome", value.get("state")) not in {"success", "not_applicable"}]
            if incomplete:
                receipt["detail"] = (
                    f"Post-merge pipeline incomplete ({', '.join(incomplete)}) for merge commit {merge_sha}: "
                    "the PR is merged, but CI Full and the staging deploy of that commit must both complete. "
                    "Wait for them, then retry completion.")
        receipt["ok"] = receipt["classification"] == "success"
        return receipt
    except _GateAuthError as exc:
        login = f"assignee profile {assignee!r}'s gh login" if assignee else "the ambient gh login"
        receipt.update(classification="auth",
                       detail=f"GitHub refused the acceptance read ({exc}) as {login}; "
                              "fix that profile's GitHub credentials/access to the repository, then retry completion.")
        return receipt
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError):
        # Never persist gh stderr (credentials/host details); the failed phase is actionable.
        receipt.update(classification="infra", detail="GitHub acceptance evidence unavailable or incomplete; check gh authentication/API access and retry.")
        return receipt


def _classify(check: dict, sha: str, outcome: str | None, is_run: bool) -> str:
    if check.get("head_sha", check.get("sha")) != sha:
        return "stale"
    if is_run and check.get("status") != "completed":
        return "pending"
    # A REQUIRED context whose job was path-filtered out reports `skipped`, and GitHub counts it
    # satisfied — which is why such a PR is mergeable and `gh pr checks --required` exits 0.
    # `ci-pr.yml` computes run_typecheck=false for a docs-only change, so typecheck/unit-test/
    # config-drift-check legitimately skip; reading that as `infra` made every docs-only PR card
    # uncompletable (t_e73a6f56, t_e1aadc97, t_ec077972). Only THIS conclusion is forgiven: a
    # sibling required context that failed, errored, is pending or went stale still decides the
    # verdict, because the receipt takes the first non-success.
    return {"success": "success", "skipped": "success", "failure": "failure",
            "error": "infra", "pending": "pending"}.get(outcome, "infra")
