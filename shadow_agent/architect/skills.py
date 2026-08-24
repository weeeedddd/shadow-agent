"""The Skill Forge -- abstracting solved problems into reusable skills.

*Format assimilated from* ``aiming-lab/AutoResearchClaw`` (MIT), whose skills
are `.claude/skills/<name>/SKILL.md` -- markdown with YAML frontmatter -- and
``lsdefine/GenericAgent`` (MIT), whose `memory/*.md` files are SOPs the agent
reads back as procedure. ``ANative-Lab/EvoAgentX`` was read for the
self-evolution framing; it carries a **non-standard licence**
(``NOASSERTION``), so nothing was derived from its code.

Why markdown and not a database
-------------------------------
A skill a human cannot read is a skill nobody can audit. Markdown with
frontmatter means the same file is loadable by the framework, greppable from a
shell, reviewable in a diff, and editable by hand when the forge gets something
wrong -- which it will. A pickle or a SQLite blob is none of those things.

What actually gets forged
-------------------------
Only runs that **succeeded and did real work**. Three gates, all of which must
pass:

* every executed step exited zero
* at least one step ran (a run that executed nothing solved nothing)
* the command sequence is not already a known skill

The third gate is what stops the forge from filling `.shadow/skills/` with a
hundred near-identical `git status` entries. A repeat of a known skill does not
create a new one -- it increments the existing skill's use count and reinforces
it, which is the signal that separates a lucky one-off from a real procedure.

Honest scope
------------
This is **abstraction by recording, not by generalisation.** The forge captures
the literal command sequence that worked, parameterises the paths it can
recognise, and stores the outcome. It does not infer the *principle* behind a
solution -- that needs the reasoning core, and until then a forged skill is a
replayable recipe rather than transferable understanding. It is still worth
having: a recipe that is known to have worked beats re-deriving it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..core.pathsafe import sanitize_segment

SKILLS_DIRNAME = "skills"
SKILL_FILENAME = "SKILL.md"

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Skill:
    """One learned procedure."""

    name: str
    description: str
    commands: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    intent: str = ""
    uses: int = 1
    successes: int = 1
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    origin: str = ""

    @property
    def confidence(self) -> float:
        """Success rate. A skill that keeps failing should stop being trusted."""
        return self.successes / self.uses if self.uses else 0.0

    def signature(self) -> str:
        """Identity of the procedure, independent of its name."""
        return "\x1f".join(c.strip() for c in self.commands)

    # --- serialisation -------------------------------------------------------

    def to_markdown(self) -> str:
        """Render as SKILL.md -- frontmatter plus a human-readable body."""
        frontmatter = {
            "name": self.name,
            "description": self.description,
            "intent": self.intent,
            "tags": self.tags,
            "uses": self.uses,
            "successes": self.successes,
            "confidence": round(self.confidence, 3),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "origin": self.origin,
        }
        lines = ["---", json.dumps(frontmatter, indent=2, sort_keys=True), "---", ""]
        lines.append(f"# {self.name}")
        lines.append("")
        lines.append(self.description)
        lines.append("")
        lines.append(f"Used {self.uses}× · {self.successes} succeeded · confidence {self.confidence:.0%}")
        lines.append("")
        lines.append("## Procedure")
        lines.append("")
        lines.append("```bash")
        lines.extend(self.commands)
        lines.append("```")
        lines.append("")
        if self.origin:
            lines.append("## Origin")
            lines.append("")
            lines.append(f"Forged from: {self.origin}")
            lines.append("")
        return "\n".join(lines)

    @classmethod
    def from_markdown(cls, text: str) -> Optional["Skill"]:
        """Parse a SKILL.md. Returns None rather than raising on damage.

        A hand-edited skill file is expected, and one broken file must not
        take down the whole registry load.
        """
        match = _FRONTMATTER.match(text)
        if not match:
            return None
        try:
            meta = json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
        if not isinstance(meta, dict) or not meta.get("name"):
            return None

        body = match.group(2)
        commands: List[str] = []
        inside = False
        for line in body.splitlines():
            if line.strip().startswith("```"):
                inside = not inside and "bash" in line
                continue
            if inside and line.strip():
                commands.append(line)

        return cls(
            name=str(meta["name"]),
            description=str(meta.get("description", "")),
            commands=commands,
            tags=list(meta.get("tags") or []),
            intent=str(meta.get("intent", "")),
            uses=int(meta.get("uses", 1)),
            successes=int(meta.get("successes", 1)),
            created_at=str(meta.get("created_at", "")),
            updated_at=str(meta.get("updated_at", "")),
            origin=str(meta.get("origin", "")),
        )


class SkillForge:
    """Reads, writes, and forges skills under ``.shadow/skills/``."""

    def __init__(self, state_dir: Path) -> None:
        self.dir = Path(state_dir) / SKILLS_DIRNAME

    # --- registry ------------------------------------------------------------

    def all(self) -> List[Skill]:
        """Every readable skill, newest first. Damaged files are skipped."""
        if not self.dir.is_dir():
            return []
        skills: List[Skill] = []
        for directory in sorted(self.dir.iterdir()):
            path = directory / SKILL_FILENAME
            if not path.is_file():
                continue
            try:
                skill = Skill.from_markdown(path.read_text(encoding="utf-8"))
            except OSError:
                continue
            if skill:
                skills.append(skill)
        return sorted(skills, key=lambda s: s.updated_at, reverse=True)

    def find_by_signature(self, commands: Sequence[str]) -> Optional[Skill]:
        target = "\x1f".join(c.strip() for c in commands)
        for skill in self.all():
            if skill.signature() == target:
                return skill
        return None

    def get(self, name: str) -> Optional[Skill]:
        path = self.dir / sanitize_segment(name) / SKILL_FILENAME
        if not path.is_file():
            return None
        try:
            return Skill.from_markdown(path.read_text(encoding="utf-8"))
        except OSError:
            return None

    def save(self, skill: Skill) -> Optional[Path]:
        """Write a skill durably. The directory name is path-sanitised."""
        from ..core.atomic import write_atomic

        directory = self.dir / sanitize_segment(skill.name, fallback="skill")
        try:
            directory.mkdir(parents=True, exist_ok=True)
            return write_atomic(directory / SKILL_FILENAME, skill.to_markdown())
        except OSError:
            return None

    # --- the forge -----------------------------------------------------------

    def forge(
        self,
        raw_request: str,
        commands: Sequence[str],
        intent: str = "",
        succeeded: bool = True,
    ) -> Optional[Skill]:
        """Abstract a successful run into a skill, or reinforce a known one.

        Returns the skill when one was created or reinforced, ``None`` when the
        run did not earn one.
        """
        commands = [c.strip() for c in commands if c and c.strip()]
        if not commands:
            return None

        existing = self.find_by_signature(commands)
        if existing:
            # Known procedure. Reinforce rather than duplicate -- repetition is
            # the evidence that separates a real skill from a lucky one-off.
            existing.uses += 1
            if succeeded:
                existing.successes += 1
            existing.updated_at = _utc_now()
            self.save(existing)
            return existing

        if not succeeded:
            return None  # never forge a new skill from a failure

        name = self._name_for(raw_request, commands)
        skill = Skill(
            name=name,
            description=raw_request.strip()[:200] or "learned procedure",
            commands=list(commands),
            tags=self._tags_for(commands),
            intent=intent,
            origin=f"run at {_utc_now()}",
        )
        return skill if self.save(skill) else None

    @staticmethod
    def _name_for(raw_request: str, commands: Sequence[str]) -> str:
        """A short, stable, filesystem-safe name."""
        words = [w for w in re.findall(r"[a-zA-Z0-9]+", raw_request.lower()) if len(w) > 2][:4]
        if not words:
            head = commands[0].split()[0] if commands and commands[0].split() else "skill"
            words = [re.sub(r"[^a-z0-9]", "", head.lower()) or "skill"]
        return sanitize_segment("-".join(words), fallback="skill")

    @staticmethod
    def _tags_for(commands: Sequence[str]) -> List[str]:
        """Tag by the programs involved -- what the recall engine matches on."""
        tags: List[str] = []
        for command in commands:
            parts = command.split()
            if parts:
                program = parts[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
                if program and program not in tags:
                    tags.append(program)
        return tags[:6]

    # --- recall integration --------------------------------------------------

    def as_facts(self) -> List[Any]:
        """Expose skills to the Monarch's recall engine.

        Lets a new request surface a procedure that already solved something
        like it -- which is the entire point of forging them.
        """
        from ..monarch.recall import Fact

        return [
            Fact(
                key=skill.name,
                value=f"{skill.description} :: {' && '.join(skill.commands)}",
                tags=skill.tags + ["skill"],
                entity="skills",
                updated_at=skill.updated_at,
            )
            for skill in self.all()
        ]
