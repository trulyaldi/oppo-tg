"""CLI entry point: collect / digest / once. Real Bot and Anthropic are built only here (D-05)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from src import config as config_mod
from src import db
from src.clock import SystemClock
from src.config import (
    AppConfig,
    OutreachConfig,
    ScoringConfig,
    Settings,
    TelegramDiscoveryConfig,
    TriageConfig,
)
from src.delivery.digest import send_digest
from src.http import build_client
from src.lock import AlreadyRunning, InstanceLock
from src.logging_setup import configure_logging
from src.pipeline.filters import load_filter_cues
from src.pipeline.run import (
    collect_all,
    collect_tier,
    pipeline_lock,
    process_collected,
    run_tier_cycle,
    score_pending,
)
from src.pipeline.scoring_alert import is_total_failure

log = logging.getLogger("oppo-tg")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="oppo-tg", description="Personal opportunity bot.")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="collect, dedupe, filter, score and send instant alerts")
    sub.add_parser("digest", help="send the digest now")
    sub.add_parser("once", help="collect, score, alert and send the digest")
    sub.add_parser("discover", help="find new ATS boards in collected postings and validate them")
    sub.add_parser("run", help="unattended mode: scheduler plus bot polling")
    cvr = sub.add_parser("cvreview", help="review the stored CV (or --cv-file) and send it")
    cvr.add_argument("--cv-file", default=None, help="ingest this text or PDF file first")
    probe = sub.add_parser("probe", help="run every enabled source once against the live network")
    probe.add_argument("--db", default="data/probe.db", help="separate probe DB path")
    return p


async def _run(command: str, settings: Settings) -> int:
    clock = SystemClock()
    cfg = AppConfig.load(settings.oppo_config)
    conn = db.connect(settings.db_path)
    db.migrate(conn)
    exit_code = 0
    try:
        if command == "discover":
            from src.discovery.boards import run_board_discovery

            http = build_client(
                conn, clock, contact=settings.http_contact, politeness=cfg.politeness
            )
            try:
                stats = await run_board_discovery(conn, http, cfg, clock, settings)
            finally:
                await http.aclose()
            log.info("discovery: %s", stats)
        if command in ("collect", "once"):
            http = build_client(
                conn, clock, contact=settings.http_contact, politeness=cfg.politeness
            )
            try:
                stats = await collect_all(conn, http, settings, cfg, clock)
            finally:
                await http.aclose()
            log.info("collected: %s", stats)
            from aiogram import Bot
            from anthropic import AsyncAnthropic

            from src.delivery.alerts import send_alerts

            client = AsyncAnthropic(api_key=settings.anthropic_api_key)
            bot = Bot(settings.telegram_bot_token)
            try:
                scored = await score_pending(
                    conn,
                    client,
                    settings.scoring_model,
                    clock,
                    settings.max_scores_per_run,
                    bot=bot,
                    chat_id=settings.telegram_chat_id,
                    scoring_cfg=ScoringConfig.from_cfg(cfg),
                    triage_cfg=TriageConfig.from_cfg(cfg),
                    cues=load_filter_cues(cfg),
                )
                log.info("scored: %s", scored)
                if is_total_failure(scored):
                    log.error("scoring failed for every posting; exiting non-zero (S2)")
                    exit_code = 1
                n_alerts = await send_alerts(bot, conn, settings, cfg, clock)
            finally:
                await bot.session.close()
            log.info("alerts sent: %d", n_alerts)
        if command in ("digest", "once"):
            from aiogram import Bot

            bot = Bot(settings.telegram_bot_token)
            try:
                n = await send_digest(bot, conn, settings, cfg, clock)
            finally:
                await bot.session.close()
            log.info("digest sent: %d items", n)
    finally:
        conn.close()
    return exit_code


async def run_until_stopped(
    scheduler: Any, polling: Callable[[], Awaitable[Any]], stop: asyncio.Event
) -> None:
    """Run scheduler and bot polling in one loop until `stop` is set (graceful shutdown)."""
    scheduler.start()
    poll_task = asyncio.ensure_future(polling())

    def _poll_done(t: asyncio.Future) -> None:
        if not t.cancelled() and t.exception() is not None:
            log.error("bot polling stopped: %r (scheduler keeps running)", t.exception())

    poll_task.add_done_callback(_poll_done)
    try:
        await stop.wait()
    finally:
        scheduler.shutdown(wait=False)
        poll_task.cancel()
        await asyncio.gather(poll_task, return_exceptions=True)


async def _noop() -> None:
    return None


async def _serve(settings: Settings) -> None:
    from aiogram import Bot
    from anthropic import AsyncAnthropic

    from src.bot.app import build_dispatcher
    from src.delivery.alerts import send_alerts
    from src.scheduler import SchedulerDeps, build_scheduler, tiers_from_config

    clock = SystemClock()
    cfg = AppConfig.load(settings.oppo_config)
    conn = db.connect(settings.db_path)
    db.migrate(conn)
    http = build_client(conn, clock, contact=settings.http_contact, politeness=cfg.politeness)
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    bot = Bot(settings.telegram_bot_token)

    scoring_cfg = ScoringConfig.from_cfg(cfg)
    triage_cfg = TriageConfig.from_cfg(cfg)

    async def collect_t(tier: str) -> None:
        stats = await collect_tier(conn, http, settings, cfg, clock, tier, process=False)
        log.info("collected %s: %s", tier, stats)

    async def process() -> None:
        log.info("processed: %s", process_collected(conn, cfg, clock))

    async def score() -> None:
        log.info(
            "scored: %s",
            await score_pending(
                conn,
                client,
                settings.scoring_model,
                clock,
                settings.max_scores_per_run,
                bot=bot,
                chat_id=settings.telegram_chat_id,
                scoring_cfg=scoring_cfg,
                triage_cfg=triage_cfg,
                cues=load_filter_cues(cfg),
            ),
        )

    async def run_tier(tier: str) -> None:
        await run_tier_cycle(
            tier, collect=lambda: collect_t(tier), process=process, score=score, alerts=alerts
        )

    async def alerts() -> None:
        log.info("alerts sent: %d", await send_alerts(bot, conn, settings, cfg, clock))

    async def digest() -> None:
        async with pipeline_lock():
            log.info("digest sent: %d items", await send_digest(bot, conn, settings, cfg, clock))

    async def discover() -> None:
        from src.discovery.boards import run_board_discovery

        log.info("discovery: %s", await run_board_discovery(conn, http, cfg, clock, settings))

    async def tg_discover() -> None:
        from src.discovery.telegram_flow import weekly_discovery

        await weekly_discovery(bot, conn, settings, cfg, clock, load_filter_cues(cfg))

    async def prune() -> None:
        log.info("pruned: %s", db.prune(conn, clock.now()))

    async def live_flush(batch: list) -> None:
        log.info("live telegram: %d new messages", len(batch))
        await run_tier_cycle("live", collect=_noop, process=process, score=score, alerts=alerts)

    from src.sources.telegram_channels import set_listener
    from src.sources.telegram_live import TelegramListener

    listener = TelegramListener(settings, cfg, conn, on_new=live_flush, clock=clock)
    set_listener(listener)

    tgd_cfg = TelegramDiscoveryConfig.from_cfg(cfg)
    scheduler = build_scheduler(
        SchedulerDeps(
            clock=clock,
            run_tier=run_tier,
            tiers=tiers_from_config(cfg.scheduler),
            digest=digest,
            prune=prune,
            timezone=settings.timezone,
            digest_time=settings.digest_time,
            discover=discover if cfg.discovery.enabled else None,
            discover_time=cfg.discovery.run_time,
            tg_discover=tg_discover if tgd_cfg.enabled else None,
            tg_discover_day=tgd_cfg.weekly_day,
            tg_discover_time=tgd_cfg.run_time,
        )
    )
    dp = build_dispatcher(conn, settings, cfg, clock, client, http)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # non-Unix
            loop.add_signal_handler(sig, stop.set)
    try:
        await listener.start()  # False (logged once) falls back to polling; never raises
        await run_until_stopped(
            scheduler, lambda: dp.start_polling(bot, handle_signals=False), stop
        )
    finally:
        set_listener(None)
        await listener.stop()
        await http.aclose()
        await bot.session.close()
        conn.close()
        log.info("shut down")


def _read_bytes(path: str) -> bytes:
    return Path(path).read_bytes()


class _ChatTarget:
    """Message-like adapter so the bot handlers' senders work for the CLI (one chat)."""

    def __init__(self, bot: Any, chat_id: int) -> None:
        self.bot, self.chat_id = bot, chat_id

    async def answer(self, text: str, **kw: Any) -> None:
        await self.bot.send_message(self.chat_id, text, **kw)

    async def answer_document(self, document: Any, **kw: Any) -> None:
        await self.bot.send_document(self.chat_id, document, **kw)


