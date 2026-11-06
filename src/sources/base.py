"""Source interface, context, registry and the failure-isolating runner (PLAN 1.5)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from src.clock import Clock
from src.config import AppConfig, Settings
from src.http import PoliteClient
from src.models import Posting


class Source(Protocol):
    name: str
    description: str

    def enabled(self, settings: Settings, cfg: AppConfig) -> bool: ...

    async def fetch(self, ctx: SourceContext) -> list[Posting]: ...


@dataclass
class SourceContext:
    http: PoliteClient
    settings: Settings
    cfg: AppConfig
    clock: Clock
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("src.sources"))

    def __post_init__(self) -> None:
        # D-34: without db+clock the client silently skips robots checks and caching.
        if self.http.db is None or self.http.clock is None:
            raise ValueError(
                "SourceContext requires a PoliteClient built with db and clock "
                "(use src.http.build_client) so robots.txt and caching are active"
            )


@dataclass(frozen=True)
class SourceResult:
    name: str
    ok: bool
    items: list[Posting]
    error: str | None = None


REGISTRY: dict[str, Callable[[], Source]] = {}


def register(name: str) -> Callable[[Callable[[], Source]], Callable[[], Source]]:
    def deco(factory: Callable[[], Source]) -> Callable[[], Source]:
        if name in REGISTRY:
            raise ValueError(f"source {name!r} already registered")
        REGISTRY[name] = factory
        return factory

    return deco


async def run_source(src: Source, ctx: SourceContext) -> SourceResult:
    """Run one source; never raises (a failing source must not hurt the pipeline)."""
    try:
        items = await src.fetch(ctx)
    except Exception as exc:  # noqa: BLE001 - isolation boundary
        ctx.log.exception("source %s failed", src.name)
        return SourceResult(src.name, False, [], f"{type(exc).__name__}: {exc}")
    return SourceResult(src.name, True, list(items))


MIN_SOURCE_FAMILIES = 6  # D-52


def source_family(src: Source) -> str:
    return str(getattr(src, "family", src.name))


def source_credentials(src: Source) -> tuple[str, ...]:
    return tuple(getattr(src, "credentials", ()))


@dataclass
class CountReport:
    counted: dict[str, list[str]] = field(default_factory=dict)
    credential_gated: dict[str, tuple[str, ...]] = field(default_factory=dict)
    disabled: dict[str, str] = field(default_factory=dict)


def count_report(settings: Settings, cfg: AppConfig) -> CountReport:
    """Classify registered sources per PLAN 1.5.1 (D-52); test-file checks live in tests."""
    report = CountReport()
    for name in sorted(REGISTRY):
        src = REGISTRY[name]()
        creds = source_credentials(src)
        if creds:
            report.credential_gated[name] = creds
        elif not cfg.source_enabled(name) or not src.enabled(settings, cfg):
            reason = "disabled in config.toml"
            fn = getattr(src, "disabled_reason", None)
            if callable(fn):
                reason = fn(settings, cfg) or reason
            report.disabled[name] = reason
        else:
            report.counted.setdefault(source_family(src), []).append(name)
    return report
