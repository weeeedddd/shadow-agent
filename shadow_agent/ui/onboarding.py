"""The Shadow Garden Interface -- the onboarding sequence.

Everything printed here is either a fixed piece of typography or a value read
from the live machine. There are no placeholders, no invented statistics, and
no decorative telemetry. If a value cannot be determined, it is reported as
unknown rather than fabricated.
"""

from __future__ import annotations

from typing import List, Optional

from ..core.context import SystemState
from . import ansi
from .render import (
    kv_rows,
    meter,
    pad,
    panel,
    resolve_width,
    rule,
    truncate,
    visible_width,
    wrap,
)
from .theme import ASH, BOLD, BONE, DIM, EMBER, INDIGO, ITALIC, JADE, VIOLET, glyphs

WORDMARK = "SHADOW AGENT"
SUBTITLE = "the shadow garden"
EPIGRAPH = "Those who dwell in shadow do not announce themselves. They act."

CALL_TO_ACTION = "Provide your unpolished input. The Monarch will refine it."

AGENTS = [
    (
        "THE MONARCH",
        "Analysis",
        "Intercepts your input, scans the ground it will touch, and rewrites "
        "the request into a directive worth executing.",
    ),
    (
        "THE EMINENCE",
        "Execution",
        "Runs the commands, writes the files, drives the tools. Real work "
        "against the real filesystem.",
    ),
    (
        "THE ARCHITECT",
        "Versioning",
        "Snapshots before mutation, journals every decision, restores the "
        "world when a path proves wrong.",
    ),
]


def _spaced(text: str, gap: str = "  ") -> str:
    """Letter-space a word, widening the gap between words.

    Typographic weight without a font: the eye reads a spaced capital line as
    a mark rather than as a word.
    """
    words = text.split(" ")
    return (gap * 2).join(gap.join(word) for word in words)


def masthead(width: Optional[int] = None) -> List[str]:
    """The framed wordmark. Every line is padded to an identical width."""
    g = glyphs()
    width = resolve_width(width)
    inner = width - 4  # two border columns, two gutter columns

    body = [
        "",
        ansi.paint(g.diamond, VIOLET),
        "",
        ansi.paint(_spaced(WORDMARK), BOLD + VIOLET),
        ansi.paint(g.h * min(inner - 8, visible_width(_spaced(WORDMARK))), INDIGO),
        ansi.paint(_spaced(SUBTITLE, gap=" "), ASH),
        "",
    ]
    centered = [pad(line, inner, align="center") for line in body]
    return panel(centered, width=width, heavy=True, color=INDIGO, pad_x=1)


def epigraph(width: Optional[int] = None) -> List[str]:
    width = resolve_width(width)
    return [ansi.paint(pad(EPIGRAPH, width, align="center"), ITALIC + DIM)]


def agent_roster(width: Optional[int] = None) -> List[str]:
    """Introduce the three modules of the pipeline."""
    g = glyphs()
    width = resolve_width(width)
    # width - 2 border - 4 gutter - 3 description indent
    body_width = width - 9

    lines: List[str] = []
    for index, (name, role, description) in enumerate(AGENTS):
        if index:
            lines.append("")
        header = (
            ansi.paint(g.diamond + " ", VIOLET)
            + ansi.paint(name, BOLD + BONE)
            + ansi.paint("  " + g.dot + "  ", DIM)
            + ansi.paint(role, ASH)
        )
        lines.append(header)
        for segment in wrap(description, body_width):
            lines.append("   " + ansi.paint(segment, DIM))
    return panel(lines, width=width, title="THE THREE", color=INDIGO)


