"""Source interface, context, registry and the failure-isolating runner (PLAN 1.5)."""

from __future__ import annotations

import json
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
    revalidate: bool = False
    # (source, target key) -> has_validators, reported by board sources (FC-08).
    validators: dict[tuple[str, str], bool] = field(default_factory=dict)

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


@dataclass(frozen=True)
class Target:
    """One fetchable unit of a source: a board, channel, feed or page (D-186)."""

    source: str
    key: str
    company: str = ""
    origin: str = "config"
    tier: str = ""


@dataclass(frozen=True)
class TargetResult:
    source: str
    target: Target
    ok: bool
    items: list[Posting]
    error: str | None = None
    has_validators: bool | None = None


class SourceUnavailable(Exception):
    """Every request of a run failed (blocked, down, robots-disallowed): FIX-02."""


@dataclass
class RequestTally:
    """Counts sub-request outcomes so a total failure is not recorded as ok (FIX-02).

    Partial failure stays ok; "fetched but nothing new" counts as success.
    """

    total: int = 0
    failed: int = 0

    def success(self) -> None:
        self.total += 1

    def failure(self) -> None:
        self.total += 1
        self.failed += 1

    def raise_if_all_failed(self) -> None:
        if self.total and self.failed == self.total:
            raise SourceUnavailable(f"all {self.total} requests failed: no data")


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


def source_targets(src: Source, ctx: SourceContext) -> list[Target]:
    """The source's own targets, or one empty-key target for single-endpoint sources."""
    fn = getattr(src, "targets", None)
    if callable(fn):
        return list(fn(ctx))
    return [Target(src.name, "")]


async def fetch_all_targets(src: Source, ctx: SourceContext) -> list[Posting]:
    """`fetch` for target sources: every target in turn, one bad target never stops the rest."""
    tally = RequestTally()
    items: list[Posting] = []
    for target in source_targets(src, ctx):
        try:
            items.extend(await src.fetch_target(ctx, target))  # type: ignore[attr-defined]
            tally.success()
        except Exception:  # noqa: BLE001 - isolation boundary
            ctx.log.exception("%s target %r failed", src.name, target.key)
            tally.failure()
    tally.raise_if_all_failed()
    return items


async def run_target(src: Source, ctx: SourceContext, target: Target) -> TargetResult:
    """Fetch one target; never raises."""
    try:
        fn = getattr(src, "fetch_target", None)
        items = await fn(ctx, target) if callable(fn) else await src.fetch(ctx)
    except Exception as exc:  # noqa: BLE001 - isolation boundary
        ctx.log.exception("source %s target %r failed", src.name, target.key)
        ctx.validators.pop((src.name, target.key), None)
        return TargetResult(src.name, target, False, [], f"{type(exc).__name__}: {exc}")
    hv = ctx.validators.pop((src.name, target.key), None)
    return TargetResult(src.name, target, True, list(items), has_validators=hv)


async def fetch_board_json(ctx: SourceContext, target: Target, url: str, params: dict) -> object:
    """Fetch a board's JSON (revalidating for tier1) and note its has_validators.

    Returns None on any failure. A 304 returns the cached body, so it is parsed again
    and last_seen_at stays current (FC-08).
    """
    res = await ctx.http.fetch(url, params=params, ttl_s=3600.0, revalidate=ctx.revalidate)
    if res.text is None:
        return None
    ctx.validators[(target.source, target.key)] = res.has_validators
    try:
        return json.loads(res.text)
    except ValueError:
        ctx.log.warning("invalid JSON from %s", url)
        return None


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
