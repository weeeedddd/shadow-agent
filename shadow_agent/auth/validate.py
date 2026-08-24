"""Instant credential validation.

Validation hits ``GET /v1/models?limit=1`` rather than the Messages API. It is
the cheapest authenticated call available: no tokens generated, no bill, no
model selected, and the answer to "is this key live" is exactly the HTTP status.

Implemented on ``urllib`` from the standard library, on purpose. Validation is
the very first thing a new user touches, and requiring an SDK install *before*
you can prove your key works inverts the order of operations.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

API_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
KEY_PREFIX = "sk-ant-"
TIMEOUT = 15.0


class Verdict(Enum):
    VALID = "valid"
    REJECTED = "rejected"            # authenticated request refused: 401 / 403
    MALFORMED = "malformed"          # failed the shape check; never sent
    UNREACHABLE = "unreachable"      # DNS, TLS, proxy, offline
    UNKNOWN = "unknown"              # reached the API, got an unexpected status


@dataclass
class Validation:
    verdict: Verdict
    message: str
    status: Optional[int] = None
    models: Optional[List[str]] = None

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.VALID


def looks_like_key(candidate: str) -> bool:
    """Cheap shape check, run before any network call.

    A paste that is obviously not a key -- a truncated copy, a whole shell
    line, an OpenAI key -- should be rejected with a useful message instead of
    a 401 that says nothing about what went wrong.
    """
    candidate = candidate.strip()
    return candidate.startswith(KEY_PREFIX) and len(candidate) >= 40 and " " not in candidate


def validate_key(api_key: str, timeout: float = TIMEOUT) -> Validation:
    """Check a key against the live API. Never raises."""
    api_key = (api_key or "").strip()

    if not api_key:
        return Validation(Verdict.MALFORMED, "no key provided")
    if not api_key.startswith(KEY_PREFIX):
        return Validation(
            Verdict.MALFORMED,
            f"an Anthropic key starts with '{KEY_PREFIX}' — this does not",
        )
    if not looks_like_key(api_key):
        return Validation(Verdict.MALFORMED, "key is too short or contains whitespace")

    request = urllib.request.Request(
        f"{API_BASE}/v1/models?limit=1",
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "accept": "application/json",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        if status in (401, 403):
            return Validation(Verdict.REJECTED, "the API rejected this key", status)
        if status == 429:
            # The key authenticated; it is simply rate limited. Treating that
            # as invalid would send a user off to regenerate a working key.
            return Validation(Verdict.VALID, "key accepted (rate limited)", status)
        return Validation(Verdict.UNKNOWN, f"unexpected response: HTTP {status}", status)
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return Validation(Verdict.UNREACHABLE, f"could not reach the API: {reason}")

    models: List[str] = []
    try:
        for entry in (json.loads(body).get("data") or []):
            if isinstance(entry, dict) and entry.get("id"):
                models.append(str(entry["id"]))
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass

    return Validation(Verdict.VALID, "key accepted by the API", status, models)


def validate_ant_profile(timeout: float = TIMEOUT) -> Validation:
    """Validate an ``ant`` profile by exchanging it for a short-lived token.

    ``ant auth print-credentials --access-token`` yields a bearer token. OAuth
    tokens go on ``Authorization: Bearer`` with the ``oauth-2025-04-20`` beta
    header -- not on ``x-api-key``. Sending one as an API key returns 401 and
    looks exactly like a bad login.
    """
    import subprocess

    from .detect import ant_available

    if not ant_available():
        return Validation(Verdict.UNREACHABLE, "ant is not installed")

    try:
        result = subprocess.run(
            ["ant", "auth", "print-credentials", "--access-token"],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Validation(Verdict.UNREACHABLE, f"could not run ant: {type(exc).__name__}")

    token = (result.stdout or "").strip()
    if result.returncode != 0 or not token:
        return Validation(Verdict.REJECTED, "ant did not return an access token")

    request = urllib.request.Request(
        f"{API_BASE}/v1/models?limit=1",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": ANTHROPIC_VERSION,
            "accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return Validation(Verdict.VALID, "ant profile accepted", response.status)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return Validation(Verdict.REJECTED, "the API rejected the ant profile", exc.code)
        return Validation(Verdict.UNKNOWN, f"unexpected response: HTTP {exc.code}", exc.code)
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        return Validation(Verdict.UNREACHABLE, f"could not reach the API: {getattr(exc, 'reason', exc)}")
