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
    # Passed through verbatim to the server request body (llama.cpp-specific
    # knobs such as ``{enable_thinking: false}`` for Qwen3-style templates).
    # Ignored by servers that don't know the key.
    chat_template_kwargs: dict[str, object] = Field(default_factory=dict)


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


class NewsSourceCfg(BaseModel):
    name: str
    url: str
    kind: str = "rss"  # rss | html
    weight: float = 1.0


class NewsConfig(BaseModel):
    """News collector (Phase 2). ``sources`` empty → built-in AI-focused list."""

    max_age_hours: float = 72.0
    per_source_limit: int = 25
    digest_max_items: int = 8
    digest_window_hours: float = 48.0
    sources: list[NewsSourceCfg] = Field(default_factory=list)


class OccasionCfg(BaseModel):
    """A recurring date the gifts review tracks (``day`` may not exist in some
    years — those occurrences are skipped)."""

    name: str
    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)


class GiftsConfig(BaseModel):
    """Gift vault (Phase 3). Gift records live in the vault; occasions are
    calendar dates (config-driven, collected via the onboarding interview)."""

    review_window_days: int = Field(default=90, ge=1, le=365)
    stale_after_days: int = Field(default=30, ge=1)
    occasions: list[OccasionCfg] = Field(default_factory=list)


class TravelConfig(BaseModel):
    """Holiday planner (Phase 3). Plans are vault docs; preferences are a
    human-edited vault file the brainstorm reads."""

    max_budget: float = Field(default=0.0, ge=0.0)  # 0 = no ceiling stated
    max_trip_days: int = Field(default=14, ge=1, le=60)
    preferences: str = "travel/preferences.md"  # relative to the vault root


class GoogleCalendarCfg(BaseModel):
    """Google backend (Phase-4 scaffold). Inert until the owner completes
    docs/google-calendar-setup.md and runs ``plp calendar connect`` — then a
    missing/broken credentials file falls back to ICS with a warning, never
    a failure."""

    enabled: bool = False
    credentials_file: str = ""  # JSON: client_id, client_secret[, refresh_token, calendar_id after connect]


class CalendarConfig(BaseModel):
    """Calendar spine (PRD.md §1). Backend is the ICS file now, the Google
    scaffold when enabled; ``week_start`` drives ``plp calendar week``."""

    backend: str = "ics"  # ics | google
    ics_file: str = "data/calendar/main.ics"
    week_start: int = Field(default=0, ge=0, le=6)  # 0=Monday … 6=Sunday (Python weekday)
    categories: list[str] = Field(default_factory=list)  # suggested labels (informational)
    google: GoogleCalendarCfg = Field(default_factory=GoogleCalendarCfg)


class ScorecardConfig(BaseModel):
    """Life scorecard + weekly checkup (Phase 5). Goals live in the vault
    (``goals_file``); the scorecard time series live in the state DB. The
    checkup's cadence defaults to Sunday 20:00 (PRD.md §11)."""

    week_start: int = Field(default=0, ge=0, le=6)  # 0=Monday … 6=Sunday (Python weekday)
    goals_file: str = "goals.md"  # relative to the vault root
    checkup_cron: str = "0 20 * * 0"  # Sunday 20:00
    proposal_max: int = Field(default=3, ge=1, le=5)
    history_weeks: int = Field(default=26, ge=2, le=156)
    # Categories ranked most-personal first; the checkup proposes for the
    # under-targeted goals in this order (work categories last).
    personal_categories: list[str] = Field(
        default_factory=lambda: ["wife", "family", "gifts", "travel"]
    )


class EmailConfig(BaseModel):
    """Email scanner (Phase 6): read-only Gmail triage. ``credentials_file``
    is the Google OAuth client JSON; ``token_file`` (local, gitignored) holds
    the refresh token after ``plp email connect``. LLM thread summarization
    is a separate opt-in flag: ``features.email_summarization``."""

    credentials_file: str = ""  # empty = Gmail not connected → email.scan is a no-op
    token_file: str = "data/email/token.json"
    scan_days: int = Field(default=2, ge=1, le=14)  # look-back window per scan
    scan_cron: str = "0 7 * * *"  # daily 07:00 (PRD.md §11 cadence)
    max_items: int = Field(default=25, ge=1, le=200)  # per-run fetch cap
    life_keywords: list[str] = Field(default_factory=list)  # extra life-relevant terms


class PlpConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    timezone: str = ""
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)
    vault: VaultConfig = Field(default_factory=VaultConfig)
    state_db: StateDbConfig = Field(default_factory=StateDbConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    schedules: dict[str, str] = Field(default_factory=dict)
    features: FeaturesConfig = Field(default_factory=FeaturesConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)
    calendar: CalendarConfig = Field(default_factory=CalendarConfig)
    gifts: GiftsConfig = Field(default_factory=GiftsConfig)
    travel: TravelConfig = Field(default_factory=TravelConfig)
    scorecard: ScorecardConfig = Field(default_factory=ScorecardConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
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
