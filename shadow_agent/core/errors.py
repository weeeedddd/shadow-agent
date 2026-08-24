"""Exception hierarchy.

One root, three branches -- one per module. A caller that wants to catch
everything the framework raises catches :class:`ShadowError`; a caller that
cares which stage failed catches the specific branch. Nothing in this codebase
raises a bare ``Exception``.
"""

from __future__ import annotations


class ShadowError(Exception):
    """Root of every error raised by the framework."""


class MonarchError(ShadowError):
    """Analysis failed: the directive could not be formed."""


class EminenceError(ShadowError):
    """Execution failed: a command or file operation did not complete."""


class ArchitectError(ShadowError):
    """State failed: persistence, journalling, or rollback could not proceed."""


class ConfigError(ShadowError):
    """Configuration is missing, malformed, or contradictory."""


class GuardrailError(EminenceError):
    """A command was refused by the Eminence guardrail.

    Carries the matched pattern so the caller can explain *why* it was
    stopped rather than merely reporting a refusal.
    """

    def __init__(self, message: str, command: str = "", pattern: str = "") -> None:
        super().__init__(message)
        self.command = command
        self.pattern = pattern
