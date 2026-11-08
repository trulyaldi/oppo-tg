"""Settings (env) and AppConfig (config.toml). Nothing here reads os.environ at import."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class MissingConfig(Exception):
    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__("missing required environment variables: " + ", ".join(missing))


@dataclass(frozen=True)
class EnvVar:
    name: str
    required: bool
    default: str | None
    description: str


DEFAULT_HTTP_CONTACT = "https://github.com/trulyaldi/oppo-tg"

ENV_VARS: list[EnvVar] = [
    EnvVar("TELEGRAM_BOT_TOKEN", True, None, "Bot token from @BotFather"),
    EnvVar("TELEGRAM_CHAT_ID", True, None, "Your chat id; the only chat the bot answers"),
    EnvVar("ANTHROPIC_API_KEY", True, None, "Claude API key used for scoring"),
    EnvVar("SCORING_MODEL", False, "claude-haiku-4-5", "Claude model id used for scoring"),
    EnvVar("DB_PATH", False, "data/oppo.db", "SQLite database path"),
    EnvVar("OPPO_CONFIG", False, "config.toml", "Path to the non-secret config file"),
    EnvVar("TIMEZONE", False, "Asia/Almaty", "IANA timezone for digest scheduling"),
    EnvVar("DIGEST_TIME", False, "09:00", "Daily digest time (HH:MM, in TIMEZONE)"),
    EnvVar("COLLECT_CRON_HOURS", False, "3", "ignored; see [scheduler] in config.toml"),
    EnvVar("MAX_SCORES_PER_RUN", False, "150", "Maximum LLM scoring calls per run"),
    EnvVar("LOG_LEVEL", False, "INFO", "Python logging level"),
    EnvVar(
        "HTTP_CONTACT",
        False,
        DEFAULT_HTTP_CONTACT,
        "Email or URL placed in the HTTP User-Agent",
    ),
    EnvVar("HH_ACCESS_TOKEN", False, None, "hh.kz API token, if required"),
    EnvVar("TG_API_ID", False, None, "Telegram API id for Telethon channel reading"),
    EnvVar("TG_API_HASH", False, None, "Telegram API hash for Telethon channel reading"),
    EnvVar("TG_SESSION_PATH", False, "data/telethon.session", "Telethon session file path"),
]


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: str
    anthropic_api_key: str
    scoring_model: str = "claude-haiku-4-5"
    db_path: str = "data/oppo.db"
    oppo_config: str = "config.toml"
    timezone: str = "Asia/Almaty"
    digest_time: str = "09:00"
    collect_cron_hours: int = 3
    max_scores_per_run: int = 150
    log_level: str = "INFO"
    http_contact: str = DEFAULT_HTTP_CONTACT
    hh_access_token: str | None = None
    tg_api_id: str | None = None
    tg_api_hash: str | None = None
    tg_session_path: str = "data/telethon.session"

    # Uppercase aliases matching the environment variable names.
    @property
    def TIMEZONE(self) -> str:  # noqa: N802
        return self.timezone

    @property
    def DIGEST_TIME(self) -> str:  # noqa: N802
        return self.digest_time

    @property
    def MAX_SCORES_PER_RUN(self) -> int:  # noqa: N802
        return self.max_scores_per_run

    @classmethod
    def from_env(cls, environ: Mapping[str, str], *, require_secrets: bool = True) -> Settings:
        def get(name: str) -> str | None:
            value = environ.get(name)
            return value.strip() if value and value.strip() else None

        missing = [v.name for v in ENV_VARS if v.required and get(v.name) is None]
        if missing and require_secrets:
            raise MissingConfig(missing)
        defaults = {v.name: v.default for v in ENV_VARS}

        def val(name: str) -> str:
            return get(name) or defaults[name] or ""

        return cls(
            telegram_bot_token=val("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=val("TELEGRAM_CHAT_ID"),
            anthropic_api_key=val("ANTHROPIC_API_KEY"),
            scoring_model=val("SCORING_MODEL"),
            db_path=val("DB_PATH"),
            oppo_config=val("OPPO_CONFIG"),
            timezone=val("TIMEZONE"),
            digest_time=val("DIGEST_TIME"),
            collect_cron_hours=int(val("COLLECT_CRON_HOURS")),
            max_scores_per_run=int(val("MAX_SCORES_PER_RUN")),
            log_level=val("LOG_LEVEL"),
            http_contact=val("HTTP_CONTACT"),
            hh_access_token=get("HH_ACCESS_TOKEN"),
            tg_api_id=get("TG_API_ID"),
            tg_api_hash=get("TG_API_HASH"),
            tg_session_path=val("TG_SESSION_PATH"),
        )


_FRESH_INTS = {"fresh_alert_max_per_day", "fresh_window_hours"}
TIERS = ("tier1", "tier2", "tier3")


def _check_number(label: str, value: Any, *, integer: bool = False) -> None:
    ok = not isinstance(value, bool) and isinstance(value, int if integer else int | float)
    if not ok or value <= 0:
        kind = "integer" if integer else "number"
        raise ValueError(f"{label} must be a positive {kind}")


@dataclass(frozen=True)
class Thresholds:
    digest_min_score: float = 5.0
    alert_score: float = 9.0
    alert_deadline_days: int = 7
    alert_deadline_min_score: float = 0.0
    fresh_alert_score: float = 7.5
    fresh_alert_max_per_day: int = 5
    fresh_window_hours: int = 48
    digest_score_band: float = 1.0

    def __post_init__(self) -> None:
        for name in (
            "fresh_alert_score",
            "fresh_alert_max_per_day",
            "fresh_window_hours",
            "digest_score_band",
        ):
            _check_number(f"[thresholds] {name}", getattr(self, name), integer=name in _FRESH_INTS)


# Caps where 0 is a legitimate "off" switch (the cap-reached replies are tested with 0).
_ZERO_OK = {"daily_llm_call_cap", "max_page_fetches_per_prepare", "max_page_fetches_per_day"}


@dataclass(frozen=True)
class OutreachConfig:
    cheap_model: str = "claude-haiku-4-5"
    tailor_model: str = "claude-sonnet-4-6"
    daily_llm_call_cap: int = 40
    max_page_fetches_per_prepare: int = 2
    max_page_fetches_per_day: int = 20
    max_contacts_per_posting: int = 10
    contact_ttl_days: int = 60
    rediscover_after_days: int = 7
    cv_max_bytes: int = 5_000_000
    cv_max_pages: int = 20
    cv_max_chars: int = 30000
    pending_input_minutes: int = 15

    def __post_init__(self) -> None:
        for name, f in self.__dataclass_fields__.items():
            value = getattr(self, name)
            if f.type == "str":
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"[outreach] {name} must be a non-empty string")
            elif isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"[outreach] {name} must be an integer")
            elif value < (0 if name in _ZERO_OK else 1):
                kind = "non-negative" if name in _ZERO_OK else "positive"
                raise ValueError(f"[outreach] {name} must be a {kind} integer")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> OutreachConfig:
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown outreach keys: {sorted(unknown)}")
        return cls(**data)

    @classmethod
    def from_cfg(cls, cfg: AppConfig) -> OutreachConfig:
        return cls.from_mapping(cfg.outreach)


@dataclass(frozen=True)
class ScoringConfig:
    daily_call_cap: int = 200  # 0 means "no scoring" (D-184)
    baseline_max_share: float = 0.5

    def __post_init__(self) -> None:
        cap = self.daily_call_cap
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
            raise ValueError("[scoring] daily_call_cap must be a non-negative integer")
        share = self.baseline_max_share
        if isinstance(share, bool) or not isinstance(share, int | float) or not 0 <= share <= 1:
            raise ValueError("[scoring] baseline_max_share must be a number between 0 and 1")

    @property
    def baseline_cap(self) -> int:
        return int(self.baseline_max_share * self.daily_call_cap)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ScoringConfig:
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown scoring keys: {sorted(unknown)}")
        return cls(**data)

    @classmethod
    def from_cfg(cls, cfg: AppConfig) -> ScoringConfig:
        return cls.from_mapping(cfg.scoring)


@dataclass(frozen=True)
class TriageConfig:
    """Cheap relevance pass before full scoring (D-196)."""

    enabled: bool = True
    model: str = "claude-haiku-4-5"  # D-183
    max_desc_chars: int = 500
    daily_call_cap: int = 600  # must be >= 1 while enabled; set enabled = false to skip
    baseline_max_share: float = 0.5
    exempt_sources: tuple[str, ...] = ("pagewatch",)

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("[triage] enabled must be a boolean")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("[triage] model must be a non-empty string")
        _check_number("[triage] max_desc_chars", self.max_desc_chars, integer=True)
        cap = self.daily_call_cap
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
            raise ValueError("[triage] daily_call_cap must be a non-negative integer")
        if self.enabled and cap == 0:
            raise ValueError(
                "[triage] daily_call_cap = 0 with triage enabled would stop all scoring; "
                "set [triage] enabled = false to skip triage"
            )
        share = self.baseline_max_share
        if isinstance(share, bool) or not isinstance(share, int | float) or not 0 <= share <= 1:
            raise ValueError("[triage] baseline_max_share must be a number between 0 and 1")
        ex = self.exempt_sources
        if not isinstance(ex, list | tuple) or not all(isinstance(x, str) for x in ex):
            raise ValueError("[triage] exempt_sources must be a list of strings")

    @property
    def baseline_cap(self) -> int:
        return int(self.baseline_max_share * self.daily_call_cap)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> TriageConfig:
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown triage keys: {sorted(unknown)}")
        d = dict(data)
        if isinstance(d.get("exempt_sources"), list):
            d["exempt_sources"] = tuple(d["exempt_sources"])
        return cls(**d)

    @classmethod
    def from_cfg(cls, cfg: AppConfig) -> TriageConfig:
        return cls.from_mapping(cfg.triage)


@dataclass(frozen=True)
class SchedulerConfig:
    tier1_minutes: int = 15
    tier2_minutes: int = 90
    tier3_minutes: int = 60
    tier1_jitter_s: int = 60
    tier2_jitter_s: int = 300
    tier3_jitter_s: int = 300
    no_validator_min_minutes: int = 60

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            _check_number(f"[scheduler] {name}", getattr(self, name), integer=True)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> SchedulerConfig:
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown scheduler keys: {sorted(unknown)}")
        return cls(**data)


@dataclass(frozen=True)
class PolitenessConfig:
    default_min_interval_s: float = 2.0
    jitter_fraction: float = 0.25
    max_concurrent_requests: int = 4
    backoff_base_s: int = 300
    backoff_max_s: int = 21600
    hosts: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_number("[politeness] default_min_interval_s", self.default_min_interval_s)
        _check_number(
            "[politeness] max_concurrent_requests", self.max_concurrent_requests, integer=True
        )
        _check_number("[politeness] backoff_base_s", self.backoff_base_s, integer=True)
        _check_number("[politeness] backoff_max_s", self.backoff_max_s, integer=True)
        _check_number("[politeness] jitter_fraction", self.jitter_fraction)
        if self.jitter_fraction > 1:
            raise ValueError("[politeness] jitter_fraction must be at most 1")
        if not isinstance(self.hosts, dict):
            raise ValueError("[politeness] hosts must be a table")
        for host, secs in self.hosts.items():
            _check_number(f"[politeness.hosts] {host}", secs)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> PolitenessConfig:
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown politeness keys: {sorted(unknown)}")
        return cls(**data)


@dataclass(frozen=True)
class DiscoveryConfig:
    enabled: bool = True
    boards_file: str = "data/discovered_boards.toml"
    max_validations_per_day: int = 40
    max_new_boards_per_day: int = 20
    negative_cache_days: int = 30
    run_time: str = "05:30"

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("[discovery] enabled must be a boolean")
        if not isinstance(self.boards_file, str) or not self.boards_file:
            raise ValueError("[discovery] boards_file must be a non-empty string")
        for name in ("max_validations_per_day", "max_new_boards_per_day", "negative_cache_days"):
            _check_number(f"[discovery] {name}", getattr(self, name), integer=True)
        if not isinstance(self.run_time, str) or not re.fullmatch(
            r"([01]\d|2[0-3]):[0-5]\d", self.run_time
        ):
            raise ValueError("[discovery] run_time must be HH:MM")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> DiscoveryConfig:
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown discovery keys: {sorted(unknown)}")
        return cls(**data)


@dataclass(frozen=True)
class AppConfig:
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    filters: dict[str, Any] = field(default_factory=dict)
    thresholds: Thresholds = field(default_factory=Thresholds)
    ranking: dict[str, Any] = field(default_factory=dict)
    outreach: dict[str, Any] = field(default_factory=dict)
    scoring: dict[str, Any] = field(default_factory=dict)
    triage: dict[str, Any] = field(default_factory=dict)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    politeness: PolitenessConfig = field(default_factory=PolitenessConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AppConfig:
        th = dict(data.get("thresholds", {}))
        for gone in ("digest_min_items", "digest_max_items"):  # D-79, D-118: old configs still load
            th.pop(gone, None)
        known = Thresholds.__dataclass_fields__
        unknown = set(th) - set(known)
        if unknown:
            raise ValueError(f"unknown thresholds: {sorted(unknown)}")
        sources = {k: dict(v) for k, v in dict(data.get("sources", {})).items()}
        outreach = dict(data.get("outreach", {}))
        OutreachConfig.from_mapping(outreach)  # fail fast at load (D-147)
        scoring = dict(data.get("scoring", {}))
        ScoringConfig.from_mapping(scoring)  # fail fast at load
        triage = dict(data.get("triage", {}))
        TriageConfig.from_mapping(triage)  # fail fast at load
        return cls(
            sources=sources,
            filters=dict(data.get("filters", {})),
            thresholds=Thresholds(**th),
            ranking=dict(data.get("ranking", {})),
            outreach=outreach,
            scoring=scoring,
            triage=triage,
            scheduler=SchedulerConfig.from_mapping(dict(data.get("scheduler", {}))),
            politeness=PolitenessConfig.from_mapping(dict(data.get("politeness", {}))),
            discovery=DiscoveryConfig.from_mapping(dict(data.get("discovery", {}))),
        )

    @classmethod
    def load(cls, path: str | Path) -> AppConfig:
        with open(path, "rb") as fh:
            return cls.from_dict(tomllib.load(fh))

    def source(self, name: str) -> dict[str, Any]:
        return self.sources.get(name, {})

    def source_enabled(self, name: str) -> bool:
        return bool(self.source(name).get("enabled", False))

    def source_tier(self, name: str, default: str) -> str:
        tier = self.source(name).get("tier", default)
        if tier not in TIERS:
            raise ValueError(f"[sources.{name}] tier must be one of {', '.join(TIERS)}")
        return tier
