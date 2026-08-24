#!/usr/bin/env python3
"""The Arena -- a live test of the CORAL permission wall.

    python arena.py

Every other test in this repository proves the wall's logic with an injected
prompt function. This one puts a real destructive command in front of a real
person and waits.

What it does, precisely
-----------------------
1. Creates a **throwaway directory** under the system temp path and fills it
   with three files. Nothing outside that directory is touched.
2. Sends a recursive delete of it through the full pipeline: policy
   classification, then the permission wall.
3. Waits for you to type ``y`` or ``n``.
4. Reports what actually happened on disk, and cleans up after itself either
   way.

**Answering `y` really does delete the directory.** That is the point -- a
rehearsal where the destructive step is faked proves nothing about the step
that matters. The directory is one this script created seconds earlier, in
temp, containing three files with no value.

There is a second act. After the first decision, the same command is re-sent
in a slightly different spelling -- a trailing slash, doubled spaces, reversed
flags -- to show whether the canonicalising cache recognises it as the same
approval you already gave.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shadow_agent.eminence.coral import (
    Decision,
    PermissionWall,
    action_for_command,
    canonicalize,
)
from shadow_agent.eminence.executor import Eminence, default_shell
from shadow_agent.ui import ansi
from shadow_agent.ui.render import panel, resolve_width, rule, wrap
from shadow_agent.ui.theme import ASH, BLOOD, BOLD, BONE, DIM, EMBER, INDIGO, JADE, VIOLET, glyphs


def emit(text: str = "") -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(encoding, "replace").decode(encoding, "replace"))


def use_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass


def build_target() -> Path:
    """A throwaway directory with real files in it."""
    root = Path(tempfile.mkdtemp(prefix="shadow_arena_"))
    target = root / "test_dir"
    target.mkdir()
    for name, body in (
        ("notes.txt", "these files exist only for this test\n"),
        ("data.json", '{"disposable": true}\n'),
        ("script.sh", "#!/bin/sh\necho nothing important\n"),
    ):
        (target / name).write_text(body, encoding="utf-8")
    return target


def delete_command(target: Path) -> str:
    """A recursive delete phrased for whatever shell will actually run it."""
    shell = default_shell()
    program = Path(shell[0]).name.lower()
    if "cmd" in program:
        return f'rmdir /s /q "{target}"'
    return f'rm -rf "{target}"'


def variant_command(command: str) -> str:
    """The same command, spelled differently, to exercise the cache."""
    if command.startswith("rm -rf"):
        return command.replace("rm -rf", "rm  -fr", 1).replace('"', "").rstrip("/") + "/"
    return "  ".join(command.split())


def header(width: int) -> None:
    g = glyphs()
    emit()
    for line in panel(
        [
            ansi.paint("T H E   A R E N A", BOLD + VIOLET),
            "",
            ansi.paint("The wall has never met a human. It is about to.", DIM),
            "",
            ansi.paint("A throwaway directory has been created in your system temp", BONE),
            ansi.paint("path. A recursive delete of it is about to be put in front", BONE),
            ansi.paint("of you. Answering 'y' will really delete it — that is the", BONE),
            ansi.paint("point. Nothing outside that directory is reachable from here.", BONE),
        ],
        width=width,
        title="LIVE PERMISSION TEST",
        color=INDIGO,
    ):
        emit(line)
    emit()


def show_target(target: Path, width: int) -> None:
    files = sorted(p.name for p in target.iterdir()) if target.is_dir() else []
    for line in panel(
        [
            ansi.paint("TARGET", ASH),
            "  " + ansi.paint(str(target), BONE),
            "",
            ansi.paint("CONTENTS", ASH),
            *["  " + ansi.paint(name, DIM) for name in files],
        ],
        width=width,
        title="BEFORE",
        color=INDIGO,
    ):
        emit(line)


def main() -> int:
    use_utf8()
    width = resolve_width(None)

    if not PermissionWall.interactive():
        emit()
        for line in panel(
            [
                ansi.paint("This test needs a real terminal.", BOLD + EMBER),
                "",
                ansi.paint("stdin is not a TTY, so the wall would resolve to its", DIM),
                ansi.paint("headless policy instead of asking you anything — which", DIM),
                ansi.paint("is correct behaviour, and useless as a rehearsal.", DIM),
                "",
                ansi.paint("Run it directly:", BONE),
                ansi.paint("  python arena.py", VIOLET),
            ],
            width=width,
            title="NO TERMINAL",
            color=EMBER,
        ):
            emit(line)
        return 2

    header(width)
    target = build_target()
    show_target(target, width)

    command = delete_command(target)
    wall = PermissionWall(width=width)
    eminence = Eminence(root=target.parent)

    # --- act one: the decision ------------------------------------------------
    action = action_for_command(command, cwd=str(target.parent))
    emit()
    emit(ansi.paint(f"  policy verdict: {action.verdict.value.upper()}  ({action.reason})", DIM))

    decision = wall.request(action)

    deleted = False
    if decision.allows:
        result = eminence.run(command, allow_destructive=True)
        deleted = not target.exists()
        emit()
        emit(
            ansi.paint("  executed  ", DIM)
            + ansi.paint(f"exit {result.returncode}", JADE if result.ok else BLOOD)
            + ansi.paint(f"  ·  {result.duration:.3f}s", DIM)
        )
    else:
        emit()
        emit(ansi.paint("  not executed — the directory is untouched.", DIM))

    # --- act two: the cache ---------------------------------------------------
    if decision is Decision.ALWAYS:
        variant = variant_command(command)
        emit()
        for line in panel(
            [
                ansi.paint("You answered 'a' — always. Now the same command, respelled:", BONE),
                "",
                "  " + ansi.paint(command, DIM),
                "  " + ansi.paint(variant, VIOLET),
                "",
                ansi.paint("canonical form of both:", ASH),
                "  " + ansi.paint(canonicalize(command, str(target.parent))[:width - 10], DIM),
                "  " + ansi.paint(canonicalize(variant, str(target.parent))[:width - 10], DIM),
            ],
            width=width,
            title="CACHE TEST",
            color=INDIGO,
        ):
            emit(line)

        second = wall.request(action_for_command(variant, cwd=str(target.parent)))
        emit()
        if second is Decision.PROCEED:
            emit(ansi.paint("  ✔ recognised as the same approval — you were not asked twice.", JADE))
        else:
            emit(ansi.paint("  ✘ not recognised; the cache treated it as a new command.", EMBER))

    # --- verdict --------------------------------------------------------------
    emit()
    emit(rule(width, color=DIM))
    emit()

    lines = [
        ansi.paint("DECISION", ASH) + "   " + ansi.paint(decision.value, BOLD + BONE),
        ansi.paint("DIRECTORY", ASH) + "  "
        + ansi.paint("deleted" if deleted else "still present", BLOOD if deleted else JADE),
        "",
    ]
    if decision.allows and deleted:
        lines.append(ansi.paint("The wall asked, you approved, and the deletion happened.", BONE))
        lines.append(ansi.paint("The human-in-the-loop path is proven end to end.", DIM))
    elif decision.allows:
        lines.append(ansi.paint("You approved, but the directory survived — the command", EMBER))
        lines.append(ansi.paint("did not do what it claimed. Worth investigating.", EMBER))
    else:
        lines.append(ansi.paint("The wall asked, you refused, and nothing ran.", BONE))
        lines.append(ansi.paint("The refusal path is proven end to end.", DIM))

    for line in panel(lines, width=width, title="AFTER", color=INDIGO):
        emit(line)

    # Clean up whatever survived. This script leaves nothing behind either way.
    shutil.rmtree(target.parent, ignore_errors=True)
    emit()
    emit(ansi.paint(f"  arena cleaned up: {target.parent}", DIM))
    emit()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        emit()
        emit("  interrupted — nothing was executed.")
        raise SystemExit(130)
