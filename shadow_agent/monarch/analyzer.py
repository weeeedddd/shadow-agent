"""The Monarch -- analysis and directive drafting.

The Monarch runs before anything is executed. It does three things:

1. **Scans the ground.** Inventories the working directory so a plan is drawn
   against the project that actually exists, not an imagined one.
2. **Reads the intent.** Classifies what kind of work the request implies.
3. **Drafts the directive.** Rewrites a raw request into a structured object
   the Eminence can act on, with the risk level attached.

Honest scope: the classifier in this module is deterministic and lexical. It
recognises the shape of a request, not its meaning. The LLM-backed rewrite --
where the request is genuinely reformulated rather than categorised -- attaches
at :meth:`Monarch.draft` through the reasoning core, and is not wired in this
build. The heuristic path is what runs today, and it is honest about being a
heuristic: `Directive.confidence` reports how sure it is.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from ..config import Config
from ..core.context import SystemState

# Directories that are never worth scanning: build output, vendored code,
# virtual environments, and version-control internals.
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".shadow", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", "venv", ".venv", "env", ".env", "dist", "build",
    "target", ".idea", ".vscode", ".gradle", ".next", ".nuxt", "vendor", "coverage",
}

SIGNAL_FILES = {
    "pyproject.toml": "python",
    "setup.py": "python",
    "requirements.txt": "python",
    "package.json": "node",
    "tsconfig.json": "typescript",
    "go.mod": "go",
    "Cargo.toml": "rust",
    "pom.xml": "java",
    "build.gradle": "java",
    "Gemfile": "ruby",
    "composer.json": "php",
    "Dockerfile": "docker",
    "docker-compose.yml": "docker",
    "Makefile": "make",
    "CMakeLists.txt": "cmake",
}


class Intent(Enum):
    """The shape of a request, as far as lexical analysis can tell."""

    INSPECT = "inspect"        # read, list, explain, show
    BUILD = "build"            # create, add, implement, scaffold
    MODIFY = "modify"          # edit, refactor, rename, fix
    DESTROY = "destroy"        # delete, remove, drop, reset
    EXECUTE = "execute"        # run, test, install, deploy
    UNKNOWN = "unknown"


class Risk(Enum):
    """How much damage a directive could do if it is wrong."""

    READ_ONLY = "read-only"
    REVERSIBLE = "reversible"
    DESTRUCTIVE = "destructive"


INTENT_KEYWORDS: Dict[Intent, Sequence[str]] = {
    Intent.DESTROY: ("delete", "remove", "drop", "purge", "wipe", "reset", "uninstall", "revert"),
    Intent.EXECUTE: ("run", "execute", "test", "install", "build", "deploy", "start", "launch", "compile"),
    Intent.MODIFY: ("edit", "change", "refactor", "rename", "fix", "update", "patch", "migrate", "rewrite"),
    Intent.BUILD: ("create", "add", "write", "implement", "scaffold", "generate", "make", "new", "set up"),
    Intent.INSPECT: ("show", "list", "read", "explain", "what", "where", "why", "how", "find", "check", "review"),
}


@dataclass
class Scan:
    """A bounded inventory of the working directory."""

    root: str
    files: int = 0
    directories: int = 0
    truncated: bool = False
    languages: Dict[str, int] = field(default_factory=dict)
    signals: List[str] = field(default_factory=list)
    top_level: List[str] = field(default_factory=list)

    @property
    def primary_language(self) -> Optional[str]:
        if not self.languages:
            return None
        return max(self.languages.items(), key=lambda kv: kv[1])[0]

    def summary(self) -> str:
        if self.files == 0:
            return "empty working directory"
        parts = [f"{self.files} files", f"{self.directories} directories"]
        if self.primary_language:
            parts.append(f"primarily {self.primary_language}")
        if self.truncated:
            parts.append("scan truncated")
        return ", ".join(parts)


@dataclass
class Directive:
    """The rewritten, structured form of a raw request."""

    raw: str
    objective: str
    intent: Intent
    risk: Risk
    confidence: float                       # 0.0-1.0; heuristic path caps at 0.6
    steps: List[str] = field(default_factory=list)
    context_paths: List[str] = field(default_factory=list)
    open_questions: List[str] = field(default_factory=list)
    scan: Optional[Scan] = None
    source: str = "heuristic"               # "heuristic" | "reasoning-core"

    @property
    def needs_confirmation(self) -> bool:
        return self.risk is Risk.DESTRUCTIVE


class Monarch:
    """Pre-processing: scan, classify, draft."""

    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or Config()

    # --- scanning ------------------------------------------------------------

    def scan(self, root: Path, max_depth: Optional[int] = None, limit: Optional[int] = None) -> Scan:
        """Walk ``root`` to a bounded depth and file count.

        Both bounds matter. An unbounded walk of a home directory or a
        monorepo is the difference between a responsive tool and a hung one.
        """
        root = Path(root)
        max_depth = self.config.context_scan_depth if max_depth is None else max_depth
        limit = self.config.context_scan_limit if limit is None else limit

        result = Scan(root=str(root))
        if not root.is_dir():
            return result

        try:
            result.top_level = sorted(
                p.name + ("/" if p.is_dir() else "")
                for p in root.iterdir()
                if not p.name.startswith(".") or p.name in (".gitignore", ".shadow")
            )[:24]
        except OSError:
            pass

        root_depth = len(root.resolve().parts)
        for current, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            depth = len(Path(current).resolve().parts) - root_depth
            if depth >= max_depth:
                dirnames[:] = []
            result.directories += len(dirnames)

            for name in filenames:
                if result.files >= limit:
                    result.truncated = True
                    return result
                result.files += 1
                if name in SIGNAL_FILES:
                    marker = SIGNAL_FILES[name]
                    if marker not in result.signals:
                        result.signals.append(marker)
                suffix = Path(name).suffix.lower()
                if suffix:
                    result.languages[suffix] = result.languages.get(suffix, 0) + 1
        return result

    # --- classification ------------------------------------------------------

    @staticmethod
    def classify(text: str) -> Intent:
        """Lexical intent classification.

        Checked destructive-first: a request that mentions both "delete" and
        "run" is treated as the more dangerous of the two.
        """
        lowered = f" {text.lower()} "
        for intent in (Intent.DESTROY, Intent.EXECUTE, Intent.MODIFY, Intent.BUILD, Intent.INSPECT):
            for keyword in INTENT_KEYWORDS[intent]:
                if f" {keyword}" in lowered:
                    return intent
        return Intent.UNKNOWN

    @staticmethod
    def assess_risk(intent: Intent) -> Risk:
        if intent is Intent.DESTROY:
            return Risk.DESTRUCTIVE
        if intent in (Intent.INSPECT, Intent.UNKNOWN):
            return Risk.READ_ONLY
        return Risk.REVERSIBLE

    # --- drafting ------------------------------------------------------------

    def draft(self, raw: str, state: SystemState, root: Optional[Path] = None) -> Directive:
        """Turn a raw request into a structured directive.

        This is the heuristic path. When the reasoning core is attached, the
        objective and steps are produced by the model and ``source`` becomes
        ``"reasoning-core"``; the scan and risk assessment below still run and
        still constrain what the model is allowed to plan.
        """
        root = Path(root) if root else Path(state.cwd)
        scan = self.scan(root)
        intent = self.classify(raw)
        risk = self.assess_risk(intent)

        objective = raw.strip() or "(no request given)"
        if objective and not objective.endswith((".", "?", "!")):
            objective += "."

        steps: List[str] = []
        questions: List[str] = []

        if intent is Intent.UNKNOWN:
            questions.append(
                "Intent could not be classified lexically; the reasoning core is "
                "required to interpret this request."
            )
        if scan.files == 0:
            questions.append("Working directory is empty -- there is no code to act against.")
        if risk is Risk.DESTRUCTIVE:
            steps.append("Snapshot every affected path before mutation (Architect).")
        steps.append(f"Resolve context against {scan.summary()}.")
        steps.append("Execute through the Eminence with guardrails active.")
        steps.append("Journal the outcome and emit the Implementory (Architect).")

        confidence = 0.6 if intent is not Intent.UNKNOWN else 0.2

        return Directive(
            raw=raw,
            objective=objective,
            intent=intent,
            risk=risk,
            confidence=confidence,
            steps=steps,
            context_paths=scan.top_level[:12],
            open_questions=questions,
            scan=scan,
            source="heuristic",
        )
