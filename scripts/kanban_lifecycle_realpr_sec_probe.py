"""Prove `sec_required_if` against REAL PR diffs with the REAL profile identity.

The board-evidence script runs under a temp HERMES_HOME, where an assignee profile
cannot be resolved (so the diff is unreadable and sec fails closed). This probe
runs with the real home so `resolve_profile_env` finds the assignee's own gh login,
exactly as the dispatcher does in production, and prints the decision for two real
PRs. READ-ONLY: no board is opened, no DB is written.
"""
import os
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
os.environ.pop("HERMES_KANBAN_DB", None)
os.environ.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
os.environ.pop("HERMES_PROFILE", None)
sys.path.insert(0, str(root))

from hermes_cli import kanban_lifecycle as kbl   # noqa: E402

PATTERNS = [".github/workflows/**", ".github/actions/**", "**/secrets/**",
            "supabase/migrations/**"]
CASES = [
    ("workflow-touching PR", "https://github.com/Cong-ty-TNHH-Ban-Gioi/bangioi/pull/19197"),
    ("docs/code-only PR", "https://github.com/vitran1987/hermes-agent/pull/37"),
]
for label, url in CASES:
    for assignee in (None, "code-reviewer-bangioi"):
        paths = kbl.changed_paths(url, assignee=assignee)
        who = assignee or "ambient login"
        if paths is None:
            print(f"  {label:<22} as {who:<24} -> UNREADABLE (sec would stay required)")
            continue
        required, hits = kbl.sec_required(paths, PATTERNS)
        print(f"  {label:<22} as {who:<24} -> {len(paths)} files  sec_required={required}  matched={hits}")
