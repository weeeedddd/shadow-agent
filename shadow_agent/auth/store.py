"""Local credential storage.

**This is a permission-restricted file, not encrypted storage.** Say it plainly
rather than implying protection that does not exist: anything running as this
user can read it, and it outlives the session. What is actually enforced:

* stored under ``~/.shadow-agent/``, never in the project directory, so it
  cannot be swept into a commit by a wildcard ``git add``
* ``0600`` on POSIX -- owner read/write, nothing for group or other
* owner-only ACL on Windows via ``icacls``, with inheritance broken so a
  permissive parent directory cannot widen it back
* written through a temp file whose restrictive mode is set *before* the
  secret is written, so the key is never briefly world-readable
* never logged, never echoed, masked in every render

If you want real secret storage, the honest answer is an OS keyring. That is
one optional dependency away and this class is the seam for it.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

APP_DIRNAME = ".shadow-agent"
CREDENTIALS_FILENAME = "credentials.json"


def app_dir() -> Path:
    """The per-user framework directory. Honours ``SHADOW_HOME``."""
    override = os.environ.get("SHADOW_HOME")
    return Path(override) if override else Path.home() / APP_DIRNAME


def _restrict_windows(path: Path) -> bool:
    """Owner-only ACL, inheritance removed. True if both steps succeeded.

    ``/inheritance:r`` is what makes this real. Granting the owner full control
    while leaving inherited ACEs in place does not remove anyone's access --
    a permissive parent directory keeps granting it.
    """
    user = os.environ.get("USERNAME")
    if not user:
        return False
    domain = os.environ.get("USERDOMAIN")
    principal = f"{domain}\\{user}" if domain else user
    try:
        for args in (
            ["icacls", str(path), "/inheritance:r"],
            ["icacls", str(path), "/grant:r", f"{principal}:(F)"],
        ):
            result = subprocess.run(args, capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                return False
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def harden(path: Path) -> bool:
    """Restrict ``path`` to its owner. True when the platform confirmed it."""
    try:
        if os.name == "nt":
            return _restrict_windows(path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        return True
    except OSError:
        return False


class CredentialStore:
    """Reads and writes ``~/.shadow-agent/credentials.json``."""

    def __init__(self, directory: Optional[Path] = None) -> None:
        self.dir = Path(directory) if directory else app_dir()
        self.path = self.dir / CREDENTIALS_FILENAME
        self.hardened: Optional[bool] = None

    # --- read ----------------------------------------------------------------

    def _read(self) -> Dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def read_key(self) -> Optional[str]:
        key = self._read().get("api_key")
        return key.strip() if isinstance(key, str) and key.strip() else None

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def world_readable(self) -> bool:
        """True when POSIX permissions leak the file beyond its owner.

        Windows always reports False -- ACLs are not a mode bitmask, and
        guessing from one would be worse than not reporting.
        """
        if os.name == "nt" or not self.path.is_file():
            return False
        try:
            return bool(self.path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))
        except OSError:
            return False

    # --- write ---------------------------------------------------------------

    def write_key(self, api_key: str, note: str = "") -> Path:
        """Persist an API key with owner-only permissions.

        The temp file is hardened *before* the secret enters it. Writing first
        and restricting afterwards leaves a window -- brief, but real -- in
        which the key sits on disk under the default umask.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        harden(self.dir)

        payload = dict(self._read())
        payload.update({"api_key": api_key, "note": note, "version": 1})

        tmp = self.path.with_suffix(".tmp")
        tmp.touch(mode=0o600, exist_ok=True)
        self.hardened = harden(tmp)
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)
        self.hardened = harden(self.path) and (self.hardened is not False)
        return self.path

    def clear(self) -> bool:
        """Remove the stored credential. True if a file was deleted."""
        if not self.path.is_file():
            return False
        try:
            self.path.unlink()
            return True
        except OSError:
            return False
