"""ANSI plumbing: capability detection, colour enablement, escape helpers.

Nothing in the Shadow Agent interface writes an escape sequence directly.
Everything routes through this module so that a single ``NO_COLOR=1``, a
redirected stdout, or a console that never learned about virtual terminals
degrades the whole UI to clean monochrome text -- without disturbing a single
column of alignment.
"""

from __future__ import annotations

import os
import sys
from typing import Optional, TextIO

CSI = "\x1b["
RESET = f"{CSI}0m"

_enabled: Optional[bool] = None


def _enable_windows_vt() -> bool:
    """Switch the Windows console into virtual-terminal mode.

    Returns True when the console understands ANSI sequences afterwards.
    On non-Windows platforms this is a no-op that reports success.
    """
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        if handle in (0, -1, None):
            return False
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        enable_vt = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if mode.value & enable_vt:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | enable_vt))
    except Exception:
        return False


def detect_support(stream: Optional[TextIO] = None) -> bool:
    """Decide whether colour is safe to emit on ``stream``."""
    stream = stream if stream is not None else sys.stdout

    if os.environ.get("SHADOW_NO_COLOR"):
        return False
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR"):
        _enable_windows_vt()
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    return _enable_windows_vt()


def enabled() -> bool:
    """Memoised colour capability for the current process."""
    global _enabled
    if _enabled is None:
        _enabled = detect_support()
    return _enabled


def set_enabled(value: Optional[bool]) -> None:
    """Force colour on/off, or pass None to re-detect on next use."""
    global _enabled
    _enabled = value


def sgr(*codes: int) -> str:
    """Build a Select Graphic Rendition sequence, or nothing if colour is off."""
    if not codes or not enabled():
        return ""
    return f"{CSI}{';'.join(str(c) for c in codes)}m"


def fg(index: int) -> str:
    """256-colour foreground."""
    return sgr(38, 5, index)


def paint(text: str, *prefixes: str) -> str:
    """Wrap ``text`` in the given escape prefixes and reset afterwards.

    Empty prefixes (colour disabled) collapse to the bare text, so callers
    never need to branch on capability.
    """
    lead = "".join(p for p in prefixes if p)
    if not lead:
        return text
    return f"{lead}{text}{RESET}"
