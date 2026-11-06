"""Logging setup (D-141, D-154). Third-party SDK loggers stay at WARNING whatever LOG_LEVEL says."""

from __future__ import annotations

import contextlib
import logging
import os

# At DEBUG these log full request bodies (CV, posting, contact text) or request lines.
QUIET_LOGGERS = ("anthropic", "httpx", "httpx2", "httpcore", "aiogram.event", "telethon")


def _pin() -> None:
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def configure_logging(level: str | int) -> None:
    logging.basicConfig(level=level)
    # The SDK's import-time setup_logging() sets the "anthropic" and "httpx2" loggers to
    # DEBUG when ANTHROPIC_LOG=debug (also loaded from .env). Drop the variable, import
    # the SDK now so that setup is over, then pin; a later import or client cannot undo it.
    os.environ.pop("ANTHROPIC_LOG", None)
    with contextlib.suppress(ImportError):  # the SDK is a hard dependency
        import anthropic  # noqa: F401
    _pin()