class _Sink:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def answer(self, text: str, **kw: Any) -> None:
        self.sent.append(text)


async def run_cvreview(
    settings: Settings, cv_file: str | None, bot: Any, client: Any, clock: Any = None
) -> int:
    """CLI: optionally ingest a CV file, then send the review to the chat. Prints only counts.

    Use DB_PATH to point at a scratch database so a fictional CV never enters the real one.
    """
    from src.bot import outreach_handlers as oh
    from src.cv.extract import CvInputError, pdf_to_text
    from src.cv.store import current_cv

    clock = clock or SystemClock()
    cfg = AppConfig.load(settings.oppo_config)
    conn = db.connect(settings.db_path)
    db.migrate(conn)
    try:
        if cv_file:
            is_pdf = cv_file.lower().endswith(".pdf")
            try:
                raw = await asyncio.to_thread(_read_bytes, cv_file)
                if is_pdf:
                    oc = OutreachConfig.from_cfg(cfg)
                    text, kind = pdf_to_text(raw, oc.cv_max_pages), "pdf"
                else:
                    text, kind = raw.decode("utf-8"), "text"
            except (OSError, UnicodeDecodeError, CvInputError) as e:
                print(f"cannot read CV file: {type(e).__name__}", file=sys.stderr)
                return 1
            sink = _Sink()
            await oh.ingest_cv_text(
                sink, conn, clock, cfg, client, text, source_kind=kind, auto_review=False
            )
            for line in sink.sent:
                print(line)
        if current_cv(conn) is None:
            print("no CV stored", file=sys.stderr)
            return 1
        target = _ChatTarget(bot, settings.telegram_chat_id)
        res = await oh.run_review(target, conn, clock, cfg, client)
        if isinstance(res, oh.ReviewError):
            print(f"review failed: {res.kind}", file=sys.stderr)
            return 1
        print(f"cv review v{res.cv_version}: removed {res.removed}, questions {res.questions}")
        return 0
    finally:
        conn.close()


