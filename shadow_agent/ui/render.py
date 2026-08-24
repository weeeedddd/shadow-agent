"""Width-safe terminal rendering primitives.

The alignment contract
----------------------
Every frame this module produces closes. A border never drifts, a column never
splits, and a coloured cell occupies exactly as many terminal columns as an
uncoloured one. That guarantee rests on one rule enforced throughout:

    *Never measure a string with ``len()``.*

``len()`` counts code points. A terminal counts columns. The two disagree
whenever ANSI escapes, zero-width combining marks, or East Asian wide
characters are present -- and every one of those shows up in real output.
:func:`visible_width` is the only measurement any renderer is allowed to use.
"""

from __future__ import annotations

import re
import shutil
import unicodedata
from typing import Iterable, List, Optional, Sequence, Tuple

from . import ansi
from .theme import ASH, BONE, DIM, INDIGO, VIOLET, glyphs

# The full CSI escape grammar, not merely the colour subset.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")

_ZERO_WIDTH = ("​", "‌", "‍", "﻿")

MIN_WIDTH = 56
MAX_WIDTH = 88
DEFAULT_WIDTH = 74


def strip_ansi(text: str) -> str:
    """Return ``text`` with every escape sequence removed."""
    return _ANSI_RE.sub("", text)


def char_width(ch: str) -> int:
    """Column cost of a single character."""
    if unicodedata.combining(ch) or ch in _ZERO_WIDTH:
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def visible_width(text: str) -> int:
    """Number of terminal columns ``text`` occupies when printed."""
    return sum(char_width(ch) for ch in strip_ansi(text))


def truncate(text: str, width: int, ellipsis: Optional[str] = None) -> str:
    """Cut ``text`` to at most ``width`` visible columns.

    Only plain text should be truncated; colour is applied afterwards. That
    ordering matters -- slicing a string that already carries escapes can
    orphan a colour code and bleed it across the remainder of the line.
    """
    if width <= 0:
        return ""
    if visible_width(text) <= width:
        return text
    if ellipsis is None:
        ellipsis = glyphs().ellipsis
    if visible_width(ellipsis) > width:
        ellipsis = ""
    budget = width - visible_width(ellipsis)
    out: List[str] = []
    used = 0
    for ch in text:
        w = char_width(ch)
        if used + w > budget:
            break
        out.append(ch)
        used += w
    return "".join(out) + ellipsis


def pad(text: str, width: int, align: str = "left", fill: str = " ") -> str:
    """Pad ``text`` to exactly ``width`` visible columns.

    Over-long input is truncated rather than permitted to break the frame.
    """
    current = visible_width(text)
    if current > width:
        return truncate(text, width)
    slack = width - current
    if align == "right":
        return fill * slack + text
    if align == "center":
        left = slack // 2
        return fill * left + text + fill * (slack - left)
    return text + fill * slack


def resolve_width(requested: Optional[int] = None) -> int:
    """Pick a frame width that fits the current terminal."""
    if requested:
        return max(MIN_WIDTH, min(MAX_WIDTH, requested))
    try:
        columns = shutil.get_terminal_size(fallback=(DEFAULT_WIDTH + 2, 24)).columns
    except Exception:
        columns = DEFAULT_WIDTH + 2
    return max(MIN_WIDTH, min(MAX_WIDTH, columns - 2))


# --- Frames -------------------------------------------------------------------


def rule(width: int, char: Optional[str] = None, color: str = DIM) -> str:
    """A horizontal rule of exactly ``width`` columns."""
    char = char or glyphs().h
    return ansi.paint(char * width, color)


def panel(
    lines: Sequence[str],
    width: Optional[int] = None,
    title: Optional[str] = None,
    color: str = INDIGO,
    heavy: bool = False,
    pad_x: int = 2,
) -> List[str]:
    """Draw a titled box around ``lines``.

    ``lines`` may already contain colour; every width is measured with
    :func:`visible_width`, so styled content aligns identically to plain text.
    """
    g = glyphs()
    width = resolve_width(width)
    h, v = (g.h_heavy, g.v_heavy) if heavy else (g.h, g.v)
    tl, tr = (g.tl_heavy, g.tr_heavy) if heavy else (g.tl, g.tr)
    bl, br = (g.bl_heavy, g.br_heavy) if heavy else (g.bl, g.br)

    inner = width - 2
    content_width = inner - (pad_x * 2)
    out: List[str] = []

    if title:
        head = h + " " + truncate(title, max(0, content_width - 4)) + " "
        head = head + h * max(0, inner - visible_width(head))
        out.append(ansi.paint(tl + head + tr, color))
    else:
        out.append(ansi.paint(tl + h * inner + tr, color))

    gutter = " " * pad_x
    for line in lines:
        body = gutter + pad(line, content_width) + gutter
        out.append(ansi.paint(v, color) + body + ansi.paint(v, color))

    out.append(ansi.paint(bl + h * inner + br, color))
    return out


def kv_rows(
    pairs: Sequence[Tuple[str, str]],
    width: int,
    key_color: str = ASH,
    value_color: str = BONE,
    leader_color: str = DIM,
    min_leader: int = 3,
) -> List[str]:
    """Render ``key ..... value`` rows that all terminate at the same column.

    The leader absorbs every difference in key and value length. That is what
    makes a state table read as one block instead of a ragged list.
    """
    g = glyphs()
    rows: List[str] = []
    for key, value in pairs:
        key = str(key)
        value = str(value)
        # Budget: key + space + leader + space + value
        max_value = width - visible_width(key) - min_leader - 2
        if max_value < 4:
            rows.append(ansi.paint(truncate(key, width), key_color))
            continue
        value = truncate(value, max_value)
        dots = max(min_leader, width - visible_width(key) - visible_width(value) - 2)
        rows.append(
            ansi.paint(key, key_color)
            + " "
            + ansi.paint(g.leader * dots, leader_color)
            + " "
            + ansi.paint(value, value_color)
        )
    return rows


def wrap(text: str, width: int) -> List[str]:
    """Greedy word wrap on visible width. Over-long words are hard-split."""
    if width <= 0:
        return [text]
    words = text.split()
    if not words:
        return [""]
    lines: List[str] = []
    current = ""
    for word in words:
        candidate = word if not current else current + " " + word
        if visible_width(candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        while visible_width(word) > width:
            head = truncate(word, width, ellipsis="")
            lines.append(head)
            word = word[len(head):]
        current = word
    if current:
        lines.append(current)
    return lines


def bullets(
    items: Iterable[str],
    width: int,
    color: str = BONE,
    marker_color: str = VIOLET,
) -> List[str]:
    """Bullet list with a hanging indent that survives wrapping."""
    g = glyphs()
    out: List[str] = []
    marker = g.bullet + " "
    indent = " " * visible_width(marker)
    body_width = width - visible_width(marker)
    for item in items:
        for i, segment in enumerate(wrap(item, body_width)):
            prefix = ansi.paint(marker, marker_color) if i == 0 else indent
            out.append(prefix + ansi.paint(segment, color))
    return out


def meter(value: int, total: int, cells: int = 10, color: str = VIOLET) -> str:
    """A progress meter that is always exactly ``cells`` columns wide."""
    g = glyphs()
    total = max(1, total)
    filled = max(0, min(cells, round(cells * value / total)))
    return ansi.paint(g.bar_full * filled, color) + ansi.paint(g.bar_empty * (cells - filled), DIM)
