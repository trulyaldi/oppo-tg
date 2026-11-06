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
