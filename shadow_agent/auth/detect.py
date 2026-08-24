"""Credential detection.

An unset ``ANTHROPIC_API_KEY`` does **not** mean there are no credentials. The
SDK and the ``ant`` CLI resolve them in a defined order, and a user who ran
``ant auth login`` has a working profile on disk with no environment variable
set anywhere. A framework that checks one variable and declares failure is
wrong about the machine it is running on.

Resolution order, first match wins -- mirroring the SDK's own:

    1. ANTHROPIC_API_KEY          environment
    2. ANTHROPIC_AUTH_TOKEN       environment (OAuth bearer)
    3. ~/.shadow-agent/credentials.json    this framework's own store
    4. an active `ant auth` profile        only if the binary exists

Step 4 is the one that must never assume. ``ant`` is an optional install; on a
machine without it, probing is a ``FileNotFoundError`` waiting to happen. Every
call here goes through :func:`ant_available` first.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .store import CredentialStore

ANT_PROBE_TIMEOUT = 6.0


class Source(Enum):
    """Where a credential came from. Ordered by resolution precedence."""

    ENV_API_KEY = "environment: ANTHROPIC_API_KEY"
    ENV_AUTH_TOKEN = "environment: ANTHROPIC_AUTH_TOKEN"
    LOCAL_STORE = "local store: ~/.shadow-agent/credentials.json"
    ANT_PROFILE = "ant CLI profile"
    NONE = "none"


@dataclass
class Credential:
    """The resolved credential state of this machine."""

    source: Source = Source.NONE
    detail: str = ""
    secret: Optional[str] = None       # None for ANT_PROFILE: the CLI holds it
    ant_installed: bool = False
    ant_profile: Optional[str] = None

    @property
    def present(self) -> bool:
        return self.source is not Source.NONE

    @property
    def masked(self) -> str:
        """A safe rendering. The full secret is never returned to the UI."""
        if not self.secret:
            return "—" if not self.present else "(held by ant CLI)"
        s = self.secret
        if len(s) <= 14:
            return s[:4] + "…"
        return f"{s[:11]}…{s[-4:]}"


def ant_available() -> bool:
    """True when the ``ant`` binary is on PATH.

    Every ``ant`` invocation in this framework is gated on this. The binary is
    an optional install, and a public tool must not fault on a machine that
    never had it.
    """
    return shutil.which("ant") is not None


def ant_status() -> tuple:
    """Probe ``ant auth status``. Returns ``(logged_in, profile_or_message)``.

    Never raises: a missing binary, a non-zero exit, a hang, or a garbled
    response all resolve to ``(False, reason)``.
    """
    if not ant_available():
        return False, "ant is not installed"
    try:
        result = subprocess.run(
            ["ant", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=ANT_PROBE_TIMEOUT,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"ant probe failed: {type(exc).__name__}"

    blob = f"{result.stdout}\n{result.stderr}".strip()
    if result.returncode != 0:
        return False, "no active ant profile"

    # The CLI's exact phrasing is not contractual, so match loosely and treat
    # a zero exit as the authoritative signal.
    lowered = blob.lower()
    if "not logged in" in lowered or "no active" in lowered:
        return False, "no active ant profile"

    profile = ""
    for line in blob.splitlines():
        stripped = line.strip()
        if "profile" in stripped.lower() and ":" in stripped:
            profile = stripped.split(":", 1)[1].strip()
            break
    return True, profile or "active"


def detect(store: Optional[CredentialStore] = None) -> Credential:
    """Resolve credentials for this machine. Never raises."""
    store = store or CredentialStore()
    installed = ant_available()

    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if key:
        return Credential(Source.ENV_API_KEY, "exported in this shell", key, installed)

    token = (os.environ.get("ANTHROPIC_AUTH_TOKEN") or "").strip()
    if token:
        return Credential(Source.ENV_AUTH_TOKEN, "OAuth bearer token", token, installed)

    stored = store.read_key()
    if stored:
        return Credential(Source.LOCAL_STORE, str(store.path), stored, installed)

    if installed:
        logged_in, profile = ant_status()
        if logged_in:
            return Credential(
                Source.ANT_PROFILE,
                "resolved by the SDK at call time",
                None,
                True,
                profile,
            )

    return Credential(Source.NONE, "no credential found", None, installed)
