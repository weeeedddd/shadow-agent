"""Shared foundations: state retrieval, errors, path safety, locking, retry."""

from .locking import LockTimeout, locking_backend, state_lock
from .pathsafe import sanitize_segment
from .retry import retry_with_backoff

__all__ = ["LockTimeout", "locking_backend", "retry_with_backoff", "sanitize_segment", "state_lock"]
