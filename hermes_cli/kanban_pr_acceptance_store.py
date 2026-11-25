"""Persist acceptance with the same ownership snapshot as the terminal write."""
from __future__ import annotations

import json
import re
import subprocess
from urllib.parse import quote

from hermes_cli import kanban_pr_acceptance as _accept
from hermes_cli.kanban_db_connect import write_txn
from hermes_cli.kanban_pr_acceptance import _PR, collect_acceptance

# The dispatcher's duplicate-PR guard already treats "a PR URL in a card comment" as "this card
# has a PR" (hermes_cli/kanban_db_dispatch.py::_RESPAWN_GUARD_PR_URL_RE). The completion gate
# reads the same shape, so the two cannot disagree about whether a card published something.
# A literal copy, not an import: kanban_db_dispatch imports kanban_db, which imports THIS module
# from inside complete_task, so importing it here is an import cycle on the completion path.
_PR_URL_RE = re.compile(r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+", re.IGNORECASE)

# Handoff keys a worker uses for "the PR I published". ``published_pr`` is the declared one and
# keeps its own refusal; the rest are the undeclared spellings that escape it (t_3813815d handed
# over ``{"pr": {"number": 19284, "head": ..., "files": ...}}`` and completed).
_PR_KEYS = ("pr", "pr_url", "pr_number", "pull_request", "pull_request_url", "pullrequest",
            "merge_pr", "merged_pr")
# Fields only a *publication* report carries: they describe the branch/diff this card pushed.
# A lane card citing somebody else's PR reports its review verdict instead, so requiring one of
# these keeps citations out (measured over 400 recent cards: t_3813815d is the only local-only
# dict hit, and it is exactly the escaped case).
_PUBLICATION_FIELDS = ("head", "base", "head_sha", "files", "file", "diff", "mergeable",
                       "mergeStateStatus", "behind_by", "headRefName")
# Bound on PR references read per completion. A thread citing more cannot be attributed fully,
# and a partial attribution proves nothing, so the caller refuses instead.
_MAX_PR_REFERENCES = 8

_DETAIL = ("This card declares completion_contract=local-only but {source} {pr}, so no merge/CI/"
           "staging evidence can be verified. A PR-publishing card must be created with the "
           "repository slug (OWNER/REPO) or the exact PR URL as its contract.")
# A PR whose repository requires NO status checks has no merge/CI/staging verdict to fetch, so
# refusing a local-only card over it is not fail-closed — it is always-closed, and it strands every
# card that publishes to a fork (PM rule 2026-11-25 on t_81d713e5: refuse only when the evidence
# still exists to be fetched; the PR is then transport, and local evidence closes the card).
_TRANSPORT_DETAIL = ("This card declares completion_contract=local-only and {source} {pr}, but that "
                     "repository requires no status checks on its base branch, so no merge/CI/"
                     "staging verdict exists there to verify. The PR is transport, not evidence: the "
                     "completion closes on this card's local evidence, and the URL stays on this "
                     "receipt (and in metadata.published_pr_transport) as the trace.")
_RECOVERY = ("Report the PR in the completion prose instead of a metadata PR key, or have the board "
             "owner re-create the card with a PR contract and retry completion there.")


def _snapshot(conn, task_id):
    row = conn.execute("SELECT current_run_id, status, completion_contract FROM tasks WHERE id=?", (task_id,)).fetchone()
    return tuple(row) if row else None


def _refusal(published_pr, detail, classification="contract_missing"):
    """Refuse the completion. Same receipt shape ``collect_acceptance`` produces."""
    return {
        "ok": False, "classification": classification, "head_sha": None,
        "pr_url": published_pr, "checks": [], "merge_commit_sha": None, "merged_at": None,
        "detail": detail, "recovery": _RECOVERY,
    }


def _transport_receipt(published_pr, source):
    """The attributed PR cannot carry a verdict: record it and let the card close on local evidence.

    ``ok=True`` is deliberate and narrow — it is only reached after ``_evidence_exists`` positively
    established that the PR's repository requires no status checks at all, so there is nothing that
    could have been verified. The receipt keeps the URL on the task event log as the trace.
    """
    return {
        "ok": True, "classification": "transport_only", "head_sha": None,
        "pr_url": published_pr, "checks": [], "merge_commit_sha": None, "merged_at": None,
        "detail": _TRANSPORT_DETAIL.format(source=source, pr=published_pr), "recovery": "",
    }


def _evidence_exists(url, assignee):
    """Does ``url``'s repository still hold evidence a completion could be verified against?

    ``True``  — the PR's base branch requires status checks: the PR CAN carry a verdict.
    ``False`` — it requires none (fork: no branch protection, no rulesets), read from both sources
                ``collect_acceptance`` itself uses, so the two cannot disagree.
    Raises when the repository cannot be read — "could not read" is never "requires nothing".
    """
    match = _PR.fullmatch(url)
    if not match:
        raise ValueError(f"{url!r} is not a PR URL")
    repo, number = match[1], int(match[2])
    owner, name = repo.split("/")
    profile_home = _accept._assignee_profile_home(assignee)
    query = (f'{{repository(owner:{json.dumps(owner)},name:{json.dumps(name)})'
             f'{{pullRequest(number:{number:d}){{baseRefName '
             f'baseRef{{branchProtectionRule{{requiredStatusChecks{{context app{{databaseId}}}}}}}}}}}}}}')
    repository = _accept._api("graphql", query=query, profile_home=profile_home)["data"]["repository"]
    if repository is None:
        raise _accept._GateAuthError(f"HTTP 404 on graphql {repo}")
    pr = repository.get("pullRequest") or {}
    branch = pr.get("baseRefName")
    if not branch:
        raise ValueError(f"the base branch of {repo}#{number:d} is unavailable")
    protection = (pr.get("baseRef") or {}).get("branchProtectionRule") or {}
    required = {(r["context"], (r.get("app") or {}).get("databaseId"))
                for r in protection.get("requiredStatusChecks") or []}
    for page in _accept._api(f"repos/{repo}/rules/branches/{quote(branch, safe='')}?per_page=100",
                             paginate=True, profile_home=profile_home):
        for rule in page:
            if rule.get("type") == "required_status_checks":
                required.update((r["context"], r.get("integration_id"))
                                for r in rule["parameters"]["required_status_checks"])
    return bool(required)


def _handoff_publication(metadata):
    """``(ref, url)`` for the PR this completion handoff reports *publishing*, or ``None``.

    This is the shape that escaped on t_3813815d: a publication report under a metadata key the
    gate never read. ``ref`` is the URL when the handoff carried one, else ``#<number>``.
    """
    if not isinstance(metadata, dict):
        return None
    for key in _PR_KEYS:
        value = metadata.get(key)
        if isinstance(value, dict):
            number = value.get("number") or value.get("pr_number")
            if not isinstance(number, int) or isinstance(number, bool):
                continue
            if not any(field in value for field in _PUBLICATION_FIELDS):
                continue  # a citation/review report, not a publication of this card's own PR
            url = value.get("url") or value.get("html_url")
            if isinstance(url, str) and _PR.fullmatch(url):
                return url, url
            return f"#{number}", None
        elif isinstance(value, str) and _PR.fullmatch(value):
            return value, value
    return None


def _commented_pr_urls(conn, task_id):
    """Distinct PR URLs in the card's comment thread, first-seen order."""
    urls = []
    for row in conn.execute("SELECT body FROM task_comments WHERE task_id = ? ORDER BY id", (task_id,)):
        for match in _PR_URL_RE.finditer(row[0] or ""):
            url = match.group(0).rstrip(").,;")
            if url not in urls:
                urls.append(url)
    return urls


def _published_by(url, task_id, branch_name, assignee):
    """Does ``url``'s PR belong to this card — task id in its title or head ref, or its head ref
    being the branch this card was cut on? One REST read. Anything else is a lane card citing
    somebody else's PR (measured: 2 of 9 open local-only cards citing a URL cite another card's).
    """
    match = _PR.fullmatch(url)
    if not match:
        return False
    pr = _accept._api(f"repos/{match[1]}/pulls/{match[2]}",
                      profile_home=_accept._assignee_profile_home(assignee))
    title = pr.get("title") or ""
    head = ((pr.get("head") or {}).get("ref")) or ""
    return task_id in title or task_id in head or (bool(branch_name) and head == branch_name)


# A git remote arrives in one of four spellings — ``https://host/o/r(.git)``,
# ``ssh://git@host/o/r.git``, scp-like ``git@host:o/r.git``, and a bare local path. The first three
# all name a hosted repository; the last one cannot have published a PR. Splitting on ``:`` is what
# broke the HTTPS arm (``https://`` became ``https///``, so ``"https"`` counted as a path segment).
_REMOTE_URL_RE = re.compile(
    r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*)://(?:[^@/]+@)?(?P<host>[^/]+)(?P<path>/.*)$")
_REMOTE_SCP_RE = re.compile(r"^(?:[^@/\\]+@)?(?P<host>[^:/\\]+):(?P<path>.+)$")


def _remote_slug(remote):
    """``owner/repo`` from any git remote spelling, or ``None`` when it is not a hosted remote."""
    remote = (remote or "").strip()
    if not remote:
        return None
    match = _REMOTE_URL_RE.match(remote)
    if match is not None:
        if match["scheme"].lower() == "file":
            return None
        path = match["path"]
    else:
        if "://" in remote or "\\" in remote:
            return None                      # a scheme this parser does not know, or a Windows path
        match = _REMOTE_SCP_RE.match(remote)
        if match is None or (len(match["host"]) == 1 and match["host"].isalpha()):
            return None                      # a local POSIX path / a ``C:`` drive, not a host
        path = match["path"]
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return None
    return f"{parts[-2]}/{parts[-1].removesuffix('.git')}"


def _workspace_remote(conn, task_id):
    """``owner/repo`` of the card workspace's git remote, or ``None`` when it has none.

    A workspace with no remote cannot have published a PR, so "no remote" is the absence of
    evidence rather than a failed read; a git that cannot run at all IS a failed read.
    """
    row = conn.execute("SELECT workspace_path FROM tasks WHERE id=?", (task_id,)).fetchone()
    path = row[0] if row else None
    if not path:
        return None
    try:
        result = subprocess.run(["git", "-C", str(path), "config", "--get", "remote.origin.url"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        raise _accept._GateAuthError(f"git could not read the workspace remote of {task_id}") from None
    if result.returncode != 0:
        return None
    remote = (result.stdout or "").strip()
    if not remote:
        return None
    return _remote_slug(remote)


def _branch_publication(conn, task_id, branch_name, assignee):
    """URL of an OPEN PR whose head is this card's own branch, or ``None``."""
    repo = _workspace_remote(conn, task_id)
    if repo is None:
        return None
    owner = repo.split("/")[0]
    pulls = _accept._api(f"repos/{repo}/pulls?state=open&head={quote(owner, safe='')}:"
                         f"{quote(branch_name, safe='')}",
                         profile_home=_accept._assignee_profile_home(assignee))
    for pull in pulls if isinstance(pulls, list) else []:
        url = pull.get("html_url")
        if isinstance(url, str) and _PR.fullmatch(url):
            return url
    return None


def _derived_publications(conn, task_id, metadata, branch_name, assignee):
    """Every ``(pr_ref_for_detail, source_phrase)`` a local-only card shows it published, in order.

    Empty means no arm found any publication evidence — the card really is local-only, and a
    genuinely local card reads nothing over the network. Every unreadable source raises and the
    caller refuses: an unreadable source must never be read as "nothing was published".

    All candidates are returned, not just the first: the caller must judge each PR's repository
    (does evidence still exist there?), and stopping at the first one would let a card that cites
    both a gated and an ungated PR close on the ungated one.
    """
    found = []
    reported = None
    handoff = _handoff_publication(metadata)
    if handoff is not None:
        ref, url = handoff
        if url is None or _published_by(url, task_id, branch_name, assignee):
            found.append((ref, "the completion handoff's metadata PR report names"))
        else:
            # A failed attribution is not evidence of absence: the handoff itself reports publishing
            # a PR, so arms (b)/(c) may corroborate *which* PR, but "nothing was published" stopped
            # being an available answer. Dropping the report here closed the card and skipped both
            # fallbacks — the fail-open that rewarded metadata ambiguity (adding `url` to an
            # otherwise refused report flipped refuse -> close).
            reported = url
    urls = _commented_pr_urls(conn, task_id)
    if len(urls) > _MAX_PR_REFERENCES:
        raise ValueError(f"{len(urls)} distinct PR references in the comment thread cannot be attributed")
    for url in urls:
        # The card's own thread citing the very PR its handoff reports publishing attributes it.
        if url == reported or _published_by(url, task_id, branch_name, assignee):
            found.append((url, "a card comment cites"))
    if branch_name:
        url = _branch_publication(conn, task_id, branch_name, assignee)
        if url is not None:
            found.append((url, f"the card branch {branch_name} has an open PR at"))
    if reported is not None:
        found.append((reported, "the completion handoff's metadata PR report names (unattributed)"))
    return found


def prepare_acceptance(conn, task_id, expected_run_id, metadata):
    snapshot = _snapshot(conn, task_id)
    if snapshot is None:
        return False
    run_id, status, contract = snapshot
    published_pr = metadata.get("published_pr") if isinstance(metadata, dict) else None
    if not contract or contract == "local-only":
        # A card that publishes a PR cannot close as local-only: returning None here is what
        # let a worker open a PR and complete in the same breath, leaving it orphaned
        # (t_0e6376ca, PR #19268; still true for an UNDECLARED publication — t_3813815d closed
        # done while PR #19284 was OPEN because the handoff never used published_pr).
        if isinstance(published_pr, str) and _PR.fullmatch(published_pr):
            # A DECLARED publication on a local-only contract is a contract error, not an evidence
            # question: the worker chose to report a PR while declaring the card needs no PR
            # acceptance. It refuses without a network read (t_52a486d7), and the fix is to stop
            # declaring it — so this arm is deliberately NOT subject to the transport rule below.
            return snapshot, _refusal(
                published_pr,
                _DETAIL.format(source="the handoff hands over", pr=published_pr)
                + " It was declared as metadata.published_pr.")
        row = conn.execute("SELECT assignee, branch_name FROM tasks WHERE id=?", (task_id,)).fetchone()
        assignee, branch_name = (row[0], row[1]) if row else (None, None)
        try:
            candidates = _derived_publications(conn, task_id, metadata, branch_name, assignee)
            # Refuse only while evidence still EXISTS to be fetched (PM rule 2026-11-25 on
            # t_81d713e5): a PR whose repository requires no status checks can never carry a
            # merge/CI/staging verdict, so refusing a local-only card over it is always-closed — it
            # strands every card that publishes to a fork. The rule is narrow, not a waiver: any
            # candidate whose repository DOES gate its base branch still refuses the whole card.
            transport = []
            for ref, source in candidates:
                url = ref if _PR.fullmatch(ref) else None
                if url is not None and not _evidence_exists(url, assignee):
                    transport.append((url, source))
                    continue
                return snapshot, _refusal(ref, _DETAIL.format(source=source, pr=ref))
        except (OSError, subprocess.SubprocessError, ValueError, _accept._GateAuthError) as exc:
            # Fail closed, named: a completion must not pass by making the evidence unreadable.
            return snapshot, _refusal(
                None, f"The publication evidence a local-only card must be checked against could "
                      f"not be read ({exc}); an unreadable source is never read as \"nothing was "
                      f"published\".", classification="unverifiable")
        if not transport:
            return None
        # Every publication this card shows is transport-only: record it and close on local evidence.
        ref, source = transport[0]
        if isinstance(metadata, dict):
            metadata["published_pr_transport"] = ref
        return snapshot, _transport_receipt(ref, source)
    if status not in {"running", "ready", "blocked", "review"} or (expected_run_id is not None and run_id != expected_run_id):
        return False
    match = _PR.fullmatch(published_pr) if isinstance(published_pr, str) else None
    # Publication binds once. Retrying cannot replace the task's PR with a green sibling.
    if match and contract == match[1]:
        with write_txn(conn):
            if _snapshot(conn, task_id) != snapshot:
                return False
            conn.execute("UPDATE tasks SET completion_contract=? WHERE id=?", (published_pr, task_id))
        snapshot = (run_id, status, published_pr)
        contract = published_pr
    # The assignee profile's gh login owns the repo: acceptance must not run as
    # the ambient login of whichever process completes the card (#122689).
    assignee = conn.execute("SELECT assignee FROM tasks WHERE id=?", (task_id,)).fetchone()["assignee"]
    return snapshot, collect_acceptance(contract, published_pr, assignee=assignee)


def record_acceptance(conn, task_id, acceptance):
    """Called under complete_task's write_txn, before its terminal UPDATE."""
    from hermes_cli.kanban_db import _append_event
    snapshot, receipt = acceptance
    if _snapshot(conn, task_id) != snapshot:
        return False
    _append_event(conn, task_id, "pr_acceptance", receipt, run_id=snapshot[0])
    if not receipt["ok"]:
        detail = f"PR acceptance {receipt['classification']}: {receipt.get('detail', '')} {receipt['recovery']}"
        conn.execute("UPDATE tasks SET last_failure_error=? WHERE id=?", (detail, task_id))
    return receipt["ok"]
