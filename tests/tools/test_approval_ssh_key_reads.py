"""`~/.ssh` private-key READS are gated in every spelling of the home at once.

The only read gate that ever existed keyed on the literal ``users/<name>/.ssh`` substring — a
*spelling* rule. Every dialect of the resolved home folds to ``~/...`` before the pattern pass
(``_normalize_command_for_detection``), so the moment a spelling folded it stopped matching:
``cat ~/.ssh/id_rsa``, ``cat C:/Users/<u>/.ssh/id_rsa``,
``type "C:\\Users\\<u>\\.ssh\\id_rsa"`` and ``cat $HOME/.ssh/id_rsa`` were all unflagged, while
the MSYS ``/c/`` spelling of the identical command was gated. One rule on the folded form closes
the whole matrix together — no spelling left behind, and the ``/c/`` spelling stays gated whichever
of the two rules reaches it first (it folds once #128173's drive-dialect fold is present, and the
``users/<name>/.ssh`` rule gates it before that).

The home is pinned through ``HOME`` so the test means the same thing on every host, and detection
is not host-gated — the same contract ``tests/tools/test_approval_msys_drive_paths.py`` pins.
"""
from __future__ import annotations

import pytest

from tools import approval_detection

detect_dangerous_command = approval_detection.detect_dangerous_command

# A Windows-shaped home that is nobody's real home, so the fold resolves it identically on POSIX
# (expanduser reads HOME) and on Windows (HOME is itself in the fold candidate list).
HOME = r"C:\Users\sshkeyreads"
USER = "sshkeyreads"
READ_KEYS = {
    "access to SSH keys (home path)",
    "access to SSH keys (Windows path)",
}
READ_VERDICT_KEY = "access to SSH keys (home path)"

SPELLINGS = {
    "msys": f"/c/Users/{USER}",
    "msys_upper": f"/C/Users/{USER}",
    "fwd": f"C:/Users/{USER}",
    "back": f"C:\\Users\\{USER}",
    "tilde": "~",
    "dollar": "$HOME",
    "brace": "${HOME}",
}

# The nine non-write verbs the parent card pins: reads, exfiltration, permission/unlink of a key.
READ_VERBS = [
    "cat {p}/.ssh/id_rsa",
    "base64 {p}/.ssh/id_rsa",
    "grep -l BEGIN {p}/.ssh/id_rsa",
    "cp {p}/.ssh/id_rsa /tmp/loot",
    "mv {p}/.ssh/id_rsa /tmp/loot",
    "chmod 644 {p}/.ssh/id_rsa",
    "rm -f {p}/.ssh/id_rsa",
    "shred -u {p}/.ssh/id_rsa",
    "ssh-keygen -y -f {p}/.ssh/id_rsa",
]
# Subset where the new SSH read rule is the FIRST match on every spelling of the home —
# reads, exfiltration, permission/unlink/key-extract. No earlier rule claims these verbs, so the
# new rule owns the verdict (and therefore the reason key) end-to-end.
READ_VERBS_NEW_RULE_OWNS = [
    "cat {p}/.ssh/id_rsa",
    "base64 {p}/.ssh/id_rsa",
    "grep -l BEGIN {p}/.ssh/id_rsa",
    "cp {p}/.ssh/id_rsa /tmp/loot",
    "mv {p}/.ssh/id_rsa /tmp/loot",
    "chmod 644 {p}/.ssh/id_rsa",
    "shred -u {p}/.ssh/id_rsa",
    "ssh-keygen -y -f {p}/.ssh/id_rsa",
]
# Subset where an EARLIER rule legitimately fires first on some spellings. `rm -f` on
# `/c/Users/<u>/.ssh/...` and `/C/Users/<u>/.ssh/...` is already gated by the pre-existing
# `delete in root path` rule (it keys on `rm` + path starting with `/`, fires before the new
# rule); the new rule was appended LAST on purpose so that earlier rules keep their own
# reason keys (AC2 contract). The blast probe (`probe_blast.head_fix.json` /
# `assert_ac3.py` exit 0) is the source of truth for the 9-verb × 5-dialect verdict matrix —
# here we just assert the destructive-verb row stays gated, regardless of which rule owns it.
READ_VERBS_FLAGGED_ANYWAY = [
    "rm -f {p}/.ssh/id_rsa",
]


@pytest.fixture(autouse=True)
def _pinned_home(monkeypatch):
    monkeypatch.setenv("HOME", HOME)


def _verdict(command: str) -> tuple[bool, str | None]:
    flagged, key, _ = detect_dangerous_command(command)
    return bool(flagged), key


def _flagged(command: str) -> bool:
    return _verdict(command)[0]


