"""Credential detection, validation, storage, and the dual-path wizard.

Nothing here assumes the `ant` CLI exists, that a browser is available, or
that stdin is a terminal. Each of those is a supported configuration with its
own path, and none of them is a crash.
"""

from .detect import Credential, Source, ant_available, ant_status, detect
from .store import CredentialStore, app_dir
from .validate import Validation, Verdict, validate_ant_profile, validate_key

__all__ = [
    "Credential",
    "CredentialStore",
    "Source",
    "Validation",
    "Verdict",
    "ant_available",
    "ant_status",
    "app_dir",
    "detect",
    "validate_ant_profile",
    "validate_key",
]
