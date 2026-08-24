"""The reasoning core -- the LLM boundary.

The framework talks to exactly one thing here, through one method. Keeping the
surface this narrow is what lets the Monarch and the Eminence be tested without
a network, and what lets the provider change without touching either.

Request shape (Claude Opus 5, the default core)
-----------------------------------------------
Every field below was verified against ``anthropic`` 1.0.0's own type stubs
rather than recalled:

* ``thinking={"type": "adaptive"}`` -- the model decides when and how deeply to
  reason. The fixed ``budget_tokens`` dial is gone and is **rejected with a
  400** on this model.
* ``output_config={"effort": ...}`` -- the spend dial, ``low`` through ``max``.
  It nests inside ``output_config``; it is not top-level.
* ``output_config={"format": {"type": "json_schema", "schema": {...}}}`` --
  structured output. ``JSONOutputFormatParam`` requires exactly those two keys.
* Streaming for anything long. A large ``max_tokens`` on a non-streaming
  request collects an HTTP timeout instead of an answer.

Error handling
--------------
SDK exceptions are translated into this module's own taxonomy so that nothing
above it needs to import ``anthropic`` to know what went wrong -- and so the
terminal never sees a raw traceback. The distinction that matters is
**retryable versus not**: a 429 or a dropped connection deserves a backoff, a
401 or a 400 deserves an immediate honest failure. Catching one broad class
loses that, and turns a bad key into four slow retries.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Sequence, runtime_checkable

from ..config import LLMConfig
from ..core.errors import ConfigError, ShadowError


class ReasoningError(ShadowError):
    """The reasoning core could not be reached, or refused the request."""

    retryable = False


class CoreUnavailable(ReasoningError):
    """Network, DNS, TLS, proxy, or timeout. Worth retrying."""

    retryable = True


class CoreRateLimited(ReasoningError):
    """429. Worth retrying, after a wait."""

    retryable = True


class CoreOverloaded(ReasoningError):
    """529 / server overload. Worth retrying."""

    retryable = True


class CoreAuthError(ReasoningError):
    """401 / 403. The credential is wrong -- retrying cannot fix it."""


class CoreBadRequest(ReasoningError):
    """400. The request is malformed -- retrying cannot fix it either."""


@dataclass
class Reply:
    """A normalised response, provider-agnostic."""

    text: str
    model: str
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    raw: Any = None

    @property
    def refused(self) -> bool:
        """True when safety classifiers declined the request.

        A refusal arrives as HTTP 200 with ``stop_reason == "refusal"`` -- not
        as an exception. Code that reads ``.text`` without checking this will
        process an empty answer as a real one.
        """
        return self.stop_reason == "refusal"

    def json(self) -> Any:
        """Parse the reply as JSON, tolerating a fenced or padded response.

        Used even with structured output on: the schema constrains the shape,
        but a defensive parse costs nothing and turns one class of transient
        formatting failure into a recoverable one.
        """
        return extract_json(self.text)


def extract_json(text: str) -> Any:
    """Pull a JSON value out of model output. Returns None if there is none."""
    if not text:
        return None
    candidate = text.strip()

    fence = re.search(r"```(?:json)?\s*\n(.*?)```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # Fall back to the outermost bracketed span.
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = candidate.find(opener), candidate.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


@runtime_checkable
class ReasoningCore(Protocol):
    """Everything the framework needs from an LLM."""

    def complete(
        self,
        system: str,
        messages: Sequence[Dict[str, Any]],
        tools: Optional[Sequence[Dict[str, Any]]] = None,
        max_tokens: Optional[int] = None,
        schema: Optional[Dict[str, Any]] = None,
    ) -> Reply:
        ...


def build_request(
    config: LLMConfig,
    system: str,
    messages: Sequence[Dict[str, Any]],
    tools: Optional[Sequence[Dict[str, Any]]] = None,
    max_tokens: Optional[int] = None,
    schema: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble the request kwargs. Pure -- no network, fully testable."""
    request: Dict[str, Any] = {
        "model": config.model,
        "max_tokens": max_tokens or config.max_tokens,
        "messages": list(messages),
    }
    if system:
        request["system"] = system
    if config.thinking == "adaptive":
        request["thinking"] = {"type": "adaptive"}

    output_config: Dict[str, Any] = {}
    if config.effort:
        output_config["effort"] = config.effort
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    if output_config:
        request["output_config"] = output_config

    if tools:
        request["tools"] = list(tools)
    return request


