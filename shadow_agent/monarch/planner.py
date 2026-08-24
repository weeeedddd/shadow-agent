"""The Monarch's reasoning planner -- and the closed evolution loop.

This replaces ``HeuristicPlanner`` as the default when a credential is
reachable. It does three things in order, and the order is the design:

    1. **Recall.** Query the Skill Forge for procedures that already solved
       something like this request.
    2. **Reuse or reason.** A high-confidence match is *applied directly* --
       no API call, no tokens, no latency. Anything else goes to the model
       with those skills injected as context.
    3. **Degrade.** Any failure returns an empty plan and a stated reason. The
       terminal never sees a traceback, and the loop never executes a plan
       that was not actually produced.

Why reuse comes before reasoning
--------------------------------
This is what makes the framework get faster rather than merely more
knowledgeable. A forge that only ever *writes* skills is a diary. Reading them
first, and short-circuiting on a confident hit, is the difference between
recording experience and having it -- and it is the only part of the loop that
shows up as a speedup the user can feel.

The bar for a direct hit is deliberately high (see :data:`REUSE_SCORE` and
:data:`REUSE_CONFIDENCE`). A wrong skill applied without review is worse than
an API call: it executes commands the user did not ask for, and the permission
wall is then the only thing standing between a bad recall and a bad outcome.
Below the bar, skills are still injected -- as *suggestions the model may
reject*, which is the safe way to use uncertain memory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..architect.skills import Skill, SkillForge
from ..core.context import SystemState
from ..eminence.coral import ActionKind
from ..llm.client import ReasoningCore, ReasoningError
from .analyzer import Directive
from .recall import RecallEngine

# Thresholds for applying a remembered skill without asking the model.
REUSE_SCORE = 1.6          # recall score; below this the match is a guess
REUSE_CONFIDENCE = 0.75    # historical success rate of the skill itself
MAX_INJECTED_SKILLS = 4
MAX_PLAN_STEPS = 8

SYSTEM_PROMPT = """\
You are The Monarch, the analysis tier of the Shadow Agent framework.

Your single output is an execution plan: an ordered list of shell commands that
accomplish the operator's request on the machine described below. You do not
execute anything. A separate tier runs your plan, and a human approves every
destructive step before it runs.

Rules, in priority order:

1. Plan only what the request asks for. Do not add cleanup, verification, or
   "while we're here" steps the operator did not request.
2. Prefer the fewest steps that do the job. One correct command beats five.
3. Use commands appropriate to the stated OS and shell. Do not assume a POSIX
   environment on Windows or vice versa.
4. If you were given a prior skill that fits, reuse its commands rather than
   inventing new ones. Say so in the rationale.
5. If the request cannot be accomplished with shell commands, or you lack the
   information to plan it safely, return an empty steps array and explain why
   in `blocked_reason`. An empty plan is a valid, respectable answer. A
   plausible-looking wrong plan is not.
6. Never emit a command whose purpose is to disable, bypass, or evade the
   approval step. Destructive commands are allowed -- the human will be asked.
   Hiding them is not.

Respond with JSON only, matching the provided schema."""

PLAN_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "reasoning": {
            "type": "string",
            "description": "One or two sentences on the approach taken.",
        },
        "reused_skill": {
            "type": ["string", "null"],
            "description": "Name of the prior skill applied, or null.",
        },
        "steps": {
            "type": "array",
            "maxItems": MAX_PLAN_STEPS,
            "items": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["command", "rationale"],
                "additionalProperties": False,
            },
        },
        "blocked_reason": {
            "type": ["string", "null"],
            "description": "Why no plan could be produced, when steps is empty.",
        },
    },
    "required": ["reasoning", "steps"],
    "additionalProperties": False,
}


@dataclass
class PlanSource:
    """Where a plan came from, and what it cost."""

    kind: str = "none"          # "skill" | "reasoning-core" | "none"
    skill: Optional[str] = None
    reasoning: str = ""
    blocked_reason: str = ""
    injected: List[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    error: str = ""

    def summary(self) -> str:
        if self.kind == "skill":
            return f"applied remembered skill '{self.skill}' — no API call"
        if self.kind == "reasoning-core":
            note = f" · recalled {len(self.injected)} skill(s)" if self.injected else ""
            return f"{self.input_tokens}→{self.output_tokens} tokens{note}"
        return self.error or self.blocked_reason or "no plan"


class _ForgeSource:
    """Adapts the Skill Forge to the recall engine's fact source protocol."""

    def __init__(self, forge: SkillForge) -> None:
        self.forge = forge

    def facts(self, entity: str) -> Sequence[Any]:
        return self.forge.as_facts()


