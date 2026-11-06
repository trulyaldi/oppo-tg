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
from typing import Any

from src import config as config_mod
from src import db
from src.clock import SystemClock
from src.config import AppConfig, Settings
from src.delivery.digest import send_digest
from src.http import build_client
from src.pipeline.run import collect_all, score_pending

log = logging.getLogger("oppo-tg")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="oppo-tg", description="Personal opportunity bot.")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="collect, dedupe, filter and score postings")
    sub.add_parser("digest", help="send the digest now")
    sub.add_parser("once", help="collect, score and send the digest")
    sub.add_parser("run", help="unattended mode: scheduler plus bot polling")
    probe = sub.add_parser("probe", help="run every enabled source once against the live network")
    probe.add_argument("--db", default="data/probe.db", help="separate probe DB path")
    return p


async def _run(command: str, settings: Settings) -> None:
    clock = SystemClock()
    cfg = AppConfig.load(settings.oppo_config)
    conn = db.connect(settings.db_path)
    db.migrate(conn)
    try:
        if command in ("collect", "once"):
            http = build_client(conn, clock, contact=settings.http_contact)
            try:
                stats = await collect_all(conn, http, settings, cfg, clock)
            finally:
                await http.aclose()
            log.info("collected: %s", stats)
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(api_key=settings.anthropic_api_key)
            scored = await score_pending(
                conn, client, settings.scoring_model, clock, settings.max_scores_per_run
            )
            log.info("scored: %s", scored)
            from aiogram import Bot

            from src.delivery.alerts import send_alerts

            bot = Bot(settings.telegram_bot_token)
            try:
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


async def _serve(settings: Settings) -> None:
    from aiogram import Bot
    from anthropic import AsyncAnthropic

    from src.bot.app import build_dispatcher
    from src.delivery.alerts import send_alerts
    from src.scheduler import SchedulerDeps, build_scheduler

    clock = SystemClock()
    cfg = AppConfig.load(settings.oppo_config)
    conn = db.connect(settings.db_path)
    db.migrate(conn)
    http = build_client(conn, clock, contact=settings.http_contact)
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    bot = Bot(settings.telegram_bot_token)

    async def collect() -> None:
        log.info("collected: %s", await collect_all(conn, http, settings, cfg, clock))

    async def score() -> None:
        log.info(
            "scored: %s",
            await score_pending(
                conn, client, settings.scoring_model, clock, settings.max_scores_per_run
            ),
        )

    async def alerts() -> None:
        log.info("alerts sent: %d", await send_alerts(bot, conn, settings, cfg, clock))

    async def digest() -> None:
        log.info("digest sent: %d items", await send_digest(bot, conn, settings, cfg, clock))

    async def prune() -> None:
        log.info("pruned: %s", db.prune(conn, clock.now()))

    scheduler = build_scheduler(
        SchedulerDeps(
            clock=clock,
            collect=collect,
            score=score,
            alerts=alerts,
            digest=digest,
            prune=prune,
            timezone=settings.timezone,
            digest_time=settings.digest_time,
            collect_hours=settings.collect_cron_hours,
        )
    )
    dp = build_dispatcher(conn, settings, cfg, clock)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # non-Unix
            loop.add_signal_handler(sig, stop.set)
    try:
        await run_until_stopped(
            scheduler, lambda: dp.start_polling(bot, handle_signals=False), stop
        )
    finally:
        await http.aclose()
        await bot.session.close()
        conn.close()
        log.info("shut down")


async def _probe(settings: Settings, db_path: str) -> int:
    import src.sources  # noqa: F401,I001  (registers the sources)
    from src.pipeline.probe import reset_probe_state, run_probe

    clock = SystemClock()
    cfg = AppConfig.load(settings.oppo_config)
    conn = db.connect(db_path)
    db.migrate(conn)
    reset_probe_state(conn)  # pagewatch must see its pages as new on every probe run (FIX-01)
    http = build_client(conn, clock, contact=settings.http_contact)
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
        settings = config_mod.Settings.from_env(environ, require_secrets=not is_probe)
    except config_mod.MissingConfig as exc:
        print(f"Missing required environment variables: {', '.join(exc.missing)}", file=sys.stderr)
        return 2
    logging.basicConfig(level=settings.log_level)
    if is_probe:
        return asyncio.run(_probe(settings, args.db))
    if args.command == "run":
        asyncio.run(_serve(settings))
        return 0
    asyncio.run(_run(args.command, settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
