"""Cross-platform exclusive lock on the state directory.

*Concept assimilated from* ``EverMind-AI/EverOS``
(``src/everos/core/persistence/locking.py``). **The implementation is not.**

Why this one was rewritten rather than ported
---------------------------------------------
EverOS locks with ``fcntl.flock`` and says so in its own docstring: POSIX only,
Windows unsupported. Copying it would have imported a hard platform failure
into a framework whose first user is on Windows 11. What survives the port is
the *reasoning*, which is sound and platform-independent:

**Poll with a non-blocking attempt rather than blocking in a thread.** A
blocking lock acquisition cannot be bounded or cancelled; if the waiter gives
up, the syscall still completes later with nobody left to release it -- worse
than having waited. Short non-blocking attempts on an interval give identical
semantics while staying bounded, cancellable, and *visible*.

**Visibility is the point.** The wait is often correct behaviour -- a second
process is supposed to wait and then find the work already done. Without a log
line, that correct wait is indistinguishable from a hang.

**A generous timeout.** This bound exists for diagnosis, not recovery. When the
holder is genuinely wedged, giving up at 30 seconds instead of 300 does not
un-wedge it, and the operator's next move is the same either way. Meanwhile a
bound set near the legitimate hold time turns the slowest honest operation into
a crash for everyone waiting on it.

Implementation
--------------
``msvcrt.locking`` on Windows, ``fcntl.flock`` on POSIX, and a stale-PID
lockfile as the last resort. All three are advisory: they coordinate cooperating
processes, and nothing here prevents a determined writer from ignoring them.

A crash-released lock is handled differently per platform. Both OS-level locks
are released by the kernel on process exit, so a dead holder never strands one.
The fallback cannot rely on that, so it records the holder's PID and reclaims a
lock whose owner is gone.
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from typing import Iterator, Optional

DEFAULT_TIMEOUT = 120.0
POLL_INTERVAL = 0.25
_WARN_AFTER = 2.0

_HAS_FCNTL = False
_HAS_MSVCRT = False
try:
    import fcntl  # type: ignore

    _HAS_FCNTL = True
except ImportError:
    try:
        import msvcrt  # type: ignore

        _HAS_MSVCRT = True
    except ImportError:
        pass


class LockTimeout(TimeoutError):
    """The lock could not be acquired within the bound."""


def _pid_alive(pid: int) -> bool:
    """True when a process with this PID exists.

    Used only by the fallback path, to reclaim a lock whose holder died.
    """
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        os.kill(pid, 0)  # signal 0: existence check, delivers nothing
        return True
    except (OSError, PermissionError):
        # PermissionError means it exists and belongs to someone else.
        return isinstance(getattr(os, "kill", None), object) and os.name != "nt"
    except Exception:
        return False


def _try_acquire(handle) -> bool:
    """One non-blocking attempt. True on success."""
    try:
        if _HAS_FCNTL:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        if _HAS_MSVCRT:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
    except OSError:
        return False
    return False


def _release(handle) -> None:
    try:
        if _HAS_FCNTL:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif _HAS_MSVCRT:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


@contextlib.contextmanager
def state_lock(
    lock_path: Path,
    timeout: float = DEFAULT_TIMEOUT,
    on_wait: Optional[callable] = None,
) -> Iterator[bool]:
    """Hold an exclusive advisory lock on ``lock_path``.

    Yields ``True`` when the lock is held, ``False`` when locking is
    unavailable on this platform (the caller proceeds unprotected but
    informed -- silently pretending to be locked would be worse).

    ``on_wait(seconds_waited)`` fires once, after the wait becomes long enough
    to be worth reporting. That callback is the visibility the EverOS docstring
    argues for.
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if not (_HAS_FCNTL or _HAS_MSVCRT):
        held = _fallback_acquire(lock_path, timeout, on_wait)
        try:
            yield held
        finally:
            if held:
                with contextlib.suppress(OSError):
                    lock_path.unlink()
        return

    handle = open(lock_path, "a+b")
    started = time.monotonic()
    warned = False
    acquired = False
    try:
        while True:
            if _try_acquire(handle):
                acquired = True
                break
            waited = time.monotonic() - started
            if waited > timeout:
                raise LockTimeout(
                    f"could not acquire {lock_path} after {timeout:.0f}s; "
                    "another shadow process is holding the state directory"
                )
            if not warned and waited >= _WARN_AFTER and on_wait:
                warned = True
                on_wait(waited)
            time.sleep(POLL_INTERVAL)

        try:
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()).encode("ascii"))
            handle.flush()
        except OSError:
            pass  # the lock is what matters; the PID note is a courtesy

        yield True
    finally:
        if acquired:
            _release(handle)
        try:
            handle.close()
        except OSError:
            pass


def _fallback_acquire(lock_path: Path, timeout: float, on_wait) -> bool:
    """Atomic-create lockfile, used when no OS lock primitive exists.

    ``O_EXCL`` create is the atomic operation. Because no kernel releases this
    on crash, a lock whose recorded PID is gone is reclaimed.
    """
    started = time.monotonic()
    warned = False
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            return True
        except FileExistsError:
            try:
                holder = int(lock_path.read_text(encoding="ascii").strip() or 0)
            except (OSError, ValueError):
                holder = 0
            if holder and not _pid_alive(holder):
                with contextlib.suppress(OSError):
                    lock_path.unlink()
                continue
            waited = time.monotonic() - started
            if waited > timeout:
                raise LockTimeout(f"could not acquire {lock_path} after {timeout:.0f}s")
            if not warned and waited >= _WARN_AFTER and on_wait:
                warned = True
                on_wait(waited)
            time.sleep(POLL_INTERVAL)
        except OSError:
            return False


def locking_backend() -> str:
    """Which primitive is in use -- reported in `shadow status`."""
    if _HAS_FCNTL:
        return "fcntl.flock (POSIX)"
    if _HAS_MSVCRT:
        return "msvcrt.locking (Windows)"
    return "lockfile fallback"