class TestSshKeyReadsGatedInEverySpelling:
    """Every read verb, in every spelling of the same file, is gated."""

    @pytest.mark.parametrize("verb", READ_VERBS_NEW_RULE_OWNS)
    @pytest.mark.parametrize("spelling", SPELLINGS)
    def test_new_rule_owns_verdict_for_read_verbs_in_every_spelling(self, verb, spelling):
        # 8 verbs × 7 spellings = 56 commands where no earlier rule fires: the new SSH read
        # rule owns the verdict AND the reason key on every spelling of the home.
        command = verb.format(p=SPELLINGS[spelling])
        flagged, key = _verdict(command)
        assert flagged is True, command
        assert key in READ_KEYS, (command, key)

    @pytest.mark.parametrize("verb", READ_VERBS_FLAGGED_ANYWAY)
    @pytest.mark.parametrize("spelling", SPELLINGS)
    def test_destructive_verb_stays_gated_in_every_spelling(self, verb, spelling):
        # `rm -f` on `/c/Users/<u>/.ssh/...` and `/C/Users/<u>/.ssh/...` is already gated by the
        # pre-existing `delete in root path` rule (it keys on `rm` + path starting with `/`),
        # which fires before the new rule on purpose. The new rule was appended LAST so that
        # earlier rules keep their own reason keys (no stored approval key moves). The contract
        # here is "flagged True, reason owned by whichever rule fires first on that spelling".
        command = verb.format(p=SPELLINGS[spelling])
        flagged, key = _verdict(command)
        assert flagged is True, command
        assert key is not None, command

    @pytest.mark.parametrize("command", [
        "cat ~/.ssh",                      # bare directory, no trailing slash
        "ls ~/.ssh -la",                   # flag AFTER the path (boundary, not `(?:/|$)`)
        "ls ~/.ssh",
        "cat ~/.ssh/authorized_keys",
        'type "C:\\Users\\sshkeyreads\\.ssh\\id_rsa"',   # probe_guard C10 as the card words it
    ])
    def test_shapes_outside_the_verb_matrix_are_gated(self, command):
        assert _flagged(command) is True, command

    def test_read_rule_owns_the_reason_for_reads(self):
        flagged, key = _verdict("cat ~/.ssh/id_rsa")
        assert flagged is True
        assert key == READ_VERDICT_KEY


class TestSshWritesKeepTheirOwnReason:
    """The new read rule is appended last, so writes must still report the WRITE reason —
    a stored approval keyed on the write pattern must not silently start matching reads."""

    @pytest.mark.parametrize("command,expected", [
        ("sed -i 's/a/b/' ~/.ssh/authorized_keys", "in-place edit of sensitive credential/SSH/shell-rc path"),
        ("cp evil.pub ~/.ssh/authorized_keys", "copy/move file into sensitive credential/SSH/shell-rc path"),
        ("echo k > ~/.ssh/authorized_keys", "overwrite system file via redirection"),
        ("echo x | tee ~/.ssh/authorized_keys", "overwrite system file via tee"),
        ("rm -rf ~/.ssh/", "recursive delete"),
    ])
    def test_write_flagged_with_write_reason(self, command, expected):
        flagged, key = _verdict(command)
        assert flagged is True, command
        assert key is not None and expected in key, (command, key)

    @pytest.mark.parametrize("spelling", SPELLINGS)
    def test_write_stays_flagged_in_every_spelling(self, spelling):
        assert _flagged(f"sed -i 's/a/b/' {SPELLINGS[spelling]}/.ssh/authorized_keys") is True


class TestNonHomePathsUnaffected:
    """The rule keys on the FOLDED home, so nothing outside it moves."""

    @pytest.mark.parametrize("command", [
        "cat /tmp/x/.ssh/id_rsa",
        "cat ./readme.txt",
        "cat ~/notes.txt",
        "ls /c/Users",
        "cp a.txt b.txt",
        "echo reboot the gateway please",
        "sed -i 's/a/b/' /srv/app/config.yaml",
        "cat ~/.bashrc",          # shell rc READ stays ungated (writes are gated; out of scope)
        "cat ~/.netrc",           # credential read likewise untouched
        "cat ~/.hermes/config.yaml",
    ])
    def test_stays_unflagged(self, command):
        assert _flagged(command) is False, command

    @pytest.mark.parametrize("command", [
        "cat /c/Users/bob/.ssh/id_rsa",
        "cat C:/Users/bob/.ssh/id_rsa",
        "cat C:\\Users\\bob\\.ssh\\id_rsa",
        "cat /c/Users/Public/.ssh/id_rsa",
    ])
    def test_other_users_keys_still_flagged_by_the_windows_rule(self, command):
        # Pre-existing coverage: these paths never fold to ~, so rule 280 keeps owning them.
        assert _flagged(command) is True, command