def _state_rows(state: SystemState, width: int) -> List[str]:
    """Build the verified-state table. Every value comes from the machine."""
    git = state.git

    pairs = [
        ("OS", state.os_label),
        ("ARCH", state.arch),
        ("PYTHON", f"{state.python_version}"),
        ("SHELL", state.shell),
        ("USER", f"{state.user}@{state.hostname}"),
        ("DIRECTORY", state.display_cwd(max_width=width - 16)),
    ]

    if not git.available:
        pairs.append(("GIT", "not installed"))
    elif not git.is_repo:
        pairs.append(("GIT", "no repository at this path"))
    else:
        pairs.append(("BRANCH", git.branch or (f"detached at {git.head_short}" if git.detached else "unknown")))
        if git.has_commits:
            subject = truncate(git.head_subject or "", max(10, width - 26))
            pairs.append(("HEAD", f"{git.head_short}  {subject}" if subject else str(git.head_short)))
        else:
            pairs.append(("HEAD", "no commits yet"))
        pairs.append(("TRACKED", f"{git.tracked_files} files"))
        pairs.append(("WORKING TREE", git.summary()))
        if git.remote:
            pairs.append(("REMOTE", git.remote))

    pairs.append(("STATE DIR", ".shadow/" + ("  present" if state.state_dir_exists else "  absent")))

    return kv_rows(pairs, width)


def readiness(state: SystemState) -> tuple:
    """Four concrete checks, each independently verifiable.

    Returns ``(passed, total, notes)``. A check that fails must always carry a
    note explaining what to do about it -- a bar with no explanation is worse
    than no bar.
    """
    notes: List[str] = []
    checks = [
        (True, ""),  # interpreter: we are running, so this one is proven
        (
            state.encoding.lower().replace("-", "") in ("utf8", "cp65001"),
            f"stdout encoding is {state.encoding}: box glyphs fall back to ASCII",
        ),
        (
            state.git.available and state.git.is_repo,
            "no repository here: the Architect can snapshot but cannot version",
        ),
        (
            state.state_dir_exists,
            "state directory absent: run `shadow init` to create it",
        ),
    ]
    passed = 0
    for ok, note in checks:
        if ok:
            passed += 1
        elif note:
            notes.append(note)
    return passed, len(checks), notes


def system_state(state: SystemState, width: Optional[int] = None) -> List[str]:
    """The verified state panel."""
    width = resolve_width(width)
    inner = width - 6
    lines = _state_rows(state, inner)

    passed, total, notes = readiness(state)
    status_word = "ONLINE" if passed == total else "PARTIAL"
    status_color = JADE if passed == total else EMBER

    lines.append("")
    lines.append(
        ansi.paint(pad("STATUS", 12), ASH)
        + meter(passed, total)
        + "  "
        + ansi.paint(status_word, status_color)
        + ansi.paint(f"   {passed}/{total} checks", DIM)
    )
    for note in notes:
        for segment in wrap(note, inner - 3):
            lines.append("   " + ansi.paint(segment, EMBER))

    return panel(lines, width=width, title="SYSTEM STATE", color=INDIGO)


def call_to_action(width: Optional[int] = None) -> List[str]:
    """Closing block. The instruction is fixed text and must not be reworded."""
    width = resolve_width(width)
    lines: List[str] = [
        rule(width, color=DIM),
        "",
        ansi.paint(pad(CALL_TO_ACTION, width, align="center"), BOLD + BONE),
        "",
        ansi.paint(pad("shadow run \"<your raw idea>\"", width, align="center"), VIOLET),
        "",
        rule(width, color=DIM),
        ansi.paint(pad("A R I S E .", width, align="center"), DIM),
    ]
    return lines


def render(state: SystemState, width: Optional[int] = None) -> str:
    """Compose the full onboarding sequence as a single printable string."""
    width = resolve_width(width)
    blocks: List[List[str]] = [
        masthead(width),
        [""],
        epigraph(width),
        [""],
        agent_roster(width),
        [""],
        system_state(state, width),
        [""],
        call_to_action(width),
    ]
    lines: List[str] = []
    for block in blocks:
        lines.extend(block)
    return "\n".join(lines)
