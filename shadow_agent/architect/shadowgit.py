"""Shadow-git: an out-of-band checkpoint repository.

*Assimilated from* ``EverMind-AI/Raven`` (``raven/agent/loop/checkpoint.py``),
*extended with* ``dolthub/dolt``'s reflog semantics.

The mechanism
-------------
A second git repository whose ``--git-dir`` lives inside ``.shadow/`` while its
``--work-tree`` points at the real project. Git is perfectly happy to have two
repositories observing one directory as long as they never share a git-dir, so
this yields commit-grade history over the workspace **without ever touching the
user's own ``.git``** -- no index lock contention, no stray commits on their
branch, no entry in their reflog.

Why this beats a manifest snapshot
----------------------------------
The framework previously copied listed files into a directory. That only
protects paths someone remembered to name. Shadow-git commits `add -A`, so it
captures what actually changed -- including files mutated by a shell command
that never announced itself.

It also resolves a real conflict in this project's own layout: this codebase
sits inside an unrelated outer repository. Shadow-git is indifferent to that.
It has its own git-dir, its own HEAD, and its own excludes.

From Dolt: the reflog is the real safety net
--------------------------------------------
Dolt's insight is that a commit history is only half of recoverability. A reset
moves a ref and the commits it skipped become unreachable -- present in the
object store, invisible to ``log``. ``dolt reflog`` exposes *every state a ref
has pointed at*, which is what makes a destructive operation survivable.
:meth:`ShadowGit.reflog` is that surface here: after a restore, the state you
restored *away from* is still addressable.

Failure policy
--------------
Every git invocation is best-effort. A missing binary, a locked index, a
read-only disk -- each degrades to ``None`` and is recorded. **The checkpoint
layer must never be the thing that breaks a run.** Losing a safety net is bad;
aborting the user's work to report that you lost it is worse.

Documented limits
-----------------
* Filesystem state only. Conversation and in-memory state are not captured.
* Per-operation granularity. This is an undo stack for the working tree, not
  crash recovery.
* Changes are captured by the *next* ``add -A``; they are not attributable to
  the specific command that made them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

GIT_TIMEOUT = 45.0

# Identity baked into every invocation so checkpoints never depend on -- or
# read from -- the user's global git config.
_IDENTITY = (
    "-c", "user.name=Shadow Agent",
    "-c", "user.email=architect@shadow.local",
    "-c", "commit.gpgsign=false",
    "-c", "core.autocrlf=false",
    "-c", "gc.auto=256",
)

# Defence in depth. The work-tree's own .gitignore files are honoured
# automatically by `add -A`; this layer covers a workspace that was never
# git-init'd, or whose ignore rules miss something that must not be stored.
DEFAULT_EXCLUDES = """\
# Shadow Agent checkpoint excludes (see architect/shadowgit.py).
# Layered on top of any .gitignore in the work-tree.

# --- self: never recurse into framework state -------------------------------
.shadow/
.shadow-agent/

# --- credentials: highest-impact leak vector --------------------------------
.env
.env.*
*.pem
*.key
*.pfx
id_rsa
id_ed25519
credentials.json
.netrc
.aws/
.ssh/

# --- build output: large and re-creatable -----------------------------------
__pycache__/
*.py[cod]
build/
dist/
target/
out/
*.egg-info/
node_modules/
.next/
.nuxt/

# --- virtualenvs ------------------------------------------------------------
venv/
.venv/
env/
ENV/

# --- caches -----------------------------------------------------------------
.pytest_cache/
.mypy_cache/
.ruff_cache/
.gradle/
.turbo/

# --- os / editor noise ------------------------------------------------------
.DS_Store
Thumbs.db
desktop.ini
.idea/
.vscode/
*.swp
*.tmp

