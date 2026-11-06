"""Tailoring service: cache per (posting, CV version), budget, verification (P2, D-126)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.clock import Clock
from src.config import AppConfig, OutreachConfig
from src.cv import store as cv_store
from src.cv.store import current_cv
from src.llm.budget import LlmBudget
from src.llm.json_call import call_json
from src.llm.untrusted import wrap_untrusted
from src.outreach.verify import verify_tailoring

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompts" / "tailor.md"
MAX_TOKENS = 2000
PURPOSE = "tailor"
DESC_MAX = 6000


def prompt_version() -> str:
    return hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest()[:12]


class TailorOut(BaseModel):
    """Loose on purpose: the deterministic verifier validates every item."""

    fit: dict = Field(default_factory=dict)
    emphasize: list = Field(default_factory=list)
    cut: list = Field(default_factory=list)
    reorder: list = Field(default_factory=list)
    keywords: list = Field(default_factory=list)
    gaps: list = Field(default_factory=list)


@dataclass
class PrepareResult:
    posting_id: str
    cv_version: int
    result: dict
    removed: int
    cached: bool


@dataclass
class PrepareError:
    kind: str  # no_cv | no_posting | budget | api | invalid


# --- cache hooks (run inside save_cv/forget_cv transactions) -----------------


def _on_new_version(conn: sqlite3.Connection, new_version: int) -> int:
    return conn.execute("DELETE FROM tailoring_cache WHERE cv_version < ?", (new_version,)).rowcount


def _on_forget(conn: sqlite3.Connection) -> int:
    return conn.execute("DELETE FROM tailoring_cache").rowcount


def register_hooks() -> None:
    if _on_new_version not in cv_store.on_new_cv_version:
        cv_store.on_new_cv_version.append(_on_new_version)
    if _on_forget not in cv_store.on_forget_cv:
        cv_store.on_forget_cv.append(_on_forget)


register_hooks()

_locks: dict[tuple[str, int], asyncio.Lock] = {}


def _cached(conn: sqlite3.Connection, posting_id: str, version: int) -> PrepareResult | None:
    row = conn.execute(
        "SELECT result, removed_claims FROM tailoring_cache WHERE posting_id=? AND cv_version=?",
        (posting_id, version),
    ).fetchone()
    if row is None:
        return None
    return PrepareResult(
        posting_id, version, json.loads(row["result"]), row["removed_claims"], True
    )


def _posting_block(row: sqlite3.Row) -> str:
    return wrap_untrusted(
        "posting",
        "\n".join(
            [
                f"Title: {row['title']}",
                f"Company: {row['company']}",
                f"Kind: {row['kind']}",
                f"Location: {row['location']}",
                f"Deadline: {row['deadline'] or 'unknown'}",
                f"Description: {(row['description'] or '')[:DESC_MAX]}",
            ]
        ),
    )


async def prepare(
    conn: sqlite3.Connection,
    client: Any,
    posting_id: str,
    cfg: AppConfig,
    clock: Clock,
    budget: LlmBudget,
) -> PrepareResult | PrepareError:
    cv = current_cv(conn)
    if cv is None:
        return PrepareError("no_cv")
    row = conn.execute("SELECT * FROM postings WHERE id=?", (posting_id,)).fetchone()
    if row is None:
        return PrepareError("no_posting")
    key = (posting_id, cv["version"])
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _cached(conn, posting_id, cv["version"])
        if hit is not None:
            return hit
        oc = OutreachConfig.from_cfg(cfg)
        posting_block = _posting_block(row)
        user_text = (
            f"Today is {clock.now().date().isoformat()}.\n"
            + wrap_untrusted("cv", cv["text"])
            + "\n"
            + posting_block
        )
        system = [
            {
                "type": "text",
                "text": PROMPT_PATH.read_text(),
                "cache_control": {"type": "ephemeral"},
            }
        ]
        res = await call_json(
            client,
            model=oc.tailor_model,
            system_blocks=system,
            user_text=user_text,
            schema=TailorOut,
            max_tokens=MAX_TOKENS,
            budget=budget,
            purpose=PURPOSE,
        )
        if res.value is None:
            return PrepareError(res.error_kind or "api")
        try:
            skills = json.loads(cv["sections"]).get("skills", [])
        except (ValueError, AttributeError):
            skills = []
        verified, removed = verify_tailoring(
            res.value.model_dump(),
            cv["text"],
            posting_block,
            [s for s in skills if isinstance(s, str)],
        )
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO tailoring_cache (posting_id, cv_version, result,"
                " removed_claims, model, prompt_version, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    posting_id,
                    cv["version"],
                    json.dumps(verified, ensure_ascii=False),
                    removed,
                    oc.tailor_model,
                    prompt_version(),
                    clock.now().isoformat(),
                ),
            )
        log.info(
            "prepare done posting=%s cv_version=%s removed=%s", posting_id, cv["version"], removed
        )
        return PrepareResult(posting_id, cv["version"], verified, removed, False)