def translate_error(exc: BaseException) -> ReasoningError:
    """Map an SDK exception onto this module's taxonomy.

    Matched on class *name* rather than by importing the exception types, so
    this keeps working if the SDK is absent, reorganised, or a different
    version than the one this was written against. The status code is
    consulted first because it is the more stable signal.
    """
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    detail = str(exc)[:300]

    if status == 429 or "RateLimit" in name:
        return CoreRateLimited(f"rate limited: {detail}")
    if status in (401, 403) or "Authentication" in name or "PermissionDenied" in name:
        return CoreAuthError(f"credential rejected: {detail}")
    if status == 400 or "BadRequest" in name:
        return CoreBadRequest(f"malformed request: {detail}")
    if status in (500, 502, 503, 529) or "Overloaded" in name or "InternalServer" in name:
        return CoreOverloaded(f"service unavailable: {detail}")
    if "Connection" in name or "Timeout" in name or isinstance(exc, (OSError, TimeoutError)):
        return CoreUnavailable(f"could not reach the API: {detail}")
    return ReasoningError(f"{name}: {detail}")


class AnthropicCore:
    """Claude-backed reasoning core.

    The SDK is imported lazily so the rest of the framework -- onboarding,
    state, execution, the wall -- runs with no third-party dependency
    installed at all.
    """

    def __init__(self, config: Optional[LLMConfig] = None, api_key: Optional[str] = None) -> None:
        self.config = config or LLMConfig()
        self.config.validate()
        self.api_key = api_key
        self._client: Any = None

    @property
    def available(self) -> bool:
        """True when the SDK can be imported. Does not prove a credential works."""
        try:
            import anthropic  # noqa: F401,PLC0415

            return True
        except ImportError:
            return False

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import anthropic  # noqa: PLC0415 -- deliberate lazy import
            except ImportError as exc:
                raise ConfigError(
                    "the anthropic SDK is not installed; run "
                    '`pip install -e ".[llm]"` or `pip install anthropic`'
                ) from exc

            # An explicit key wins; otherwise a bare constructor is correct --
            # the SDK resolves ANTHROPIC_API_KEY, then ANTHROPIC_AUTH_TOKEN,
            # then an `ant auth login` profile on disk. An unset env var does
            # not mean there is no credential.
            self._client = (
                anthropic.Anthropic(api_key=self.api_key) if self.api_key else anthropic.Anthropic()
            )
        return self._client

    def complete(
        self,
        system: str,
        messages: Sequence[Dict[str, Any]],
        tools: Optional[Sequence[Dict[str, Any]]] = None,
        max_tokens: Optional[int] = None,
        schema: Optional[Dict[str, Any]] = None,
    ) -> Reply:
        """One request. Raises a :class:`ReasoningError` subclass on failure."""
        request = build_request(self.config, system, messages, tools, max_tokens, schema)

        try:
            if self.config.stream:
                with self.client.messages.stream(**request) as stream:
                    message = stream.get_final_message()
            else:
                message = self.client.messages.create(**request)
        except ConfigError:
            raise
        except Exception as exc:
            raise translate_error(exc) from exc

        usage = getattr(message, "usage", None)
        return Reply(
            text=_extract_text(message),
            model=getattr(message, "model", self.config.model),
            stop_reason=getattr(message, "stop_reason", "") or "",
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            raw=message,
        )

    def complete_with_retry(
        self,
        system: str,
        messages: Sequence[Dict[str, Any]],
        *,
        schema: Optional[Dict[str, Any]] = None,
        max_tokens: Optional[int] = None,
        attempts: int = 3,
        on_retry: Optional[Any] = None,
    ) -> Reply:
        """Retry only what retrying can fix.

        A 429, a 529, or a dropped connection gets exponential backoff with
        jitter. A 401 or a 400 fails immediately -- retrying a bad key is four
        times slower and no more likely to work.
        """
        from ..core.retry import compute_delay

        last: Optional[ReasoningError] = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                return self.complete(system, messages, max_tokens=max_tokens, schema=schema)
            except ReasoningError as exc:
                last = exc
                if not exc.retryable or attempt >= attempts:
                    raise
                delay = compute_delay(attempt, initial=1.5, maximum=20.0)
                if on_retry:
                    try:
                        on_retry(exc, attempt, delay)
                    except Exception:
                        pass
                import time

                time.sleep(delay)
        assert last is not None
        raise last


def _extract_text(message: Any) -> str:
    """Concatenate the text blocks of a response, ignoring thinking blocks."""
    blocks = getattr(message, "content", None) or []
    parts: List[str] = []
    for block in blocks:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
    return "".join(parts).strip()
