"""The core loop -- where the three tiers become one system.

*Structure assimilated from* ``lsdefine/GenericAgent`` (``agent_loop.py``, MIT):
the ``StepOutcome`` contract, hook points around every phase, and a hard turn
bound.

The cycle
---------
::

    raw input
        │
        ▼
    ┌─ MONARCH ──────────┐   scan · classify · (research) · plan
    │                    │   → Plan(steps[])
    └────────┬───────────┘
             │  for each step:
             ▼
    ┌─ CORAL WALL ───────┐   classify → deny / ask / allow
    └────────┬───────────┘
             │  only if permitted
             ▼
    ┌─ EMINENCE ─────────┐   execute · capture · classify failure
    └────────┬───────────┘
             │
             ▼
    ┌─ ARCHITECT ────────┐   journal · checkpoint · forge skill · gc
    └────────────────────┘

Three properties the loop guarantees
------------------------------------
**It is bounded.** ``max_steps`` caps the run and ``StreakTracker`` breaks
repetition within it. An agent that cannot stop is not autonomous, it is
runaway.

**Nothing reaches the OS un-gated.** The wall sits between plan and execution
structurally, not by convention. There is no branch that skips it.

**It is observable while it runs.** :meth:`CoreLoop.stream` is a generator that
yields an event per phase transition, so a terminal renders progress live
instead of freezing until the run ends. GenericAgent's design; the reason it is
worth copying is that a silent multi-minute agent is indistinguishable from a
hung one.

Honest scope
------------
The planner is pluggable and today's default is heuristic -- it maps a small
set of recognisable requests to commands and otherwise plans nothing. **It does
not invent shell commands from natural language; that needs the reasoning
core.** Every other part of the machinery -- gating, execution, streak-breaking,
checkpointing, skill capture, garbage collection -- is real and runs now.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterator, List, Optional, Protocol

from ..architect.shadowgit import ShadowGit
from ..architect.state import StateStore
from ..config import Config
from ..core.context import SystemState
from ..core.errors import EminenceError, ShadowError
from ..eminence.coral import Action, ActionKind, Decision, PermissionDenied, PermissionWall, action_for_command
from ..eminence.executor import Eminence, ExecutionResult
from ..eminence.failure import CircuitBreaker, ExitQuality, StreakTracker, classify_exit
from ..monarch.analyzer import Directive, Monarch

DEFAULT_MAX_STEPS = 12
GC_EVERY_N_CHECKPOINTS = 10


class Phase(Enum):
    ANALYZE = "analyze"
    RESEARCH = "research"
    PLAN = "plan"
    GATE = "gate"
    EXECUTE = "execute"
    RECORD = "record"
    FORGE = "forge"
    DONE = "done"


@dataclass
class Step:
    """One proposed action in a plan."""

    kind: ActionKind
    command: str
    rationale: str = ""
    optional: bool = False


@dataclass
class StepOutcome:
    """What a step produced, and what the loop should do next.

    From GenericAgent. The value of the shape is that a step can redirect the
    loop (``next_prompt``) or end it (``should_exit``) without the loop needing
    to know anything about what the step did.
    """

    data: Any = None
    next_prompt: Optional[str] = None
    should_exit: bool = False
    ok: bool = True


@dataclass
class LoopEvent:
    """One observable moment. Yielded live so a UI can render progress."""

    phase: Phase
    message: str
    detail: str = ""
    ok: bool = True
    step_index: int = 0


@dataclass
class RunResult:
    """The complete record of one loop run."""

    directive: Optional[Directive] = None
    steps: List[Step] = field(default_factory=list)
    outcomes: List[StepOutcome] = field(default_factory=list)
    executed: int = 0
    refused: int = 0
    failed: int = 0
    aborted: bool = False
    abort_reason: str = ""
    unproductive: int = 0   # exit 0, but instant and silent
    checkpoint: Optional[str] = None
    skill_forged: Optional[str] = None
    nudges: List[str] = field(default_factory=list)
    duration: float = 0.0

    @property
    def succeeded(self) -> bool:
        """Executed real work, and nothing failed or aborted.

        ``unproductive`` is excluded deliberately: a step that exited 0 with
        no output and no elapsed time is not evidence of success, and a run
        made entirely of those has not earned a forged skill.
        """
        return (
            not self.aborted
            and self.failed == 0
            and self.executed > 0
            and self.executed > self.unproductive
        )


class Planner(Protocol):
    """Turns a directive into steps.

    The seam GenericAgent's design argues for: the loop is identical whether
    the plan came from a keyword table or from a model.
    """

    def plan(self, directive: Directive, state: SystemState) -> List[Step]:
        ...


class HeuristicPlanner:
    """The no-LLM planner.

    Maps a deliberately small set of recognisable requests to commands. Anything
    else plans nothing and says why -- **guessing a shell command from prose
    without a reasoning core is exactly the failure mode this refuses to have.**
    """

    RECIPES = {
        "run the tests": "python -m unittest discover -s tests",
        "run tests": "python -m unittest discover -s tests",
        "git status": "git status --short --branch",
        "show the diff": "git diff --stat",
        "list files": "git ls-files",
        "install dependencies": "pip install -e .",
    }

    def plan(self, directive: Directive, state: SystemState) -> List[Step]:
        lowered = directive.raw.strip().lower()
        for phrase, command in self.RECIPES.items():
            if phrase in lowered:
                return [Step(ActionKind.SHELL, command, f"matched recipe: {phrase!r}")]
        return []


@dataclass
class Hooks:
    """Phase callbacks. Every one is optional and every one is fired.

    This is the seam for the Skill Forge, for telemetry, and for anything that
    needs to observe the loop without being wired into it.
    """

    before_phase: Optional[Callable[[Phase, Dict[str, Any]], None]] = None
    after_phase: Optional[Callable[[Phase, Dict[str, Any]], None]] = None
    before_step: Optional[Callable[[Step], None]] = None
    after_step: Optional[Callable[[Step, StepOutcome], None]] = None

    def fire(self, name: str, *args) -> None:
        callback = getattr(self, name, None)
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            # A hook must never take the run down with it.
            pass


class CoreLoop:
    """Chains Monarch → Wall → Eminence → Architect."""

    def __init__(
        self,
        root,
        config: Optional[Config] = None,
        wall: Optional[PermissionWall] = None,
        planner: Optional[Planner] = None,
        hooks: Optional[Hooks] = None,
        max_steps: int = DEFAULT_MAX_STEPS,
    ) -> None:
        self.config = config or Config()
        self.root = root
        self.monarch = Monarch(self.config)
        self.eminence = Eminence(self.config, root=root)
        self.store = StateStore(root)
        self.shadowgit = ShadowGit(root)
        self.wall = wall or PermissionWall.from_env()
        self.planner = planner or HeuristicPlanner()
        self.hooks = hooks or Hooks()
        self.streak = StreakTracker()
        self.breaker = CircuitBreaker()
        self.max_steps = max_steps

    # --- the run -------------------------------------------------------------

    def stream(self, raw: str, state: SystemState) -> Iterator[LoopEvent]:
        """Run the loop, yielding an event per phase transition.

        The result is attached as ``self.result`` when the generator finishes,
        so a caller that only wants the outcome can exhaust it and read that.
        """
        started = time.perf_counter()
        result = RunResult()
        self.result = result

        # --- 1. MONARCH ------------------------------------------------------
        self.hooks.fire("before_phase", Phase.ANALYZE, {"raw": raw})
        yield LoopEvent(Phase.ANALYZE, "The Monarch reads the request.")

        directive = self.monarch.draft(raw, state, root=self.root)
        result.directive = directive
        yield LoopEvent(
            Phase.ANALYZE,
            f"intent {directive.intent.value} · risk {directive.risk.value}",
            directive.scan.summary() if directive.scan else "",
        )
        self.hooks.fire("after_phase", Phase.ANALYZE, {"directive": directive})

        # --- 2. PLAN ---------------------------------------------------------
        self.hooks.fire("before_phase", Phase.PLAN, {"directive": directive})
        steps = self.planner.plan(directive, state)[: self.max_steps]
        result.steps = steps

        if not steps:
            yield LoopEvent(
                Phase.PLAN,
                "No executable plan.",
                "The heuristic planner recognised no runnable step. Attaching the "
                "reasoning core is what turns a directive into commands.",
                ok=False,
            )
            result.duration = time.perf_counter() - started
            yield LoopEvent(Phase.DONE, "Run complete — nothing executed.", ok=False)
            return

        yield LoopEvent(Phase.PLAN, f"{len(steps)} step(s) planned.")
        self.hooks.fire("after_phase", Phase.PLAN, {"steps": steps})

        # A checkpoint before the first mutation, not after. Snapshotting
        # afterwards captures the state you were trying to preserve, gone.
        pre = self.shadowgit.checkpoint(f"before: {directive.raw[:60]}")
        if pre:
            yield LoopEvent(Phase.RECORD, f"checkpoint {pre.short} taken before execution")

        # --- 3. STEP CYCLE ---------------------------------------------------
        for index, step in enumerate(steps, start=1):
            if self.wall.quit_requested:
                result.aborted = True
                result.abort_reason = "operator abandoned the run"
                break

            self.hooks.fire("before_step", step)

            # --- GATE: nothing reaches the OS without passing here ---
            yield LoopEvent(Phase.GATE, f"step {index}: {step.command}", step.rationale, step_index=index)
            action = action_for_command(step.command, cwd=str(self.root))

            try:
                decision = self.wall.request(action)
            except PermissionDenied as exc:
                result.aborted = True
                result.abort_reason = str(exc)
                yield LoopEvent(Phase.GATE, "aborted by the permission wall", str(exc), ok=False, step_index=index)
                break

            if decision is Decision.QUIT:
                result.aborted = True
                result.abort_reason = "operator abandoned the run"
                yield LoopEvent(Phase.GATE, "run abandoned at the wall", ok=False, step_index=index)
                break

            if not decision.allows:
                result.refused += 1
                outcome = StepOutcome(next_prompt="the operator declined this step", ok=False)
                result.outcomes.append(outcome)
                self.hooks.fire("after_step", step, outcome)
                yield LoopEvent(Phase.GATE, f"step {index} refused", step.command, ok=False, step_index=index)
                if step.optional:
                    continue
                break

            # --- EXECUTE ---
            yield LoopEvent(Phase.EXECUTE, f"running step {index}", step.command, step_index=index)
            outcome = self._execute(step)
            result.outcomes.append(outcome)

            execution: Optional[ExecutionResult] = outcome.data

            # Exit 0 is not the same as success. A command that returns
            # instantly with no output almost certainly did nothing, and
            # counting it as a win is how a run reports "1 executed, 0 failed"
            # while accomplishing nothing.
            quality = (
                classify_exit(execution.returncode, execution.duration, execution.output)
                if execution
                else ExitQuality.SESSION_ERROR
            )

            if outcome.ok:
                result.executed += 1
                if quality is ExitQuality.NO_RESULT:
                    result.unproductive += 1
                suffix = "" if quality is ExitQuality.CLEAN else "  (no output — did it do anything?)"
                yield LoopEvent(
                    Phase.EXECUTE,
                    (f"step {index} ok ({execution.duration:.2f}s)" if execution else f"step {index} ok") + suffix,
                    execution.brief() if execution else "",
                    ok=quality is ExitQuality.CLEAN,
                    step_index=index,
                )
            else:
                result.failed += 1
                yield LoopEvent(
                    Phase.EXECUTE,
                    f"step {index} failed",
                    execution.brief() if execution else outcome.next_prompt or "",
                    ok=False,
                    step_index=index,
                )

            # --- STREAK: break a repeating dead approach ---
            nudge = self.streak.record(step.command.split()[0] if step.command else "shell", execution or "")
            if nudge:
                result.nudges.append(nudge)
                yield LoopEvent(Phase.EXECUTE, "loop-break nudge", nudge, ok=False, step_index=index)

            # Circuit breaker. Only non-productive exits count -- a clean
            # exit resets it, so a run of legitimate quick completions can
            # never trip a mechanism meant to catch a crash loop.
            if self.breaker.record(quality):
                result.aborted = True
                result.abort_reason = self.breaker.reason
                yield LoopEvent(Phase.EXECUTE, "circuit breaker tripped", self.breaker.reason, ok=False, step_index=index)
                break

            self.hooks.fire("after_step", step, outcome)

            if outcome.should_exit:
                break

        # --- 4. ARCHITECT ----------------------------------------------------
        self.hooks.fire("before_phase", Phase.RECORD, {"result": result})
        yield LoopEvent(Phase.RECORD, "The Architect records the outcome.")

        after = self.shadowgit.checkpoint(f"after: {directive.raw[:60]}")
        if after:
            result.checkpoint = after.short
            yield LoopEvent(Phase.RECORD, f"checkpoint {after.short} committed")

        self.store.record(
            "loop.run",
            raw=raw,
            intent=directive.intent.value,
            executed=result.executed,
            refused=result.refused,
            failed=result.failed,
            aborted=result.aborted,
        )

        collected = self._collect_garbage()
        if collected:
            yield LoopEvent(Phase.RECORD, "garbage collection ran", collected)

        self.hooks.fire("after_phase", Phase.RECORD, {"result": result})

        result.duration = time.perf_counter() - started
        yield LoopEvent(
            Phase.DONE,
            f"{result.executed} executed · {result.refused} refused · {result.failed} failed",
            f"{result.duration:.2f}s",
            ok=result.succeeded,
        )

    def run(self, raw: str, state: SystemState) -> RunResult:
        """Run to completion without streaming. Returns the result."""
        for _ in self.stream(raw, state):
            pass
        return self.result

    # --- internals -----------------------------------------------------------

    def _execute(self, step: Step) -> StepOutcome:
        """Execute one step.

        ``allow_destructive=True`` is correct here and only here: the wall has
        already made the decision, and re-asking the executor's own gate would
        double-prompt for something the operator just approved.
        """
        try:
            execution = self.eminence.run(step.command, allow_destructive=True)
        except EminenceError as exc:
            return StepOutcome(data=None, next_prompt=str(exc), ok=False)

        return StepOutcome(
            data=execution,
            next_prompt=None if execution.ok else execution.brief(),
            ok=execution.ok,
        )

    def _collect_garbage(self) -> str:
        """Keep the checkpoint store from growing without bound.

        Called every run, but only *acts* periodically -- `git gc` on every
        single run would spend more time compacting than working.
        """
        checkpoints = self.shadowgit.log(limit=1000)
        if len(checkpoints) % GC_EVERY_N_CHECKPOINTS != 0 or not checkpoints:
            return ""
        before = self.shadowgit.size_bytes()
        if not self.shadowgit.gc():
            return ""
        after = self.shadowgit.size_bytes()
        saved = max(0, before - after)
        self.store.record("architect.gc", before=before, after=after, saved=saved)
        return f"{len(checkpoints)} checkpoints · reclaimed {saved // 1024} KiB"
