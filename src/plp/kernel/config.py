"""Configuration: ``config/plp.yaml`` → validated, typed, path-resolved (PRD.md §6.2).

The config file lives at ``<root>/config/plp.yaml``; all relative paths in it
are resolved against the project root (the config directory's parent).
"""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ConfigError(Exception):
    """Configuration is missing or invalid."""


class LLMConfig(BaseModel):
    """Self-hosted LLM ("the brain") — any OpenAI-compatible server."""

    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = "qwen3.8-27b"
    api_key: str = ""
    timeout_seconds: float = 120.0
    max_tool_steps: int = 8


class EmailDelivery(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = False
    sender: str = Field(default="", alias="from")
    to: str = ""
    smtp_url: str = ""


class DeliveryConfig(BaseModel):
    terminal: bool = True
    email: EmailDelivery = Field(default_factory=EmailDelivery)


class VaultConfig(BaseModel):
    path: str = "plp-vault"


class StateDbConfig(BaseModel):
    path: str = "data/plp.db"


class PluginsConfig(BaseModel):
    dir: str = "plugins"


class FeaturesConfig(BaseModel):
    email_summarization: bool = False


class PlpConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    timezone: str = ""
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)
    vault: VaultConfig = Field(default_factory=VaultConfig)
    state_db: StateDbConfig = Field(default_factory=StateDbConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    schedules: dict[str, str] = Field(default_factory=dict)
    features: FeaturesConfig = Field(default_factory=FeaturesConfig)
    # Set by load_config(): the project root (<root>/config/plp.yaml).
    root: Path = Field(default=Path("."))

    def tzinfo(self) -> ZoneInfo | None:
        if not self.timezone:
            return None
        try:
            return ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:  # type: ignore[misc]
            raise ConfigError(f"unknown timezone {self.timezone!r}") from exc

    def default_now_factory(self):
        """A ``now`` function in the configured timezone (system-local if unset)."""
        tz = self.tzinfo()
        import datetime as _dt

        def now() -> _dt.datetime:
            if tz is not None:
                return _dt.datetime.now(tz)
            return _dt.datetime.now().astimezone()

        return now


def load_config(path: str | Path) -> PlpConfig:
    """Load and validate the config file. Raises ConfigError with a clear message."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(
            f"config file not found: {path} (pass --config or set PLP_CONFIG)"
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping, got {type(raw).__name__}")
    try:
        cfg = PlpConfig(**raw)
    except Exception as exc:  # pydantic ValidationError & co
        raise ConfigError(f"invalid config in {path}: {exc}") from exc
    cfg.root = path.resolve().parent.parent
    return cfg


def resolve(cfg: PlpConfig, rel: str) -> Path:
    """Resolve a (possibly relative) config path against the project root."""
    p = Path(rel)
    return p if p.is_absolute() else cfg.root / p
