"""Path-safety primitive.

*Assimilated from* ``EverMind-AI/EverOS``
(``src/everos/core/persistence/markdown/path_safety.py``).

The framework turns free text into filesystem path segments in several places:
memory keys, snapshot labels, checkpoint names. Every one of those sources is
untrusted in the same way -- a memory key can come from LLM output, a label
from a user request -- so a name containing ``../`` or a separator must never
survive into a directory segment. That is CWE-22, path traversal.

**This is the one place that decision is made.** Callers route through
:func:`sanitize_segment` rather than each keeping a private regex, because the
failure mode of scattered copies is that one of them is subtly weaker.

The idempotence guarantee -- ``sanitize(sanitize(x)) == sanitize(x)`` -- is
what lets a reader and a writer agree on a path when one holds the raw name
and the other only the on-disk segment.
"""

from __future__ import annotations

import re
import unicodedata

MAX_SEGMENT_LEN = 60

# \w is Unicode-aware, so CJK and other scripts survive readably rather than
# being mangled into the fallback.
_UNSAFE = re.compile(r"[^\w\-.]", re.UNICODE)
_DOT_RUN = re.compile(r"\.{2,}")
_DEGENERATE = frozenset({"", ".", ".."})

# Windows refuses these as filenames regardless of extension. A memory key
# named "con" would otherwise produce a file that cannot be created.
_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def sanitize_segment(raw: str, fallback: str = "unnamed") -> str:
    """Produce a filesystem-safe path segment from free text.

    * NFC-normalise first, so a decomposed accented letter collapses to its
      precomposed form before filtering -- otherwise the combining mark alone
      is not ``\\w`` and gets silently stripped, changing the word.
    * Replace whitespace with underscores.
    * Strip anything outside ``[\\w.-]``, which removes ``/``, ``\\``, and the
      whole separator family.
    * Collapse runs of dots, which is what neutralises ``..`` traversal even
      after the character filter.
    * Reject degenerate and Windows-reserved results.

    Best-effort on Unicode fidelity, not a guarantee: for composition-exclusion
    codepoints NFC decomposes rather than composes, and the resulting mark is
    stripped like any other. Safety is exact; readability is best-effort.
    """
    if not isinstance(raw, str):
        raw = str(raw)

    segment = unicodedata.normalize("NFC", raw).strip()
    segment = re.sub(r"\s+", "_", segment)
    segment = _UNSAFE.sub("", segment)
    segment = _DOT_RUN.sub(".", segment)
    segment = segment.strip("._-")
    segment = segment[:MAX_SEGMENT_LEN].rstrip("._-")

    if segment.lower() in _DEGENERATE or segment.lower() in _RESERVED:
        return fallback
    return segment or fallback


def is_safe_segment(candidate: str) -> bool:
    """True when ``candidate`` is already a safe segment (idempotence check)."""
    return bool(candidate) and sanitize_segment(candidate, fallback="\x00") == candidate
