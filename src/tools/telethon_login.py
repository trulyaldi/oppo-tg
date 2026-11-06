"""Interactive one-time Telethon login: creates the session file the source reuses.

Run by the user, never in tests: `.venv/bin/python -m src.tools.telethon_login`.
Reads TG_API_ID, TG_API_HASH and TG_SESSION_PATH from the environment (export them or
load your .env yourself). The session file grants account access: keep it private and
out of git (*.session is ignored).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


async def login(api_id: int, api_hash: str, session_path: str) -> None:
    from telethon import TelegramClient

    Path(session_path).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(session_path, api_id, api_hash)
    await client.start()  # prompts for phone, code and 2FA password on the terminal
    me = await client.get_me()
    print(f"Logged in as {getattr(me, 'first_name', '?')}. Session saved to {session_path}")
    await client.disconnect()


def main() -> int:
    from src.config import Settings

    settings = Settings.from_env(os.environ, require_secrets=False)  # empty means default
    if not settings.tg_api_id or not settings.tg_api_hash:
        print("Set TG_API_ID and TG_API_HASH in the environment first.", file=sys.stderr)
        return 2
    try:
        api_id = int(settings.tg_api_id)
    except ValueError:
        print(
            "TG_API_ID must be an integer (the numeric api_id from my.telegram.org).",
            file=sys.stderr,
        )
        return 2
    asyncio.run(login(api_id, settings.tg_api_hash, settings.tg_session_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
