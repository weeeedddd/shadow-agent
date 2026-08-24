"""Real local system state retrieval.

The onboarding sequence is only worth printing if every value in it is true.
This module gathers that truth: operating system, interpreter, shell, working
directory, and repository status -- all read from the machine, none of it
assumed.

Every collector here fails soft. A missing ``git`` binary, a directory that is
not a repository, a repository with no commits, a detached HEAD -- each is a
normal condition that yields a populated object with an honest ``None``, never
a traceback.
"""

from __future__ import annotations

import getpass
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

GIT_TIMEOUT = 5.0


# --- Git ----------------------------------------------------------------------


@dataclass
class GitState:
    """Everything the Architect needs to know about the local repository."""

    available: bool = False          # is a `git` binary on PATH at all
    is_repo: bool = False
    root: Optional[str] = None
    branch: Optional[str] = None
    detached: bool = False
    head_short: Optional[str] = None
    head_subject: Optional[str] = None
    has_commits: bool = False
    tracked_files: int = 0
    modified: int = 0
    staged: int = 0
    untracked: int = 0
    remote: Optional[str] = None
    error: Optional[str] = None

    @property
    def clean(self) -> bool:
        return self.modified == 0 and self.staged == 0

    def summary(self) -> str:
        """One-line human description of the working tree."""
        if not self.available:
            return "git not installed"
        if not self.is_repo:
            return "not a repository"
        if not self.has_commits:
            return "repository initialised, no commits"
        parts: List[str] = []
        if self.staged:
            parts.append(f"{self.staged} staged")
        if self.modified:
            parts.append(f"{self.modified} modified")
        if self.untracked:
            parts.append(f"{self.untracked} untracked")
        return ", ".join(parts) if parts else "clean"


def _git(args: List[str], cwd: Path) -> Optional[str]:
    """Run a git command, returning stripped stdout or None on any failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def collect_git(cwd: Path) -> GitState:
    """Read repository state for ``cwd``. Never raises."""
    state = GitState()

    if shutil.which("git") is None:
        state.error = "git executable not found on PATH"
        return state
    state.available = True

    root = _git(["rev-parse", "--show-toplevel"], cwd)
    if root is None:
        return state  # present, but not inside a work tree
    state.is_repo = True
    state.root = str(Path(root))

    head = _git(["rev-parse", "--short", "HEAD"], cwd)
    if head:
        state.has_commits = True
        state.head_short = head
        state.head_subject = _git(["log", "-1", "--format=%s"], cwd)

    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    if branch == "HEAD":
        state.detached = True
        state.branch = None
    else:
        state.branch = branch

    # Both of these run from the repository root, not from ``cwd``. Run from a
    # subdirectory, `ls-files` and `status` report only that subtree -- which
    # reads as an almost-empty repository and is simply false.
    repo_root = Path(state.root)

    tracked = _git(["ls-files"], repo_root)
    state.tracked_files = len([ln for ln in tracked.splitlines() if ln]) if tracked else 0

    status = _git(["status", "--porcelain"], repo_root)
    if status:
        for line in status.splitlines():
            if not line:
                continue
            code = line[:2]
            if code == "??":
                state.untracked += 1
                continue
            if code[0] not in (" ", "?"):
                state.staged += 1
            if code[1] not in (" ", "?"):
                state.modified += 1

    remote = _git(["remote", "get-url", "origin"], cwd)
    state.remote = remote or None
    return state


# --- System -------------------------------------------------------------------


def _os_label() -> str:
    """A display string for the operating system, precise per platform."""
    system = platform.system()
    if system == "Windows":
        release, version, _csd, _ptype = platform.win32_ver()
        # Windows 11 reports a 10.x kernel; the build number is the real tell.
        build = 0
        try:
            build = int(version.split(".")[-1])
        except (ValueError, IndexError):
            pass
        if release == "10" and build >= 22000:
            release = "11"
        return f"Windows {release} ({version})" if version else f"Windows {release}"
    if system == "Darwin":
        mac, _, _ = platform.mac_ver()
        return f"macOS {mac}" if mac else "macOS"
    if system == "Linux":
        pretty = _linux_pretty_name()
        return pretty or f"Linux {platform.release()}"
    return f"{system} {platform.release()}".strip() or "unknown"


def _linux_pretty_name() -> Optional[str]:
    try:
        with open("/etc/os-release", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        return None
    return None


def _shell_label() -> str:
    """Identify the shell hosting this process, as far as it can be known."""
    msystem = os.environ.get("MSYSTEM")
    if msystem:
        return f"{msystem} (Git Bash)"
    shell = os.environ.get("SHELL")
    if shell:
        return Path(shell).name
    if os.name == "nt":
        if os.environ.get("PSModulePath"):
            return "PowerShell"
        comspec = os.environ.get("COMSPEC")
        if comspec:
            return Path(comspec).name
    return "unknown"


@dataclass
class SystemState:
    """A single verified snapshot of the local environment."""

    os_label: str
    arch: str
    hostname: str
    user: str
    python_version: str
    python_executable: str
    shell: str
    cwd: str
    home: str
    terminal_width: int
    is_tty: bool
    color: bool
    encoding: str
    state_dir: str
    state_dir_exists: bool
    git: GitState
    captured_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def display_cwd(self, max_width: int = 44) -> str:
        """Working directory, shortened with ``~`` and an elided middle."""
        path = self.cwd
        home = self.home
        if home and path.lower().startswith(home.lower()):
            path = "~" + path[len(home):]
        path = path.replace("\\", "/")
        if len(path) <= max_width:
            return path
        parts = path.split("/")
        if len(parts) <= 3:
            return "…" + path[-(max_width - 1):]
        head, tail = parts[0], parts[-2:]
        return "/".join([head, "…", *tail])


def collect(cwd: Optional[Path] = None, state_dir: Optional[Path] = None) -> SystemState:
    """Gather the full local state snapshot. Never raises."""
    from ..ui import ansi  # local import keeps core free of a UI import cycle

    cwd = Path(cwd) if cwd else Path.cwd()
    state_dir = Path(state_dir) if state_dir else cwd / ".shadow"

    try:
        columns = shutil.get_terminal_size(fallback=(80, 24)).columns
    except Exception:
        columns = 80

    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"

    return SystemState(
        os_label=_os_label(),
        arch=platform.machine() or "unknown",
        hostname=platform.node() or "unknown",
        user=user,
        python_version=platform.python_version(),
        python_executable=sys.executable or "unknown",
        shell=_shell_label(),
        cwd=str(cwd),
        home=str(Path.home()),
        terminal_width=columns,
        is_tty=bool(getattr(sys.stdout, "isatty", lambda: False)()),
        color=ansi.enabled(),
        encoding=(getattr(sys.stdout, "encoding", None) or "unknown"),
        state_dir=str(state_dir),
        state_dir_exists=state_dir.is_dir(),
        git=collect_git(cwd),
    )
