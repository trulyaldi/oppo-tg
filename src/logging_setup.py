"""Logging setup (D-141). Third-party SDK loggers stay at WARNING whatever LOG_LEVEL says."""

from __future__ import annotations

import logging

# At DEBUG these log full request bodies (CV, posting, contact text) or request lines.
QUIET_LOGGERS = ("anthropic", "httpx", "httpx2", "httpcore", "aiogram.event", "telethon")


def configure_logging(level: str | int) -> None:
    logging.basicConfig(level=level)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
