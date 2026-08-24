"""Failure classification and stuck-loop detection.

*Assimilated from* ``EverMind-AI/Raven`` (``raven/agent/loop/failure_streak.py``).

The two judgements Raven isolates -- and why both matter
--------------------------------------------------------
**1. Which failures count at all.** A 429 or a timeout clears itself on retry.
Counting those toward a stuck-loop streak means nudging a model that is doing
nothing wrong. Only *deterministic* failures -- ones that recur identically on
an identical retry -- are evidence of a loop.

**2. What counts as "the same failure".** Two different errors from one tool
mean the model is still adapting, and interrupting adaptation is the opposite
of helping. The streak is therefore keyed on ``(tool, failure_class)``, not on
the tool alone.

There is a third case both of the above would get wrong: a tool that ran
perfectly and found nothing. A repeated empty search is legitimate exploration.
``no matches found`` is a success, and is excluded explicitly.

This module is pure. No I/O, no LLM, no loop -- it decides, the caller acts.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

# Markers of a failure a plain retry would likely clear. These must NOT count
# toward a streak.
TRANSIENT_MARKERS = (
    "429", "rate limit", "rate_limit", "timed out", "timeout",
    "no healthy upstream", "502", "503", "504", "connection reset",
    "temporarily unavailable", "try again",
)

# The tool ran fine and found nothing. Success, not failure.
EMPTY_SUCCESS_MARKERS = (
    "no matches found", "no files found", "no results", "0 results", "not modified",
)

_EXIT_CODE = re.compile(r"exit code:\s*(-?\d+)", re.IGNORECASE)

DEFAULT_STREAK_THRESHOLD = 3


def failure_class(text: str) -> str:
    """Coarse failure category, for streak accounting.

    Deliberately coarse. The streak asks one question -- *is the model
    repeating the same dead call* -- and over-fine classes would split one
    repeated mistake into several, never reaching the threshold.

    But not too coarse: ``invalid_arguments`` (malformed JSON) is kept
    separate from ``schema`` (well-formed, wrong shape). A model that moves
    from one to the other has changed what it is doing, and that is exactly
    the distinction this key exists to preserve.
    """
    low = text[:250].lower()
    if "[truncated]" in low or "output truncated" in low:
        return "truncated"
    if "[incomplete arguments]" in low:
        return "incomplete_arguments"
    if "[invalid arguments]" in low or "json" in low and "decode" in low:
        return "invalid_arguments"
    if "invalid parameters" in low or "schema" in low:
        return "schema"
    if "no such file" in low or "not found" in low or "enoent" in low:
        return "not_found"
    if "permission" in low or "denied" in low or "eacces" in low:
        return "denied"
    if "timed out" in low or "timeout" in low:
        return "timeout"
    if "refused by guardrail" in low or "refused pending" in low:
        return "guardrail"
    return "other"


def is_transient(text: str) -> bool:
    """True when a plain retry would plausibly clear this."""
    low = text.lower()
    return any(marker in low for marker in TRANSIENT_MARKERS)


def is_empty_success(text: str) -> bool:
    """True when the tool succeeded and simply found nothing."""
    normalized = text.strip().rstrip(".").lower()
    return any(normalized == marker or normalized.endswith(marker) for marker in EMPTY_SUCCESS_MARKERS)


# Harness-emitted markers. These carry no "Error:" prefix and no exit code, so
# a prefix-and-exit-code check misses them entirely -- and they are precisely
# the deterministic failures worth counting. A payload that overran the output
# limit will overrun it again on an identical retry.
_EXPLICIT_FAILURE_MARKERS = (
    "[truncated]",
    "output truncated",
    "[invalid arguments]",
    "[incomplete arguments]",
    "invalid parameters",
    "refused by guardrail",
    "refused pending",
    "denied and cannot be overridden",
)


def is_hard_failure(result: object) -> bool:
    """True for a deterministic failure -- one that recurs on an identical retry.

    False for success and for transient errors. This is the gate that decides
    whether a repeated call is evidence of a stuck loop.
    """
    text = str(result)
    if is_transient(text):
        return False
    if is_empty_success(text):
        return False

    low = text.lower()
    if any(marker in low for marker in _EXPLICIT_FAILURE_MARKERS):
        return True

    match = _EXIT_CODE.search(text)
    if match:
        return match.group(1) != "0"

    stripped = text.lstrip()
    return stripped.startswith("Error") or "error:" in text[:120].lower()


@dataclass
class StreakTracker:
    """Counts consecutive identical hard failures per ``(tool, class)``.

    A success on a tool clears every streak for that tool -- the model got
    somewhere, and history before that point is no longer evidence of being
    stuck.
    """

    threshold: int = DEFAULT_STREAK_THRESHOLD
    _counts: Dict[Tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))

    def record(self, tool: str, result: object) -> Optional[str]:
        """Record one tool outcome. Returns a nudge when a streak trips.

        Returns ``None`` on success, on a transient failure, and on every hard
        failure below the threshold.
        """
        text = str(result)

        if not is_hard_failure(text):
            for key in [k for k in self._counts if k[0] == tool]:
                del self._counts[key]
            return None

        klass = failure_class(text)
        key = (tool, klass)
        self._counts[key] += 1

        if self._counts[key] >= self.threshold:
            self._counts[key] = 0  # nudge once, then let it re-earn the streak
            return loop_break_nudge(tool, self.threshold, klass)
        return None

    def streak(self, tool: str, klass: str = "other") -> int:
        return self._counts.get((tool, klass), 0)

    def reset(self) -> None:
        self._counts.clear()


def loop_break_nudge(tool: str, n: int, klass: str = "other") -> str:
    """The message injected when a streak trips.

    Keyed on the failure class, because "change approach" is not always
    somewhere to go. A repeated truncation is a payload outrunning an output
    limit -- the fix is a smaller read, not a different tool. Telling a model
    to change tools there sends it away from the one that would have worked.
    """
    specific = {
        "truncated": (
            f"`{tool}` has returned truncated output {n} times. The payload is "
            "exceeding the output limit — narrow the request (a line range, a "
            "filter, a smaller page) rather than changing tool."
        ),
        "not_found": (
            f"`{tool}` has failed to find the target {n} times. The path or "
            "identifier is likely wrong — list the parent or search for it "
            "before addressing it directly again."
        ),
        "denied": (
            f"`{tool}` has been denied {n} times. This is a permission boundary, "
            "not a syntax problem. Retrying the same call cannot succeed; take "
            "another route or report the blocker."
        ),
        "schema": (
            f"`{tool}` has rejected your arguments {n} times. Re-read the tool's "
            "parameter definition before the next call."
        ),
        "invalid_arguments": (
            f"`{tool}` has received malformed arguments {n} times. Emit valid "
            "JSON with every required field present."
        ),
        "guardrail": (
            f"`{tool}` has been refused by the guardrail {n} times. The command "
            "is classified destructive. Rephrase it to something narrower, or "
            "ask for explicit approval — repeating it will not clear it."
        ),
    }
    return specific.get(
        klass,
        f"`{tool}` has failed the same way {n} times running. The current approach "
        "is not working — change it rather than repeating the call.",
    )
