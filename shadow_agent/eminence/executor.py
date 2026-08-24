"""The Eminence -- execution against the real machine.

This module is where the framework stops describing and starts doing. It runs
shell commands, captures their output, and enforces the one rule that keeps an
autonomous agent survivable: **nothing irreversible happens quietly.**

Guardrails
----------
A pattern list flags commands capable of unbounded destruction -- recursive
deletion of a root, disk overwrites, force-pushes, history rewrites, piping the
network into a shell. A flagged command is not silently blocked and it is not
silently run. It is *refused pending confirmation*, and the refusal names the
pattern that caught it so the operator can make an informed call.

This is a deliberately shallow defence. It matches on command text, so it stops
the accident, not the adversary. Anything sourced from an untrusted place must
be gated by a human, not by this list.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ..config import Config
from ..core.errors import EminenceError, GuardrailError

# (compiled pattern, human explanation)
GUARDRAILS: Sequence[Tuple[str, str]] = (
    (r"\brm\s+(-[a-zA-Z]*\s+)*-?[a-zA-Z]*[rf][a-zA-Z]*\s+(/|~|\$HOME|\.)(\s|$)",
     "recursive delete rooted at the filesystem root, home, or cwd"),
    (r"\bmkfs(\.\w+)?\b", "filesystem format"),
    (r"\bdd\s+.*\bof=/dev/", "raw write to a block device"),
    (r">\s*/dev/(sd|nvme|hd|disk)", "redirect onto a block device"),
    (r"\bgit\s+push\b.*(--force\b|-f\b)", "force-push rewrites published history"),
    (r"\bgit\s+reset\s+--hard\b", "hard reset discards uncommitted work"),
    (r"\bgit\s+clean\s+-[a-zA-Z]*[fdx]", "git clean deletes untracked files"),
    (r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba)?sh", "piping a network fetch into a shell"),
    (r"\bchmod\s+-R\s+777\b", "recursive world-writable permissions"),
    (r":\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:", "fork bomb"),
    (r"\bshutdown\b|\breboot\b|\bhalt\b", "host power state change"),
    (r"\bDROP\s+(DATABASE|TABLE)\b", "destructive SQL"),
)

_COMPILED = [(re.compile(pattern, re.IGNORECASE), reason) for pattern, reason in GUARDRAILS]


@dataclass
class ExecutionResult:
    """The complete record of one command run."""

    command: str
    returncode: int
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    cwd: str = ""
    truncated: bool = False
    timed_out: bool = False
    refused: bool = False
    refusal_reason: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.refused

    @property
    def output(self) -> str:
        """stdout plus stderr, in the order a terminal would have shown them."""
        parts = [p for p in (self.stdout.rstrip(), self.stderr.rstrip()) if p]
        return "\n".join(parts)

    def brief(self, limit: int = 200) -> str:
        text = self.output.strip().replace("\n", " / ")
        return text[:limit] + ("…" if len(text) > limit else "")


def inspect_command(command: str) -> Optional[str]:
    """Return the guardrail reason if ``command`` is dangerous, else None.

    Now delegates to :mod:`shadow_agent.eminence.policy`, which unwraps
    ``sudo``/``env``/``bash -c`` before classifying and splits on shell
    boundaries. The local ``_COMPILED`` table is retained only as a second
    opinion: the policy is authoritative, and a disagreement resolves toward
    refusal, never toward permission.
    """
    from . import policy

    judgement = policy.classify(command)
    if judgement.blocked:
        return judgement.reason
    for pattern, reason in _COMPILED:
        if pattern.search(command):
            return reason
    return None


def default_shell() -> List[str]:
    """The shell invocation for this platform.

    On Windows a POSIX shell is preferred when one is present -- Git Bash ships
    with Git -- because the command vocabulary the framework generates is
    POSIX. It falls back to ``cmd.exe`` rather than assuming.
    """
    if os.name != "nt":
        return [os.environ.get("SHELL") or "/bin/sh", "-c"]
    for candidate in ("bash", "sh"):
        found = shutil.which(candidate)
        if found:
            return [found, "-c"]
    return [os.environ.get("COMSPEC") or "cmd.exe", "/c"]


class Eminence:
    """Executes commands and file operations. Nothing here is simulated."""

    def __init__(self, config: Optional[Config] = None, root: Optional[Path] = None) -> None:
        self.config = config or Config()
        self.root = Path(root) if root else Path.cwd()
        self.history: List[ExecutionResult] = []

    # --- shell ---------------------------------------------------------------

    def run(
        self,
        command: str,
        cwd: Optional[Path] = None,
        timeout: Optional[float] = None,
        allow_destructive: bool = False,
        env: Optional[Dict[str, str]] = None,
    ) -> ExecutionResult:
        """Run one shell command and capture everything about it.

        Raises :class:`GuardrailError` for a flagged command unless
        ``allow_destructive`` is set -- the caller must opt in explicitly,
        which is the point.
        """
        from . import policy

        cwd = Path(cwd) if cwd else self.root
        timeout = timeout if timeout is not None else self.config.execution.timeout_seconds

        judgement = policy.classify(command)

        # DENY is absolute. `allow_destructive` opts into an approval-gated
        # command; it must never be able to unlock a hard-denied one, or the
        # deny tier is just a slower approve tier.
        if judgement.verdict is policy.Verdict.DENY:
            result = ExecutionResult(
                command=command,
                returncode=126,
                cwd=str(cwd),
                refused=True,
                refusal_reason=judgement.reason,
                stderr=f"denied: {judgement.reason}",
            )
            self.history.append(result)
            raise GuardrailError(
                f"command denied and cannot be overridden: {judgement.reason}",
                command=command,
                pattern=judgement.reason,
            )

        if (
            judgement.verdict is policy.Verdict.APPROVE
            and self.config.execution.confirm_destructive
            and not allow_destructive
        ):
            result = ExecutionResult(
                command=command,
                returncode=126,
                cwd=str(cwd),
                refused=True,
                refusal_reason=judgement.reason,
                stderr=f"refused by guardrail: {judgement.reason}",
            )
            self.history.append(result)
            raise GuardrailError(
                f"command refused pending confirmation: {judgement.reason}",
                command=command,
                pattern=judgement.reason,
            )

        shell = self.config.execution.shell
        argv = ([shell, "-c"] if shell else default_shell()) + [command]

        started = time.perf_counter()
        try:
            completed = subprocess.run(
                argv,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
                env={**os.environ, **(env or {})},
            )
            stdout, stderr, code, timed_out = completed.stdout, completed.stderr, completed.returncode, False
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr if isinstance(exc.stderr, str) else "") + f"\ntimed out after {timeout}s"
            code, timed_out = 124, True
        except OSError as exc:
            raise EminenceError(f"could not launch shell for {command!r}: {exc}") from exc

        duration = time.perf_counter() - started
        cap = self.config.execution.max_output_bytes
        truncated = False
        if len(stdout) > cap:
            stdout, truncated = stdout[:cap] + "\n… output truncated", True
        if len(stderr) > cap:
            stderr, truncated = stderr[:cap] + "\n… output truncated", True

        result = ExecutionResult(
            command=command,
            returncode=code,
            stdout=stdout,
            stderr=stderr,
            duration=duration,
            cwd=str(cwd),
            truncated=truncated,
            timed_out=timed_out,
        )
        self.history.append(result)
        return result

    # --- files ---------------------------------------------------------------

    def read_file(self, path: Path, max_bytes: int = 200_000) -> str:
        path = Path(path)
        if not path.is_absolute():
            path = self.root / path
        if not path.is_file():
            raise EminenceError(f"not a file: {path}")
        try:
            data = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise EminenceError(f"could not read {path}: {exc}") from exc
        return data[:max_bytes]

    def write_file(self, path: Path, content: str, create_parents: bool = True) -> Path:
        """Write a file atomically.

        The temp-file-then-replace dance means an interrupted write leaves the
        original intact rather than a half-written file.
        """
        path = Path(path)
        if not path.is_absolute():
            path = self.root / path
        if create_parents:
            path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".shadow-tmp")
        try:
            tmp.write_text(content, encoding="utf-8", newline="\n")
            tmp.replace(path)
        except OSError as exc:
            raise EminenceError(f"could not write {path}: {exc}") from exc
        return path
