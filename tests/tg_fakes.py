"""Helpers for fake Telethon clients: real InputPeerChannel values, no network (KF-19)."""

from zlib import crc32

from telethon.tl.types import InputPeerChannel

_HANDLES: dict[int, str] = {}


def peer_for(handle: str) -> InputPeerChannel:
    """A stable fake peer for a handle; `handle_of` maps it back."""
    cid = 1_000_000 + crc32(handle.encode()) % 1_000_000
    _HANDLES[cid] = handle
    return InputPeerChannel(cid, cid * 7)


def handle_of(entity) -> str:
    """The handle behind a peer made by `peer_for` (strings pass through)."""
    if isinstance(entity, str):
        return entity
    return _HANDLES[entity.channel_id]
