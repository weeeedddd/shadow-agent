"""Shell command classification.

*Assimilated from* ``EverMind-AI/Raven`` (``raven/agent/tools/shell_policy.py``).

What the previous guardrail got wrong
-------------------------------------
It matched a regex against the raw command string. ``rm -rf /`` was caught;
``sudo rm -rf /`` was caught by luck; ``env rm -rf /`` and
``bash -c "rm -rf /"`` walked straight through. A pattern list that can be
defeated by prefixing a word is not a boundary.

Raven's correction, adopted here: **unwrap before you classify.** Peel the
wrapper commands (``sudo``, ``env``, ``nohup``, ``command``, and the shells
themselves) plus leading ``VAR=value`` assignments until the real argv head is
exposed, then classify that. Split on shell boundaries first so every segment
of ``a && b | c`` is judged on its own.

Three verdicts, and the ordering is security-sensitive
------------------------------------------------------
``DENY`` is checked before ``APPROVE`` so a hard-denied command can never be
downgraded into a request for confirmation. Anything that cannot be parsed
resolves to ``APPROVE``, not ``ALLOW`` -- **failing closed**. An unparseable
command is not a safe command; it is an unknown one.

Honest scope
------------
This reads shell syntax, not program behaviour. It cannot see through
``./deploy.sh``, an alias, or ``rm -rf "$TARGET"``. It stops the accident and
the obvious footgun. Containment for hostile input is a sandbox's job, and
this is not one.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Sequence, Tuple


class Verdict(Enum):
    ALLOW = "allow"       # run it
    APPROVE = "approve"   # run only with explicit opt-in
    DENY = "deny"         # never run, opt-in or not


@dataclass
class Judgement:
    verdict: Verdict
    reason: str = ""
    segment: str = ""
    program: str = ""

    @property
    def blocked(self) -> bool:
        return self.verdict is not Verdict.ALLOW


# Wrappers whose arguments hide the real command. The set maps each wrapper to
# the options that consume a following value, so those values are not mistaken
# for the program name.
_WRAPPER_OPTS_WITH_VALUE = {
    "command": frozenset(),
    "nohup": frozenset(),
    "time": frozenset(),
    "xargs": frozenset({"-I", "-n", "-P", "-d", "-a", "-E", "-s"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "sudo": frozenset(
        {"-C", "--close-from", "-D", "--chdir", "-g", "--group", "-h", "--host",
         "-p", "--prompt", "-r", "--role", "-t", "--type", "-T", "--command-timeout",
         "-u", "--user"}
    ),
    "doas": frozenset({"-u", "-C"}),
}
_SHELL_WRAPPERS = frozenset({"sh", "bash", "dash", "ksh", "zsh", "busybox"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_BOUNDARY = re.compile(r"\s*(?:\|\||&&|\||;|\n)\s*")

_POWER = frozenset({"halt", "poweroff", "reboot", "shutdown", "init", "telinit"})
_POWER_MULTIPLEXERS = frozenset({"systemctl", "loginctl", "busybox"})

# --- DENY: never runs, regardless of opt-in ----------------------------------
_DENY_PATTERNS: Sequence[Tuple[str, str]] = (
    (r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (r"\bmkfs(\.\w+)?\b", "filesystem format"),
    (r"\bdd\b[^|;]*\bof=\s*/dev/(sd|nvme|hd|disk|vd)", "raw write to a block device"),
    (r">\s*/dev/(sd|nvme|hd|disk|vd)", "redirect onto a block device"),
    (r"\brm\b[^|;]*\s(-{1,2}[\w-]*\s+)*(/|/\*)\s*$", "recursive delete of the filesystem root"),
    (r"\bchmod\b\s+-R\s+0*777\s+/(\s|$)", "world-writable filesystem root"),
    (r"\b(curl|wget)\b[^|;]*\|\s*(sudo\s+)?(ba|z|k)?sh\b", "piping a network fetch into a shell"),
    (r"\bhistory\s+-c\b", "clearing shell history"),
)

# --- APPROVE: real work, needs an explicit opt-in ----------------------------
_APPROVE_PATTERNS: Sequence[Tuple[str, str]] = (
    (r"\brm\b\s+(-{1,2}[\w-]*\s+)*(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)", "recursive delete"),
    (r"\brm\b\s+(-{1,2}[\w-]*\s+)*(~|\$HOME)", "delete under the home directory"),
    (r"\bgit\s+push\b[^|;]*(--force\b|--force-with-lease\b|\s-f\b)", "force-push rewrites published history"),
    (r"\bgit\s+reset\b[^|;]*--hard\b", "hard reset discards uncommitted work"),
    (r"\bgit\s+clean\b\s+-[a-zA-Z]*[fdx]", "git clean deletes untracked files"),
    (r"\bgit\s+(filter-branch|filter-repo)\b", "history rewrite"),
    (r"\bDROP\s+(DATABASE|TABLE|SCHEMA)\b", "destructive SQL"),
    (r"\bTRUNCATE\s+TABLE\b", "destructive SQL"),
    (r"\b(kill|pkill|killall)\b\s+(-9|-KILL)", "forced process termination"),
    (r"\bchown\b\s+-R\b", "recursive ownership change"),
    (r"\bchmod\b\s+-R\b", "recursive permission change"),
    (r"\bmv\b\s+[^|;]*\s+/(\s|$)", "move onto the filesystem root"),
    (r"\b(pip|pip3|npm|yarn|pnpm|cargo|gem|apt|apt-get|brew|choco)\s+(install|add|remove|uninstall)\b",
     "package installation modifies the environment"),
    (r"\b(docker|podman)\s+(system\s+prune|rmi|volume\s+rm)\b", "container/image removal"),
    (r"\b(terraform|tofu)\s+(destroy|apply)\b", "infrastructure mutation"),
    (r"\bkubectl\s+delete\b", "cluster resource deletion"),
    (r"\bcrontab\s+-r\b", "removes all cron jobs"),
    (r">\s*/etc/", "write into system configuration"),
)

# Programs whose arguments are *data*, not commands. `echo 'rm -rf /'` prints a
# string; scanning its arguments for command patterns is a false positive that
# trains users to override the guardrail, which is worse than not having one.
# `bash -c` is deliberately absent: its argument really is a command, and
# `unwrap` recurses into it instead.
_INERT_PROGRAMS = frozenset(
    {"echo", "printf", "true", "false", ":", "cat", "head", "tail", "wc", "grep", "comment"}
)

# Redirection is a property of the segment, not of the program's arguments, so
# these are checked even for inert programs -- `echo x > /etc/passwd` is inert
# in its argument and destructive in its redirect.
_REDIRECT_PATTERNS: Sequence[Tuple[str, str]] = (
    (r">\s*/etc/", "write into system configuration"),
    (r">\s*/boot/", "write into boot configuration"),
    (r">\s*~/\.(bashrc|zshrc|profile|ssh)", "write into shell or ssh configuration"),
)

_DENY = [(re.compile(p, re.IGNORECASE), r) for p, r in _DENY_PATTERNS]
_APPROVE = [(re.compile(p, re.IGNORECASE), r) for p, r in _APPROVE_PATTERNS]
_REDIRECT = [(re.compile(p, re.IGNORECASE), r) for p, r in _REDIRECT_PATTERNS]


def split_segments(command: str) -> List[str]:
    """Split on shell boundaries so each sub-command is judged separately.

    ``ls && rm -rf /`` must not pass because its first segment is harmless.
    """
    return [seg.strip() for seg in _BOUNDARY.split(command) if seg.strip()]


def unwrap(segment: str) -> Tuple[str, List[str]]:
    """Strip wrappers and assignments; return ``(program, argv)``.

    Recurses through the shell wrappers: ``sudo env FOO=1 bash -c "rm -rf /"``
    resolves to ``rm``, which is the whole point. A parse failure returns an
    empty program, which the caller must treat as unknown -- never as safe.
    """
    try:
        argv = shlex.split(segment, posix=True)
    except ValueError:
        return "", []

    guard = 0
    while argv and guard < 12:
        guard += 1

        if _ASSIGNMENT.match(argv[0]):       # FOO=bar cmd
            argv = argv[1:]
            continue

        head = argv[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()

        if head in _SHELL_WRAPPERS:
            # `bash -c "..."` hides an entire command line in one argument.
            for i, token in enumerate(argv[1:], start=1):
                if token == "-c" and i + 1 < len(argv):
                    inner = split_segments(argv[i + 1])
                    return unwrap(inner[0]) if inner else ("", [])
            argv = argv[1:]
            continue

        if head in _WRAPPER_OPTS_WITH_VALUE:
            takes_value = _WRAPPER_OPTS_WITH_VALUE[head]
            rest = argv[1:]
            while rest:
                token = rest[0]
                if token in takes_value and len(rest) > 1:
                    rest = rest[2:]
                elif token.startswith("-") and token != "--":
                    rest = rest[1:]
                elif token == "--":
                    rest = rest[1:]
                    break
                elif _ASSIGNMENT.match(token):
                    rest = rest[1:]
                else:
                    break
            argv = rest
            continue

        return head, argv

    return (argv[0].rsplit("/", 1)[-1].lower(), argv) if argv else ("", [])


def classify(command: str) -> Judgement:
    """Judge a command line.

    Four passes, and the order is the security property:

      0. DENY against the **whole, unsplit** command. Some dangers are the
         pipeline itself -- ``curl … | sh`` only exists as a pipe, and
         splitting on ``|`` first would destroy the very thing being matched.
      1. DENY against each segment's **unwrapped** form. The dangerous command
         inside ``bash -c "…"`` is not visible in the raw text; it becomes
         visible only after :func:`unwrap` recurses into it.
      2. Redirection, checked on the raw segment -- it is a property of the
         segment, not of the program's arguments.
      3. APPROVE against the unwrapped form, skipping programs whose arguments
         are data rather than commands.

    DENY can never be reached *after* an APPROVE match, so a hard-denied
    command can never be downgraded into a request for confirmation.
    """
    command = (command or "").strip()
    if not command:
        return Judgement(Verdict.ALLOW, "empty command")

    # Pass 0 -- the pipeline as a whole.
    for pattern, reason in _DENY:
        if pattern.search(command):
            return Judgement(Verdict.DENY, reason, command, unwrap(command)[0])

    segments = split_segments(command)
    resolved = [(seg, *unwrap(seg)) for seg in segments]

    # Pass 1 -- deny on the unwrapped form of every segment.
    for segment, program, argv in resolved:
        effective = " ".join(argv) if argv else segment
        for pattern, reason in _DENY:
            if pattern.search(effective):
                return Judgement(Verdict.DENY, reason, segment, program)

    # Passes 2 and 3 -- approval-required.
    for segment, program, argv in resolved:
        for pattern, reason in _REDIRECT:
            if pattern.search(segment):
                return Judgement(Verdict.APPROVE, reason, segment, program)

        if program in _INERT_PROGRAMS:
            continue  # its arguments are data; nothing there is a command

        if program in _POWER:
            return Judgement(Verdict.APPROVE, "host power state change", segment, program)
        if program in _POWER_MULTIPLEXERS and any(
            a.lower() in _POWER or a in ("0", "6") for a in argv[1:3]
        ):
            return Judgement(Verdict.APPROVE, "host power state change", segment, program)

        if not program and segment:
            # Unparseable: fail closed. An unknown command is not a safe one.
            return Judgement(
                Verdict.APPROVE,
                "command could not be parsed; approval required",
                segment,
            )

        effective = " ".join(argv) if argv else segment
        for pattern, reason in _APPROVE:
            if pattern.search(effective):
                return Judgement(Verdict.APPROVE, reason, segment, program)

    return Judgement(Verdict.ALLOW, "no destructive pattern matched")
