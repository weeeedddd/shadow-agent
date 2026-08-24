"""Durable atomic writes.

*Assimilated from* ``Human-Agent-Society/CORAL`` (``coral/hub/auto_stop.py``,
Apache-2.0).

The bug this fixes in our own code
----------------------------------
Every atomic write in this framework was:

    tmp.write_text(payload)
    tmp.replace(path)

That is atomic but **not durable.** ``write_text`` returns once the data is in
the OS page cache, not once it is on the disk. ``os.replace`` is atomic with
respect to the *directory entry*, so after a crash you can be left pointing at
a file whose contents never landed -- a zero-length or truncated config, memory
store, or skill.

CORAL's version calls ``fsync`` on the temp file **before** the rename, which
forces the bytes down first. The ordering is the entire point: fsync after
rename does not help, because by then the directory already points at a file
whose contents may not exist.

Two further details worth keeping from their implementation:

* ``mkstemp`` in the **destination directory**, not the system temp dir.
  ``os.replace`` across filesystems is not atomic, and ``/tmp`` is frequently
  a different filesystem.
* The temp file is unlinked on failure, so a crashed write leaves no litter
  for the next reader to trip over.

We add one thing they do not: an optional directory fsync. The rename itself
is only durable once the *directory* is synced, which matters for state a
crash must not lose. It is opt-in because it costs a syscall on every write.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional


def write_atomic(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    sync_dir: bool = False,
    mode: Optional[int] = None,
) -> Path:
    """Write ``text`` to ``path`` atomically and durably.

    Order of operations, all of which matter:

    1. create the temp file **in the destination directory**
    2. write, flush, and ``fsync`` it -- bytes reach the disk
    3. optionally chmod it *before* it becomes visible under the real name
    4. ``os.replace`` -- the rename is atomic
    5. optionally fsync the directory -- the rename itself becomes durable
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            # Applied before the rename: a secret must never be briefly
            # visible under its real name with default permissions.
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    if sync_dir:
        _sync_directory(path.parent)
    return path


def _sync_directory(directory: Path) -> None:
    """fsync a directory so a rename inside it survives a crash.

    Not portable: Windows cannot open a directory as a file descriptor, and
    several filesystems refuse it. Failure here is not an error -- the write
    already succeeded, and this only strengthens a guarantee.
    """
    if os.name == "nt":
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_json_atomic(path: Path, data: Any, *, sync_dir: bool = False, mode: Optional[int] = None) -> Path:
    """Serialise ``data`` and write it durably.

    ``sort_keys`` is not cosmetic: a stable byte layout means an unchanged
    object produces an unchanged file, which keeps diffs meaningful and stops
    a rewrite from looking like a change.
    """
    payload = json.dumps(data, indent=2, sort_keys=True, default=str) + "\n"
    return write_atomic(path, payload, sync_dir=sync_dir, mode=mode)


def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON, returning ``default`` on absence or damage. Never raises."""
    path = Path(path)
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
