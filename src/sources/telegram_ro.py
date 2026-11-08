"""Read-only facade over a Telethon client (D-256).

The user's hard rules: never join a channel or group automatically, never read private chats,
never send anything from the user account. Every Telethon use goes through this wrapper, which
exposes an explicit allowlist and nothing else. Raw requests are checked by class name so tests
need no telethon import.
"""

from __future__ import annotations

import re
from typing import Any

ALLOWED_REQUESTS = frozenset({"SearchRequest", "GetFullChannelRequest", "GetHistoryRequest"})
ALLOWED_EVENTS = frozenset({"NewMessage"})
# iter_messages may only read a public channel or supergroup: an InputPeerChannel/Channel or a
# public handle string. Users, basic groups, "me" and phone numbers are refused (KF-19).
READABLE_PEERS = frozenset({"InputPeerChannel", "Channel"})
NON_CHANNEL_PEERS = frozenset(
    {
        "InputPeerUser",
        "InputPeerChat",
        "InputPeerSelf",
        "InputPeerEmpty",
        "InputPeerUserFromMessage",
        "InputPeerChannelFromMessage",
    }
)
_HANDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_NOT_HANDLES = frozenset({"self"})


class ReadOnlyViolation(RuntimeError):
    """A Telegram call outside the read-only allowlist was attempted."""


class ReadOnlyTelegram:
    def __init__(self, client: Any) -> None:
        self._client = client

    @property
    def wrapped(self) -> Any:
        return self._client

    # ----- connection -----

    async def connect(self) -> Any:
        return await self._client.connect()

    async def disconnect(self) -> Any:
        return await self._client.disconnect()

    def is_connected(self) -> bool:
        return self._client.is_connected()

    async def is_user_authorized(self) -> bool:
        return await self._client.is_user_authorized()

    # ----- reads -----

    async def get_input_entity(self, peer: Any) -> Any:
        found = await self._client.get_input_entity(peer)
        kind = type(found).__name__
        if kind in NON_CHANNEL_PEERS:  # a handle that names a user or a basic group
            raise ReadOnlyViolation(f"{kind} is not a channel (read-only Telegram)")
        return found

    def iter_messages(self, entity: Any, *args: Any, **kwargs: Any) -> Any:
        kind = type(entity).__name__
        if isinstance(entity, str):
            ok = bool(_HANDLE_RE.match(entity)) and entity.casefold() not in _NOT_HANDLES
        else:
            ok = kind in READABLE_PEERS
        if not ok:
            raise ReadOnlyViolation(
                f"iter_messages on {kind} is not allowed: public channels only (read-only Telegram)"
            )
        return self._client.iter_messages(entity, *args, **kwargs)

    def iter_dialogs(self, *args: Any, **kwargs: Any) -> Any:
        return self._client.iter_dialogs(*args, **kwargs)

    # ----- live events -----

    def add_event_handler(self, callback: Any, event: Any = None) -> Any:
        kind = type(event).__name__
        if kind not in ALLOWED_EVENTS:
            raise ReadOnlyViolation(f"event {kind} is not allowed (read-only Telegram)")
        return self._client.add_event_handler(callback, event)

    def remove_event_handler(self, callback: Any, *args: Any) -> Any:
        return self._client.remove_event_handler(callback, *args)

    # ----- raw requests -----

    async def __call__(self, request: Any) -> Any:
        kind = type(request).__name__
        if kind not in ALLOWED_REQUESTS:
            raise ReadOnlyViolation(f"request {kind} is not allowed (read-only Telegram)")
        return await self._client(request)

    def __getattr__(self, name: str) -> Any:
        # Reached only for names not defined above: send_message, send_file, forward_messages,
        # delete_messages, edit_message, send_read_acknowledge and everything else.
        raise ReadOnlyViolation(f"{name} is not allowed (read-only Telegram)")


def read_only(client: Any) -> ReadOnlyTelegram:
    """Wrap once; wrapping a wrapper returns it unchanged."""
    return client if isinstance(client, ReadOnlyTelegram) else ReadOnlyTelegram(client)
