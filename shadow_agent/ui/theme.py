"""The Shadow Garden palette and glyph set.

Two hard rules govern this file:

1. Restraint. A dark-fantasy interface earns its atmosphere from negative
   space and a narrow palette, not from saturation. Six colours, no more.
2. Never fake damage. There are no "corrupted", glitched, jittered, or
   randomised rendering modes anywhere in this codebase. Every border closes,
   every column lines up, every frame is deterministic.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from . import ansi

# --- Palette -----------------------------------------------------------------
# Muted 256-colour indices. Chosen to stay legible on both true-black and
# near-black terminal backgrounds without ever reaching neon.

VIOLET = ansi.fg(97)   # the Monarch's authority -- headings, masthead
INDIGO = ansi.fg(61)   # structural borders, frames
ASH = ansi.fg(245)     # labels, secondary text
DIM = ansi.fg(240)     # leaders, rules, the quietest ink
BONE = ansi.fg(253)    # primary values, body text
EMBER = ansi.fg(179)   # warnings, unresolved variables
JADE = ansi.fg(108)    # confirmed / healthy state
BLOOD = ansi.fg(131)   # failures, dirty state

BOLD = ansi.sgr(1)
ITALIC = ansi.sgr(3)


@dataclass(frozen=True)
class Glyphs:
    """Box-drawing character set.

    ``unicode()`` is the default. ``ascii_()`` is a strict fallback for
    consoles whose encoding cannot represent box-drawing characters -- it
    preserves alignment exactly, because every substitute is one column wide.
    """

    h: str
    v: str
    tl: str
    tr: str
    bl: str
    br: str
    h_heavy: str
    v_heavy: str
    tl_heavy: str
    tr_heavy: str
    bl_heavy: str
    br_heavy: str
    diamond: str
    dot: str
    bullet: str
    arrow: str
    leader: str
    ellipsis: str
    bar_full: str
    bar_empty: str

    @staticmethod
    def unicode() -> "Glyphs":
        return Glyphs(
            h="\u2500", v="\u2502",
            tl="\u256d", tr="\u256e", bl="\u2570", br="\u256f",
            h_heavy="\u2550", v_heavy="\u2551",
            tl_heavy="\u2554", tr_heavy="\u2557",
            bl_heavy="\u255a", br_heavy="\u255d",
            diamond="\u25c6", dot="\u00b7", bullet="\u2022",
            arrow="\u2192", leader="\u00b7", ellipsis="\u2026",
            bar_full="\u25b0", bar_empty="\u25b1",
        )

    @staticmethod
    def ascii_() -> "Glyphs":
        return Glyphs(
            h="-", v="|",
            tl="+", tr="+", bl="+", br="+",
            h_heavy="=", v_heavy="|",
            tl_heavy="+", tr_heavy="+", bl_heavy="+", br_heavy="+",
            diamond="*", dot=".", bullet="-", arrow="->",
            leader=".", ellipsis="...", bar_full="#", bar_empty="-",
        )


def _encoding_supports_box() -> bool:
    """True when stdout can actually encode the box-drawing set."""
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "\u256d\u2500\u2502\u25c6\u25b0".encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def glyphs() -> Glyphs:
    """Resolve the glyph set for this terminal.

    ``SHADOW_ASCII=1`` forces the ASCII fallback. Otherwise the encoding
    decides -- silently, and without ever producing a mojibake border.
    """
    import os

    if os.environ.get("SHADOW_ASCII"):
        return Glyphs.ascii_()
    return Glyphs.unicode() if _encoding_supports_box() else Glyphs.ascii_()
