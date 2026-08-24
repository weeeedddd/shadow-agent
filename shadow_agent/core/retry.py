"""Exponential backoff with jitter.

License note -- read before assuming provenance
-----------------------------------------------
``666ghj/MiroFish`` is licensed **AGPL-3.0**. Deriving code from it into this
MIT-licensed project would be a licence violation, so **no code was taken from
it.** MiroFish's ``backend/app/utils/retry.py`` was read and confirmed the
pattern was worth having; the implementation below is written from scratch
against the standard formulation of exponential backoff with full jitter,
which is long-established prior art independent of any single project.

Scope note: MiroFish is a swarm-simulation platform -- OASIS agent profiles,
Zep graph memory, Twitter/Reddit simulation orchestration -- not an agent
harness. Almost none of it transfers, and its one broadly-reusable idea is a
pattern rather than an implementation.

What makes the pattern worth having is the **jitter**. Backoff alone synchronises
retries: N clients that fail together retry together, and the thundering herd
re-creates the outage they were backing off from. Randomising the delay
decorrelates them. The typed exception filter matters too -- a bare
``except Exception`` retries a 401 four times before failing, which is four
times slower than failing immediately and no more likely to succeed.
"""

from __future__ import annotations

import functools
import random
import time
from typing import Callable, Optional, Tuple, Type, TypeVar

T = TypeVar("T")


def compute_delay(
    attempt: int,
    initial: float = 1.0,
    factor: float = 2.0,
    maximum: float = 30.0,
    jitter: bool = True,
) -> float:
    """Delay before ``attempt`` (1-indexed), capped and optionally jittered.

    Full jitter -- uniform over ``[0, delay]`` rather than a small wobble
    around it. Partial jitter still leaves retries clustered; full jitter
    spreads them across the whole window, which is the behaviour that actually
    breaks up a herd.
    """
    delay = min(initial * (factor ** max(0, attempt - 1)), maximum)
    return random.uniform(0, delay) if jitter else delay


def retry_with_backoff(
    max_retries: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
    jitter: bool = True,
    exceptions: Tuple[Type[BaseException], ...] = (Exception,),
    on_retry: Optional[Callable[[BaseException, int, float], None]] = None,
) -> Callable:
    """Retry a callable on the listed exceptions.

    ``on_retry(exc, attempt, delay)`` fires before each sleep. Retrying in
    silence turns a slow call into an unexplained one -- the callback is how
    the wait becomes visible.

    The final failure re-raises the original exception, not a wrapper. The
    caller's ``except`` clauses should not have to change to accommodate the
    fact that a retry happened.
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last: Optional[BaseException] = None
            for attempt in range(1, max_retries + 2):  # N retries = N+1 attempts
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last = exc
                    if attempt > max_retries:
                        break
                    delay = compute_delay(attempt, initial_delay, backoff_factor, max_delay, jitter)
                    if on_retry:
                        on_retry(exc, attempt, delay)
                    time.sleep(delay)
            assert last is not None
            raise last

        return wrapper

    return decorator
