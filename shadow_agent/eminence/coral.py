"""The CORAL Permission Wall -- human-in-the-loop enforcement.

Name disambiguation -- read this before assuming provenance
-----------------------------------------------------------
This module is **not** derived from ``Human-Agent-Society/CORAL``. That
repository is real (Apache-2.0, COLM 2026) and this project does assimilate
from it -- but into :mod:`shadow_agent.eminence.failure` and
:mod:`shadow_agent.core.atomic`, not here. CORAL is a multi-agent evolutionary
autoresearch orchestrator; it has no permission wall.

The name on this file comes from this project's own directive, which used
"CORAL" for the human-in-the-loop protocol. It is kept for continuity with
that vocabulary. The protocol below is implemented from that specification.
See ``NOTICE.md`` for the full accounting.

What this adds that the policy could not
----------------------------------------
:mod:`shadow_agent.eminence.policy` classifies. It answers *what kind of thing
is this*. It cannot answer *should this particular action happen right now, in
this directory, to these files* -- and no static classifier can, because that
answer depends on intent the machine does not have.

The wall is where that question gets asked of the only party who can answer it.

Intercept everything, prompt for what matters
---------------------------------------------
Every shell command and every file write routes through :meth:`PermissionWall.
request`. That is the interception the directive requires, and it is absolute:
there is no path from the Eminence to the OS that bypasses it.

Prompting is not the same as intercepting. A wall that stops on `ls` trains
people to hold down Y, and a reflex-approved prompt protects nobody -- it is
strictly worse than no prompt, because it manufactures the *appearance* of
review. So the tier decides:

    DENY      refused outright. Never promptable, in any mode.
    APPROVE   the operator is asked, and the exact command is shown.
    ALLOW     proceeds, and is still recorded.

``paranoid`` mode escalates ALLOW to a prompt for operators who want it.

Headless behaviour
------------------
With no TTY there is nobody to ask, and a question nobody can answer is not a
safety mechanism. The wall resolves to its configured
:class:`HeadlessPolicy` -- ``DENY`` by default. Silently proceeding because
the prompt could not be displayed is the single worst thing this module could
do, so it is the one thing it will not do.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Set

from ..ui import ansi
from ..ui.render import pad, panel, resolve_width, rule, truncate, visible_width, wrap
from ..ui.theme import ASH, BLOOD, BOLD, BONE, DIM, EMBER, INDIGO, JADE, VIOLET, glyphs
from . import policy


class ActionKind(Enum):
    SHELL = "shell command"
    WRITE = "file write"
    DELETE = "file delete"
    NETWORK = "network request"


class Decision(Enum):
    PROCEED = "proceed"
    ABORT = "abort"
    ALWAYS = "always"     # this exact action, for the rest of the session
    QUIT = "quit"         # abandon the run entirely

    @property
    def allows(self) -> bool:
        return self in (Decision.PROCEED, Decision.ALWAYS)


class HeadlessPolicy(Enum):
    """What the wall does when there is no terminal to ask on."""

    DENY = "deny"      # default: refuse anything that would need approval
    ALLOW = "allow"    # explicit opt-in, for trusted automation only
    ERROR = "error"    # raise, so a pipeline fails loudly rather than skipping


@dataclass
class Action:
    """One thing the Eminence intends to do, before it happens."""

    kind: ActionKind
    target: str                      # the command, or the path
    summary: str = ""                # one line: what this does
    detail: str = ""                 # preview: the diff, the full command
    cwd: str = ""
    reason: str = ""                 # why the policy flagged it
    verdict: policy.Verdict = policy.Verdict.ALLOW

    def fingerprint(self) -> str:
        """Identity for session-scoped 'always allow'.

        Includes the working directory: the same command is not the same
        action in a different place. ``rm -rf build`` approved in a scratch
        directory must not carry into the user's home.
        """
        return f"{self.kind.value}\x1f{self.cwd}\x1f{self.target}"


class PermissionDenied(Exception):
    """The wall refused an action."""

    def __init__(self, message: str, action: Optional[Action] = None) -> None:
        super().__init__(message)
        self.action = action


@dataclass
class PermissionWall:
    """Intercepts every OS-touching action and decides whether it proceeds."""

    headless: HeadlessPolicy = HeadlessPolicy.DENY
    paranoid: bool = False
    width: Optional[int] = None
    auto_approved: Set[str] = field(default_factory=set)
    log: List[tuple] = field(default_factory=list)
    emit: Callable[[str], None] = staticmethod(lambda text: print(text))
    prompt: Callable[[str], str] = staticmethod(input)
    quit_requested: bool = False
    # Overrides the TTY probe. A caller that supplies its own approval channel
    # -- a GUI, a chat surface, a test harness -- can answer questions without
    # a terminal, and probing stdin would wrongly rule that out. None means
    # "decide by probing", which is the right default for a CLI.
    assume_interactive: Optional[bool] = None

    # --- environment ---------------------------------------------------------

    @staticmethod
    def interactive() -> bool:
        """True only when both ends are a real terminal.

        Checked at call time rather than cached at construction: a long-lived
        process can be backgrounded, and a stale 'yes, there is a terminal'
        would send a prompt into a void and wait forever.
        """
        try:
            return bool(sys.stdin.isatty() and sys.stdout.isatty())
        except (AttributeError, ValueError, OSError):
            return False

    def can_ask(self) -> bool:
        """Whether this wall has any channel to ask a human on."""
        if self.assume_interactive is not None:
            return self.assume_interactive
        return self.interactive()

    @classmethod
    def from_env(cls, **kwargs) -> "PermissionWall":
        """Build from ``SHADOW_HEADLESS`` and ``SHADOW_PARANOID``."""
        raw = (os.environ.get("SHADOW_HEADLESS") or "deny").strip().lower()
        try:
            headless = HeadlessPolicy(raw)
        except ValueError:
            headless = HeadlessPolicy.DENY
        return cls(
            headless=headless,
            paranoid=bool(os.environ.get("SHADOW_PARANOID")),
            **kwargs,
        )

    # --- the gate ------------------------------------------------------------

    def request(self, action: Action) -> Decision:
        """Decide whether ``action`` may proceed. This is the only entry point."""
        if self.quit_requested:
            self._record(action, Decision.QUIT)
            return Decision.QUIT

        # DENY is absolute. It is not promptable, not overridable by an
        # 'always' entry, and not softened by paranoid mode being off.
        if action.verdict is policy.Verdict.DENY:
            self._render_denial(action)
            self._record(action, Decision.ABORT)
            return Decision.ABORT

        if action.fingerprint() in self.auto_approved:
            self._record(action, Decision.ALWAYS)
            return Decision.PROCEED

        needs_prompt = action.verdict is policy.Verdict.APPROVE or self.paranoid
        if not needs_prompt:
            self._record(action, Decision.PROCEED)
            return Decision.PROCEED

        if not self.can_ask():
            return self._resolve_headless(action)

        return self._ask(action)

    def _resolve_headless(self, action: Action) -> Decision:
        if self.headless is HeadlessPolicy.ERROR:
            self._record(action, Decision.ABORT)
            raise PermissionDenied(
                f"{action.kind.value} requires approval and no terminal is attached: {action.target}",
                action,
            )
        decision = Decision.PROCEED if self.headless is HeadlessPolicy.ALLOW else Decision.ABORT
        self._render_headless(action, decision)
        self._record(action, decision)
        return decision

    # --- rendering -----------------------------------------------------------

    def _detail_lines(self, action: Action, inner: int) -> List[str]:
        g = glyphs()
        lines: List[str] = []

        lines.append(ansi.paint(action.kind.value.upper(), BOLD + EMBER))
        lines.append("")

        # The exact text that will run. Boxed and unwrapped -- an operator
        # approving a command has to see the command, not a summary of it.
        for segment in wrap(action.target, inner - 4):
            lines.append("  " + ansi.paint(segment, BOLD + BONE))
        lines.append("")

        if action.cwd:
            lines.append(ansi.paint(pad("in", 10), ASH) + ansi.paint(action.cwd, DIM))
        if action.reason:
            lines.append(ansi.paint(pad("flagged", 10), ASH) + ansi.paint(action.reason, EMBER))
        if action.summary:
            for i, segment in enumerate(wrap(action.summary, inner - 10)):
                label = pad("effect", 10) if i == 0 else " " * 10
                lines.append(ansi.paint(label, ASH) + ansi.paint(segment, BONE))

        if action.detail:
            lines.append("")
            lines.append(ansi.paint(rule(inner, color=DIM), DIM))
            for line in action.detail.splitlines()[:14]:
                lines.append(ansi.paint(truncate(line, inner), DIM))
            overflow = len(action.detail.splitlines()) - 14
            if overflow > 0:
                lines.append(ansi.paint(f"… {overflow} more lines", DIM))
        return lines

    def _render_denial(self, action: Action) -> None:
        width = resolve_width(self.width)
        inner = width - 6
        lines = self._detail_lines(action, inner)
        lines.append("")
        lines.append(ansi.paint("REFUSED", BOLD + BLOOD) + ansi.paint("  - not promptable", DIM))
        for segment in wrap(
            "This class of command is denied outright. There is no confirmation "
            "that unlocks it, in any mode. Narrow the command and try again.",
            inner,
        ):
            lines.append(ansi.paint(segment, DIM))
        self._emit_block(panel(lines, width=width, title="PERMISSION WALL - DENIED", color=BLOOD))

    def _render_headless(self, action: Action, decision: Decision) -> None:
        width = resolve_width(self.width)
        inner = width - 6
        lines = self._detail_lines(action, inner)
        lines.append("")
        if decision.allows:
            lines.append(ansi.paint("ALLOWED - headless policy is 'allow'", BOLD + EMBER))
            note = (
                "No terminal is attached and SHADOW_HEADLESS=allow, so this ran "
                "without review. That setting is for trusted automation only."
            )
        else:
            lines.append(ansi.paint("ABORTED - no terminal to ask on", BOLD + EMBER))
            note = (
                "This action needs approval and stdin is not a TTY. Run it from a "
                "real terminal, or set SHADOW_HEADLESS=allow if this environment "
                "is trusted."
            )
        for segment in wrap(note, inner):
            lines.append(ansi.paint(segment, DIM))
        self._emit_block(panel(lines, width=width, title="PERMISSION WALL - HEADLESS", color=EMBER))

    def _ask(self, action: Action) -> Decision:
        """Render the wall and block until the operator answers."""
        width = resolve_width(self.width)
        inner = width - 6

        lines = self._detail_lines(action, inner)
        lines.append("")
        lines.append(
            ansi.paint("[y] ", JADE) + ansi.paint("proceed once", BONE)
            + ansi.paint("     [a] ", VIOLET) + ansi.paint("always, this session", BONE)
        )
        lines.append(
            ansi.paint("[n] ", EMBER) + ansi.paint("abort this action", BONE)
            + ansi.paint("   [q] ", BLOOD) + ansi.paint("abandon the run", BONE)
        )

        self._emit_block(panel(lines, width=width, title="PERMISSION WALL", color=EMBER))

        while True:
            try:
                answer = self.prompt("  proceed? [y/N/a/q] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                # stdin closed or Ctrl-C mid-prompt. Refuse; never assume yes.
                self.emit("")
                self.emit(ansi.paint("  interrupted — action aborted.", EMBER))
                self._record(action, Decision.ABORT)
                return Decision.ABORT

            # Default is abort. A bare Enter must never mean yes.
            if answer in ("", "n", "no"):
                self.emit(ansi.paint("  aborted.", DIM))
                self._record(action, Decision.ABORT)
                return Decision.ABORT
            if answer in ("y", "yes"):
                self._record(action, Decision.PROCEED)
                return Decision.PROCEED
            if answer in ("a", "always"):
                self.auto_approved.add(action.fingerprint())
                self.emit(
                    ansi.paint("  approved for this session ", DIM)
                    + ansi.paint("(this exact command, in this directory only)", DIM)
                )
                self._record(action, Decision.ALWAYS)
                return Decision.ALWAYS
            if answer in ("q", "quit"):
                self.quit_requested = True
                self.emit(ansi.paint("  run abandoned.", EMBER))
                self._record(action, Decision.QUIT)
                return Decision.QUIT

            self.emit(ansi.paint("  answer y, n, a, or q.", DIM))

    # --- bookkeeping ---------------------------------------------------------

    def _emit_block(self, lines: List[str]) -> None:
        self.emit("")
        for line in lines:
            self.emit(line)

    def _record(self, action: Action, decision: Decision) -> None:
        self.log.append((action.kind.value, action.target, action.verdict.value, decision.value))

    def summary(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for _, _, _, decision in self.log:
            counts[decision] = counts.get(decision, 0) + 1
        return counts


def action_for_command(command: str, cwd: str = "") -> Action:
    """Build an :class:`Action` for a shell command, classified by the policy."""
    judgement = policy.classify(command)
    return Action(
        kind=ActionKind.SHELL,
        target=command,
        cwd=cwd,
        reason=judgement.reason if judgement.blocked else "",
        verdict=judgement.verdict,
        summary=f"runs `{judgement.program}`" if judgement.program else "",
    )


def action_for_write(path: str, content: str, existing: Optional[str] = None) -> Action:
    """Build an :class:`Action` for a file write, with a real diff preview.

    Overwriting an existing file is a different act from creating a new one,
    and the wall shows which -- with the line delta, so an operator can see at
    a glance whether a 900-line file is about to become nine.
    """
    from pathlib import Path

    is_overwrite = existing is not None
    new_lines = content.count("\n") + 1
    if is_overwrite:
        old_lines = existing.count("\n") + 1
        delta = new_lines - old_lines
        summary = f"overwrites {old_lines} lines with {new_lines} ({delta:+d})"
        verdict = policy.Verdict.APPROVE
    else:
        summary = f"creates a new file, {new_lines} lines"
        verdict = policy.Verdict.ALLOW

    preview = "\n".join(content.splitlines()[:12])
    return Action(
        kind=ActionKind.WRITE,
        target=str(path),
        summary=summary,
        detail=preview,
        cwd=str(Path(path).parent),
        reason="overwrites an existing file" if is_overwrite else "",
        verdict=verdict,
    )