# --- logs -------------------------------------------------------------------
*.log
logs/
"""


@dataclass
class Checkpoint:
    """One commit in the shadow repository."""

    sha: str
    short: str
    label: str
    when: str

    def __str__(self) -> str:
        return f"{self.short}  {self.when}  {self.label}"


@dataclass
class ReflogEntry:
    """One state HEAD has pointed at -- reachable or not.

    ``selector`` is the addressable form (``HEAD@{3}``). It resolves even when
    the commit has dropped out of ``log``, which is the entire point.
    """

    selector: str
    sha: str
    action: str
    message: str


class ShadowGit:
    """Commit-grade checkpoints over a work-tree, out of band."""

    def __init__(self, root: Path, git_dir: Optional[Path] = None) -> None:
        self.root = Path(root).resolve()
        self.git_dir = Path(git_dir) if git_dir else self.root / ".shadow" / "checkpoints.git"
        self.last_error: Optional[str] = None

    # --- plumbing ------------------------------------------------------------

    @staticmethod
    def available() -> bool:
        return shutil.which("git") is not None

    @property
    def initialized(self) -> bool:
        return (self.git_dir / "HEAD").is_file()

    def _run(self, *args: str, timeout: float = GIT_TIMEOUT) -> Optional[str]:
        """Invoke git against the shadow git-dir. Returns stdout, or None.

        The ``--git-dir``/``--work-tree`` pair is what keeps this out of band.
        ``GIT_INDEX_FILE`` is pinned into the shadow dir too -- without it, an
        inherited index path from a parent git process would have this writing
        into someone else's index.
        """
        if not self.available():
            self.last_error = "git is not installed"
            return None

        env = dict(os.environ)
        env.pop("GIT_DIR", None)
        env.pop("GIT_WORK_TREE", None)
        env["GIT_INDEX_FILE"] = str(self.git_dir / "index")
        env["GIT_TERMINAL_PROMPT"] = "0"

        argv = [
            "git",
            f"--git-dir={self.git_dir}",
            f"--work-tree={self.root}",
            *_IDENTITY,
            *args,
        ]
        try:
            result = subprocess.run(
                argv,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None

        if result.returncode != 0:
            self.last_error = (result.stderr or result.stdout or "").strip()[:400]
            return None
        self.last_error = None
        return result.stdout.strip()

    # --- lifecycle -----------------------------------------------------------

    def initialize(self) -> bool:
        """Create the shadow repository. Idempotent."""
        if self.initialized:
            self._write_excludes()
            return True
        if not self.available():
            self.last_error = "git is not installed"
            return False

        self.git_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                ["git", "init", "--bare", "--initial-branch=checkpoints", str(self.git_dir)],
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        if result.returncode != 0:
            self.last_error = (result.stderr or "").strip()[:400]
            return False

        # `init --bare` sets core.bare=true, which refuses a work-tree.
        self._run("config", "core.bare", "false")
        self._run("config", "core.logAllRefUpdates", "true")  # the reflog, explicitly
        self._write_excludes()
        return self.initialized

    def _write_excludes(self) -> None:
        info = self.git_dir / "info"
        try:
            info.mkdir(parents=True, exist_ok=True)
            (info / "exclude").write_text(DEFAULT_EXCLUDES, encoding="utf-8")
        except OSError as exc:
            self.last_error = f"could not write excludes: {exc}"

    # --- checkpoints ---------------------------------------------------------

    def checkpoint(self, label: str = "") -> Optional[Checkpoint]:
        """Stage everything and commit. ``None`` when nothing changed or git failed."""
        if not self.initialized and not self.initialize():
            return None

        if self._run("add", "-A") is None:
            return None

        # An empty commit is noise in the log and in the reflog; skip it.
        if self._run("diff", "--cached", "--quiet") is not None:
            return None

        message = label.strip() or "checkpoint"
        if self._run("commit", "--no-verify", "-m", message) is None:
            return None

        sha = self._run("rev-parse", "HEAD")
        if not sha:
            return None
        return Checkpoint(
            sha=sha,
            short=sha[:9],
            label=message,
            when=(self._run("log", "-1", "--format=%ci") or "")[:19],
        )

    def log(self, limit: int = 20) -> List[Checkpoint]:
        """Reachable checkpoints, newest first."""
        if not self.initialized:
            return []
        raw = self._run("log", f"-{limit}", "--format=%H%x1f%h%x1f%s%x1f%ci")
        if not raw:
            return []
        out: List[Checkpoint] = []
        for line in raw.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 4:
                out.append(Checkpoint(parts[0], parts[1], parts[2], parts[3][:19]))
        return out

    def reflog(self, limit: int = 30) -> List[ReflogEntry]:
        """Every state HEAD has pointed at, reachable or not.

        Dolt's contribution. After a restore rewinds HEAD, the state you left
        is still addressable here -- ``log`` alone would have lost it.
        """
        if not self.initialized:
            return []
        raw = self._run("reflog", f"-{limit}", "--format=%gd%x1f%H%x1f%gs")
        if not raw:
            return []
        out: List[ReflogEntry] = []
        for line in raw.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 3:
                action, _, message = parts[2].partition(":")
                out.append(ReflogEntry(parts[0], parts[1], action.strip(), message.strip()))
        return out

    def diff(self, ref: str = "HEAD", stat: bool = True) -> Optional[str]:
        """Working tree against a checkpoint."""
        if not self.initialized:
            return None
        return self._run("diff", "--stat" if stat else "--patch", ref)

    def changed_since(self, ref: str = "HEAD") -> List[str]:
        """Paths that differ from ``ref`` -- the recovery prompt's raw material."""
        if not self.initialized:
            return []
        raw = self._run("diff", "--name-only", ref)
        return [line for line in (raw or "").splitlines() if line]

    def restore(self, ref: str, paths: Optional[Sequence[str]] = None) -> Optional[List[str]]:
        """Restore the work-tree from a checkpoint.

        A checkpoint is taken first, unconditionally. Restoring is itself a
        destructive act, and the state being overwritten has to be recoverable
        or this tool is just a nicer way to lose work.
        """
        if not self.initialized:
            self.last_error = "no checkpoint repository"
            return None

        self.checkpoint(f"auto: before restore of {ref}")

        affected = self.changed_since(ref)
        args = ["checkout", ref, "--"]
        args.extend(paths if paths else ["."])
        if self._run(*args) is None:
            return None
        return list(paths) if paths else affected

    def resolve(self, ref: str) -> Optional[str]:
        """Full SHA for any ref -- including a reflog selector like ``HEAD@{3}``."""
        if not self.initialized:
            return None
        return self._run("rev-parse", ref)

    def size_bytes(self) -> int:
        """On-disk footprint of the checkpoint store."""
        total = 0
        if not self.git_dir.is_dir():
            return 0
        for path in self.git_dir.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
        return total

    def gc(self) -> bool:
        """Compact the object store. Long sessions accumulate loose objects."""
        return self._run("gc", "--auto", "--quiet", timeout=180.0) is not None
