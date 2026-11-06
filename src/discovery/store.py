"""Discovered ATS boards kept in a TOML file (D-190, D-212, FC-15).

The file is runtime data (gitignored). Writes go to a temp file and are moved
into place with `os.replace`, so a crash never leaves a half-written file.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_BOARDS_FILE = "data/discovered_boards.toml"
MAX_COMPANY_LEN = 120
ACTIVE = "active"
REMOVED = "removed"


class StoreCorrupt(Exception):
    """The boards file cannot be parsed; fix or delete it before adding boards."""


@dataclass(frozen=True)
class BoardEntry:
    source: str
    token: str
    company: str = ""
    status: str = ACTIVE
    added_at: str = ""


_ESC = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"}


def _quote(value: str) -> str:
    out = []
    for ch in value:
        if ch in _ESC:
            out.append(_ESC[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _render(entries: list[BoardEntry]) -> str:
    lines = ["# Discovered ATS boards (runtime data, managed by the bot).", ""]
    for e in entries:
        lines += [
            "[[board]]",
            f"source = {_quote(e.source)}",
            f"token = {_quote(e.token)}",
            f"company = {_quote(e.company)}",
            f"status = {_quote(e.status)}",
            f"added_at = {_quote(e.added_at)}",
            "",
        ]
    return "\n".join(lines)


def _read(path: str | Path) -> list[BoardEntry]:
    """Parse the file; a missing file is empty, anything unparsable raises StoreCorrupt."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise StoreCorrupt(str(exc)) from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
        rows = data.get("board", [])
        if not isinstance(rows, list):
            raise ValueError("board must be an array of tables")
        entries = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("board entry must be a table")
            source, token = row["source"], row["token"]
            status = row.get("status", ACTIVE)
            company = row.get("company", "")
            added = row.get("added_at", "")
            if not all(isinstance(v, str) for v in (source, token, status, company, added)):
                raise ValueError("board fields must be strings")
            if status not in (ACTIVE, REMOVED):
                raise ValueError(f"bad status {status!r}")
            entries.append(BoardEntry(source, token, company, status, added))
        return entries
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, ValueError) as exc:
        raise StoreCorrupt(f"{p}: {exc}") from exc


def _write(path: str | Path, entries: list[BoardEntry]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(_render(entries))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def load(path: str | Path = DEFAULT_BOARDS_FILE) -> list[BoardEntry]:
    """All entries (removed ones included); a corrupt file gives [] and a warning."""
    try:
        return _read(path)
    except StoreCorrupt as exc:
        log.warning("discovered boards file unreadable, ignoring it: %s", exc)
        return []


def _same(a: BoardEntry, source: str, token: str) -> bool:
    return a.source == source and a.token.casefold() == token.casefold()


def is_known(path: str | Path, source: str, token: str) -> bool:
    """True for active and removed (tombstoned) entries alike."""
    return any(_same(e, source, token) for e in load(path))


def add(
    path: str | Path, source: str, token: str, company: str = "", now: datetime | None = None
) -> bool:
    """Record a board. False if already known; raises StoreCorrupt on a bad file."""
    entries = _read(path)
    if any(_same(e, source, token) for e in entries):
        return False
    stamp = now.isoformat() if now else ""
    entries.append(BoardEntry(source, token, company[:MAX_COMPANY_LEN], ACTIVE, stamp))
    _write(path, entries)
    return True


def tombstone(path: str | Path, source: str, token: str) -> None:
    """Mark a board removed so it is never fetched or re-discovered."""
    entries = _read(path)
    out = [
        BoardEntry(e.source, e.token, e.company, REMOVED, e.added_at)
        if _same(e, source, token)
        else e
        for e in entries
    ]
    if not any(_same(e, source, token) for e in entries):
        out.append(BoardEntry(source, token, "", REMOVED, ""))
    _write(path, out)


def active_boards(
    path: str | Path, source: str, exclude: set[str] | frozenset[str] = frozenset()
) -> list[BoardEntry]:
    """Active boards of one source, skipping tokens in `exclude` (config wins)."""
    skip = {t.casefold() for t in exclude}
    return [
        e
        for e in load(path)
        if e.source == source and e.status == ACTIVE and e.token.casefold() not in skip
    ]