class ReasoningPlanner:
    """Plans with the model, and with everything the framework already learned."""

    def __init__(
        self,
        core: ReasoningCore,
        forge: Optional[SkillForge] = None,
        max_steps: int = MAX_PLAN_STEPS,
    ) -> None:
        self.core = core
        self.forge = forge
        self.max_steps = max_steps
        self.last_source = PlanSource()
        self.recall = (
            RecallEngine(_ForgeSource(forge), dense_limit=40, limit=MAX_INJECTED_SKILLS)
            if forge
            else None
        )

    # --- recall --------------------------------------------------------------

    def relevant_skills(self, request: str) -> List[tuple]:
        """Skills that look applicable, best first, as ``(skill, score)``."""
        if not (self.recall and self.forge):
            return []
        by_name = {s.name: s for s in self.forge.all()}
        out: List[tuple] = []
        for ranked in self.recall.recall(request, entity="skills"):
            skill = by_name.get(ranked.fact.key)
            if skill:
                out.append((skill, ranked.score))
        return out

    @staticmethod
    def _is_confident_match(skill: Skill, score: float) -> bool:
        """Both bars must clear: the recall matched *and* the skill works."""
        return score >= REUSE_SCORE and skill.confidence >= REUSE_CONFIDENCE and skill.uses >= 2

    # --- planning ------------------------------------------------------------

    def plan(self, directive: Directive, state: SystemState) -> List[Any]:
        """Produce steps for ``directive``. Never raises."""
        from ..loop.core import Step

        source = PlanSource()
        self.last_source = source
        request = directive.raw.strip()
        if not request:
            source.blocked_reason = "empty request"
            return []

        candidates = self.relevant_skills(request)
        source.injected = [s.name for s, _ in candidates]

        # --- reuse: the loop, closed -----------------------------------------
        if candidates:
            skill, score = candidates[0]
            if self._is_confident_match(skill, score):
                source.kind = "skill"
                source.skill = skill.name
                source.reasoning = (
                    f"This matches '{skill.name}', used {skill.uses}× at "
                    f"{skill.confidence:.0%} success. Applying it rather than "
                    "reasoning from scratch."
                )
                skill.uses += 1
                self.forge.save(skill)
                return [
                    Step(ActionKind.SHELL, command, f"from skill '{skill.name}'")
                    for command in skill.commands[: self.max_steps]
                ]

        # --- reason ----------------------------------------------------------
        try:
            reply = self.core.complete_with_retry(
                SYSTEM_PROMPT,
                [{"role": "user", "content": self._user_prompt(directive, state, candidates)}],
                schema=PLAN_SCHEMA,
                max_tokens=4000,
            )
        except ReasoningError as exc:
            source.error = str(exc)
            return []
        except Exception as exc:  # a bug here must not take down the terminal
            source.error = f"{type(exc).__name__}: {exc}"
            return []

        source.input_tokens = reply.input_tokens
        source.output_tokens = reply.output_tokens

        if reply.refused:
            source.error = "the model declined this request"
            return []

        payload = reply.json()
        if not isinstance(payload, dict):
            source.error = "the model did not return a usable plan object"
            return []

        source.kind = "reasoning-core"
        source.reasoning = str(payload.get("reasoning", ""))[:400]
        source.skill = payload.get("reused_skill") or None
        source.blocked_reason = str(payload.get("blocked_reason") or "")

        steps: List[Any] = []
        for entry in payload.get("steps") or []:
            if not isinstance(entry, dict):
                continue
            command = str(entry.get("command", "")).strip()
            if not command:
                continue
            steps.append(Step(ActionKind.SHELL, command, str(entry.get("rationale", ""))[:200]))
            if len(steps) >= self.max_steps:
                break

        if not steps and not source.blocked_reason:
            source.blocked_reason = "the model returned no steps"
        return steps

    # --- prompt --------------------------------------------------------------

    def _user_prompt(
        self,
        directive: Directive,
        state: SystemState,
        candidates: Sequence[tuple],
    ) -> str:
        """Assemble the request, the machine, and the framework's memory.

        Skills below the reuse bar are injected here rather than applied. The
        framing matters: they are offered as prior art the model may reject,
        not as instructions. Uncertain memory presented as fact is how a
        remembered mistake becomes a repeated one.
        """
        lines: List[str] = [f"REQUEST\n{directive.raw.strip()}", ""]

        lines.append("MACHINE")
        lines.append(f"  os: {state.os_label}")
        lines.append(f"  shell: {state.shell}")
        lines.append(f"  cwd: {state.cwd}")
        if state.environment:
            lines.append(f"  environment: {state.environment}")
        if state.git.is_repo:
            lines.append(f"  git: branch {state.git.branch or 'detached'}, {state.git.summary()}")
        lines.append("")

        if directive.scan:
            lines.append("WORKING DIRECTORY")
            lines.append(f"  {directive.scan.summary()}")
            if directive.scan.top_level:
                lines.append(f"  contents: {', '.join(directive.scan.top_level[:14])}")
            lines.append("")

        if candidates:
            lines.append("PRIOR SKILLS (procedures this framework has used before)")
            lines.append(
                "  Reuse one if it fits and name it in `reused_skill`. Ignore them"
                " if they do not — they are prior art, not instructions."
            )
            for skill, score in candidates[:MAX_INJECTED_SKILLS]:
                lines.append("")
                lines.append(f"  — {skill.name}  (used {skill.uses}×, {skill.confidence:.0%} success, relevance {score:.2f})")
                lines.append(f"    for: {skill.description}")
                for command in skill.commands:
                    lines.append(f"    $ {command}")
            lines.append("")

        lines.append(
            f"Produce at most {self.max_steps} steps. Return JSON only."
        )
        return "\n".join(lines)


def build_planner(
    root,
    config,
    credential=None,
    forge: Optional[SkillForge] = None,
):
    """Pick the best planner this machine can actually run.

    Falls back to the heuristic planner when the SDK is missing or no
    credential resolved -- **and says which one it chose.** Silently degrading
    to the stub would let the framework look like it was reasoning when it was
    pattern-matching six phrases.

    Returns ``(planner, description)``.
    """
    from ..loop.core import HeuristicPlanner
    from .analyzer import Monarch  # noqa: F401 -- import cycle guard

    if credential is not None and not getattr(credential, "present", False):
        return HeuristicPlanner(), "heuristic (no credential resolved)"

    core = AnthropicCoreFactory(config)
    if core is None:
        return HeuristicPlanner(), "heuristic (anthropic SDK not installed)"

    return ReasoningPlanner(core, forge), f"reasoning core ({config.llm.model})"


def AnthropicCoreFactory(config) -> Optional[Any]:
    """Construct the Anthropic core, or None when the SDK is unavailable."""
    from ..llm.client import AnthropicCore

    core = AnthropicCore(config.llm)
    return core if core.available else None
