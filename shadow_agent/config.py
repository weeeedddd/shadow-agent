"""Configuration: defaults, load, save, and env overlay.

Precedence, lowest to highest:

    built-in defaults  ->  .shadow/config.json  ->  SHADOW_* environment vars

The API key is deliberately absent from this file and from ``config.json``.
Credentials belong in the environment or in the SDK's own credential store,
never in a file the Architect might commit.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .core.errors import ConfigError

STATE_DIRNAME = ".shadow"
CONFIG_FILENAME = "config.json"

# Model defaults. `claude-opus-5` is the current reasoning core: 1M context
# window, adaptive thinking, and effort controlled through `output_config`
# rather than a fixed token budget (`budget_tokens` is rejected on this model).
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 16000
DEFAULT_EFFORT = "high"


@dataclass
class LLMConfig:
    """Reasoning-core settings.

    ``thinking`` is adaptive: the model decides when and how deeply to reason.
    ``effort`` is the dial that governs spend -- ``low`` for trivial routing,
    ``xhigh`` for long agentic runs, ``max`` when correctness outranks cost.
    """

    provider: str = "anthropic"
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    effort: str = DEFAULT_EFFORT
    thinking: str = "adaptive"
    stream: bool = True
    api_key_env: str = "ANTHROPIC_API_KEY"

    def validate(self) -> None:
        if self.effort not in ("low", "medium", "high", "xhigh", "max"):
            raise ConfigError(f"unknown effort level: {self.effort!r}")
        if self.thinking not in ("adaptive", "disabled"):
            raise ConfigError(f"unknown thinking mode: {self.thinking!r}")
        if self.max_tokens < 256:
            raise ConfigError("max_tokens must be at least 256")


@dataclass
class ExecutionConfig:
    """Eminence execution limits."""

    timeout_seconds: float = 120.0
    max_output_bytes: int = 200_000
    confirm_destructive: bool = True
    shell: Optional[str] = None  # None -> platform default


@dataclass
class UIConfig:
    """Interface preferences."""

    width: Optional[int] = None  # None -> auto-fit the terminal
    color: Optional[bool] = None  # None -> auto-detect
    ascii_only: bool = False
    show_implementory: bool = True


@dataclass
class Config:
    """The full resolved configuration."""

    llm: LLMConfig = field(default_factory=LLMConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    context_scan_depth: int = 2
    context_scan_limit: int = 400
    journal_enabled: bool = True
    version: int = 1

    # --- persistence ---------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        """Build from a dict, ignoring unknown keys rather than exploding.

        Forward compatibility matters here: a config written by a newer build
        must not brick an older one.
        """
        def subset(target, payload):
            if not isinstance(payload, dict):
                return target()
            allowed = {f for f in target.__dataclass_fields__}
            return target(**{k: v for k, v in payload.items() if k in allowed})

        top = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        top.pop("llm", None)
        top.pop("execution", None)
        top.pop("ui", None)
        return cls(
            llm=subset(LLMConfig, data.get("llm", {})),
            execution=subset(ExecutionConfig, data.get("execution", {})),
            ui=subset(UIConfig, data.get("ui", {})),
            **top,
        )

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)  # atomic: never leave a half-written config
        return path

    @classmethod
    def load(cls, path: Path) -> "Config":
        path = Path(path)
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"could not read {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{path} does not contain a JSON object")
        return cls.from_dict(data)

    # --- environment overlay -------------------------------------------------

    def apply_env(self, env: Optional[Dict[str, str]] = None) -> "Config":
        """Overlay ``SHADOW_*`` environment variables. Returns self."""
        env = env if env is not None else dict(os.environ)

        model = env.get("SHADOW_MODEL")
        if model:
            self.llm.model = model
        effort = env.get("SHADOW_EFFORT")
        if effort:
            self.llm.effort = effort
        width = env.get("SHADOW_WIDTH")
        if width and width.isdigit():
            self.ui.width = int(width)
        if env.get("SHADOW_ASCII"):
            self.ui.ascii_only = True
        if env.get("SHADOW_NO_COLOR") or env.get("NO_COLOR") is not None:
            self.ui.color = False
        timeout = env.get("SHADOW_TIMEOUT")
        if timeout:
            try:
                self.execution.timeout_seconds = float(timeout)
            except ValueError:
                pass
        return self

    def missing_variables(self) -> List[str]:
        """Report what the framework still needs before it can reason.

        Returned as a list of human-readable strings so the Implementory can
        print unresolved variables instead of the framework failing silently.
        """
        missing: List[str] = []
        if not os.environ.get(self.llm.api_key_env):
            missing.append(
                f"{self.llm.api_key_env} is unset -- the reasoning core cannot be reached"
            )
        return missing


def state_dir_for(root: Path) -> Path:
    return Path(root) / STATE_DIRNAME


def config_path_for(root: Path) -> Path:
    return state_dir_for(root) / CONFIG_FILENAME


def load_config(root: Path) -> Config:
    """Load configuration for a project root, with the env overlay applied."""
    return Config.load(config_path_for(root)).apply_env()
