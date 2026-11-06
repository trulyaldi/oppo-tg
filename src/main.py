"""CLI entry point: collect / digest / once. Real Bot and Anthropic are built only here (D-05)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

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


async def _probe(settings: Settings, db_path: str) -> int:
    import src.sources  # noqa: F401,I001  (registers the sources)
    from src.pipeline.probe import run_probe

    clock = SystemClock()
    cfg = AppConfig.load(settings.oppo_config)
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(db_path)
    db.migrate(conn)
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
    asyncio.run(_run(args.command, settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
