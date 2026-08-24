"""The Implementory -- the mandatory closing report.

Every execution loop ends here. Four sections, always in the same order, always
present even when a section is empty:

    STATUS            success / partial / needs input
    WHAT WAS BUILT    what actually changed on this machine
    WHAT DOESN'T WORK the honest limits -- omissions are the point of failure
    HOW IT WAS DONE   which module did what, and with which tool

The third section is the one that earns the report its keep. A run that lists
nothing under *what doesn't work* is making a claim, and that claim had better
be true.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from . import ansi
from .render import bullets, pad, panel, resolve_width, rule, wrap
from .theme import ASH, BLOOD, BOLD, BONE, DIM, EMBER, INDIGO, JADE, VIOLET, glyphs


class Status(Enum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    NEEDS_INPUT = "NEEDS INPUT"
    FAILED = "FAILED"

    @property
    def color(self) -> str:
        return {
            Status.SUCCESS: JADE,
            Status.PARTIAL: EMBER,
            Status.NEEDS_INPUT: EMBER,
            Status.FAILED: BLOOD,
        }[self]


@dataclass
class Implementory:
    """A structured execution report.

    Build it up over the course of a run, then render it once at the end.
    """

    status: Status = Status.SUCCESS
    built: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    method: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    headline: str = ""

    # --- accumulation --------------------------------------------------------

    def build(self, *items: str) -> "Implementory":
        self.built.extend(i for i in items if i)
        return self

    def limit(self, *items: str) -> "Implementory":
        self.limitations.extend(i for i in items if i)
        return self

    def how(self, *items: str) -> "Implementory":
        self.method.extend(i for i in items if i)
        return self

    def unresolved(self, *items: str) -> "Implementory":
        """Record a variable the framework could not determine.

        Recording one does not by itself downgrade the status -- a run can
        succeed while still having open questions -- but it must be printed.
        """
        self.missing.extend(i for i in items if i)
        return self

    def degrade(self, status: Status) -> "Implementory":
        """Lower the status, never raise it."""
        order = [Status.SUCCESS, Status.PARTIAL, Status.NEEDS_INPUT, Status.FAILED]
        if order.index(status) > order.index(self.status):
            self.status = status
        return self

    # --- rendering -----------------------------------------------------------

    def _section(self, title: str, items: List[str], width: int, color: str, empty: str) -> List[str]:
        g = glyphs()
        lines: List[str] = [
            ansi.paint(g.diamond + " " + title, BOLD + color),
        ]
        if items:
            lines.extend(bullets(items, width - 2, color=BONE, marker_color=color))
        else:
            for segment in wrap(empty, width - 2):
                lines.append("  " + ansi.paint(segment, DIM))
        return lines

    def render(self, width: Optional[int] = None) -> str:
        g = glyphs()
        width = resolve_width(width)
        inner = width - 6

        lines: List[str] = []

        status_line = (
            ansi.paint(pad("STATUS", 18), ASH)
            + ansi.paint(self.status.value, BOLD + self.status.color)
        )
        lines.append(status_line)
        if self.headline:
            for segment in wrap(self.headline, inner):
                lines.append(ansi.paint(segment, BONE))
        lines.append("")

        lines.extend(self._section("WHAT WAS BUILT", self.built, inner, JADE, "Nothing was created on this run."))
        lines.append("")
        lines.extend(
            self._section(
                "WHAT DOESN'T WORK",
                self.limitations,
                inner,
                EMBER,
                "No limitations recorded. Treat that as unverified, not proven.",
            )
        )

        if self.missing:
            lines.append("")
            lines.extend(
                self._section("UNRESOLVED VARIABLES", self.missing, inner, EMBER, "")
            )

        lines.append("")
        lines.extend(self._section("HOW IT WAS DONE", self.method, inner, VIOLET, "No method recorded."))

        return "\n".join(panel(lines, width=width, title="IMPLEMENTORY", color=INDIGO))
