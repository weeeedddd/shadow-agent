"""The reasoning core -- the LLM boundary.

The framework talks to exactly one thing here, through one method. Keeping the
surface this narrow is what allows the Monarch and the Eminence to be tested
without a network, and what allows the provider to change without touching
either of them.

Request shape (Claude Opus 5, the default core):

* ``thinking={"type": "adaptive"}`` -- the model decides when and how deeply to
  reason. The old fixed ``budget_tokens`` dial is gone; passing it is rejected.
* ``output_config={"effort": ...}`` -- the spend dial, ``low`` through ``max``.
  Note it nests inside ``output_config``; it is not a top-level parameter.
* Streaming for anything long. A large ``max_tokens`` on a non-streaming
  request is how you collect an HTTP timeout instead of an answer.

Status: this module is the boundary definition. It is written against the
documented request shape but has not been exercised against the live API in
this build -- see the Implementory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Sequence, runtime_checkable

from ..config import LLMConfig
from ..core.errors import ConfigError, ShadowError


class ReasoningError(ShadowError):
    """The reasoning core could not be reached or refused the request."""


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
        happily process an empty answer as a real one.
        """
        return self.stop_reason == "refusal"


@runtime_checkable
class ReasoningCore(Protocol):
    """Everything the framework needs from an LLM."""

    def complete(
        self,
        system: str,
        messages: Sequence[Dict[str, Any]],
        tools: Optional[Sequence[Dict[str, Any]]] = None,
        max_tokens: Optional[int] = None,
    ) -> Reply:
        ...


def build_request(
    config: LLMConfig,
    system: str,
    messages: Sequence[Dict[str, Any]],
    tools: Optional[Sequence[Dict[str, Any]]] = None,
    max_tokens: Optional[int] = None,
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
    if config.effort:
        request["output_config"] = {"effort": config.effort}
    if tools:
        request["tools"] = list(tools)
    return request


class AnthropicCore:
    """Claude-backed reasoning core.

    The SDK is imported lazily so the rest of the framework -- onboarding,
    state, execution -- runs with no third-party dependency installed at all.
    """

    def __init__(self, config: Optional[LLMConfig] = None) -> None:
        self.config = config or LLMConfig()
        self.config.validate()
        self._client: Any = None

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import anthropic  # noqa: PLC0415 -- deliberate lazy import
            except ImportError as exc:
                raise ConfigError(
                    "the anthropic SDK is not installed; run `pip install anthropic` "
                    "or `pip install -e .[llm]`"
                ) from exc
            # A bare constructor is correct: the SDK resolves credentials from
            # ANTHROPIC_API_KEY, then ANTHROPIC_AUTH_TOKEN, then an `ant auth
            # login` profile on disk. An unset env var does not mean no key.
            self._client = anthropic.Anthropic()
        return self._client

    def complete(
        self,
        system: str,
        messages: Sequence[Dict[str, Any]],
        tools: Optional[Sequence[Dict[str, Any]]] = None,
        max_tokens: Optional[int] = None,
    ) -> Reply:
        request = build_request(self.config, system, messages, tools, max_tokens)
        try:
            if self.config.stream:
                with self.client.messages.stream(**request) as stream:
                    message = stream.get_final_message()
            else:
                message = self.client.messages.create(**request)
        except ConfigError:
            raise
        except Exception as exc:  # SDK exception hierarchy varies by version
            raise ReasoningError(f"reasoning core request failed: {exc}") from exc

        return Reply(
            text=_extract_text(message),
            model=getattr(message, "model", self.config.model),
            stop_reason=getattr(message, "stop_reason", "") or "",
            input_tokens=getattr(getattr(message, "usage", None), "input_tokens", 0) or 0,
            output_tokens=getattr(getattr(message, "usage", None), "output_tokens", 0) or 0,
            raw=message,
        )


def _extract_text(message: Any) -> str:
    """Concatenate the text blocks of a response, ignoring thinking blocks."""
    blocks = getattr(message, "content", None) or []
    parts: List[str] = []
    for block in blocks:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
    return "".join(parts).strip()