async def _cvreview(settings: Settings, cv_file: str | None) -> int:
    from aiogram import Bot
    from anthropic import AsyncAnthropic

    bot = Bot(settings.telegram_bot_token)
    try:
        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        return await run_cvreview(settings, cv_file, bot, client)
    finally:
        await bot.session.close()


async def _probe(settings: Settings, db_path: str) -> int:
    import src.sources  # noqa: F401,I001  (registers the sources)
    from src.pipeline.probe import reset_probe_state, run_probe

    clock = SystemClock()
    cfg = AppConfig.load(settings.oppo_config)
    conn = db.connect(db_path)
    db.migrate(conn)
    reset_probe_state(conn)  # pagewatch must see its pages as new on every probe run (FIX-01)
    http = build_client(conn, clock, contact=settings.http_contact, politeness=cfg.politeness)
    try:
        return await run_probe(http, settings, cfg, clock, sys.stdout)
    finally:
        await http.aclose()
        conn.close()


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if environ is None:
        from dotenv import load_dotenv

        load_dotenv()
        environ = os.environ
    is_probe = args.command == "probe"
    try:
        settings = config_mod.Settings.from_env(
            environ, require_secrets=args.command not in ("probe", "discover")
        )
    except config_mod.MissingConfig as exc:
        print(f"Missing required environment variables: {', '.join(exc.missing)}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level)
    if is_probe:
        return asyncio.run(_probe(settings, args.db))
    if args.command == "cvreview":
        return asyncio.run(_cvreview(settings, args.cv_file))
    if args.command == "run":
        lock = InstanceLock(Path(settings.db_path).with_suffix(".lock"))
        try:
            lock.acquire()
        except AlreadyRunning as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 3
        try:
            asyncio.run(_serve(settings))
        finally:
            lock.release()
        return 0
    return asyncio.run(_run(args.command, settings))


if __name__ == "__main__":
    sys.exit(main())
