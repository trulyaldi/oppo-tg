"""Outreach draft service: cache per (contact, CV version), verified, never sent (P5, D-136)."""

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
from src.outreach.prepare import _posting_block
from src.outreach.verify import verify_draft

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompts" / "draft.md"
MAX_TOKENS = 800
PURPOSE = "draft"

GUIDANCE = {
    "recruiter": "fit for the role, ask about the process or the right team.",
    "hiring_manager": "technical hook, ask for a 15-minute call.",
    "founder": "product or mission hook, ask for a short call.",
    "engineer": "technical question, ask for advice or a referral.",
    "other": "polite, generic ask.",
}
FALLBACK_REPLY = "Could not write a draft without unsupported claims."


def prompt_version() -> str:
    return hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest()[:12]


class DraftOut(BaseModel):
    """Loose on purpose: verify_draft validates shape and content."""

    body: Any = ""
    hook: Any = Field(default_factory=dict)
    ask: Any = ""
    claims: Any = Field(default_factory=list)


@dataclass
class DraftResult:
    contact_id: int
    posting_id: str
    cv_version: int
    body: str
    hook: str
    ask: str
    removed: int
    cached: bool


@dataclass
class DraftError:
    kind: str  # no_cv | no_contact | budget | api | invalid | unsupported


def _on_new_version(conn: sqlite3.Connection, new_version: int) -> int:
    return conn.execute("DELETE FROM outreach_drafts WHERE cv_version < ?", (new_version,)).rowcount


def _on_forget(conn: sqlite3.Connection) -> int:
    return conn.execute("DELETE FROM outreach_drafts").rowcount


def register_hooks() -> None:
    if _on_new_version not in cv_store.on_new_cv_version:
        cv_store.on_new_cv_version.append(_on_new_version)
    if _on_forget not in cv_store.on_forget_cv:
        cv_store.on_forget_cv.append(_on_forget)


register_hooks()

_locks: dict[tuple[int, int], asyncio.Lock] = {}


def _cached(conn: sqlite3.Connection, contact_id: int, version: int) -> DraftResult | None:
    row = conn.execute(
        "SELECT * FROM outreach_drafts WHERE contact_id=? AND cv_version=?",
        (contact_id, version),
    ).fetchone()
    if row is None:
        return None
    return DraftResult(
        contact_id,
        row["posting_id"],
        version,
        row["body"],
        row["hook"],
        row["ask"],
        row["removed_claims"],
        True,
    )


def _contact_text(c: sqlite3.Row) -> str:
    return "\n".join(
        [
            f"Name: {c['name']}",
            f"Role: {c['role']}",
            f"Company: {c['company']}",
            f"Type: {c['contact_type']}",
            f"Note: {c['note']}",
        ]
    )


def _tailoring_context(conn: sqlite3.Connection, posting_id: str, version: int) -> str:
    row = conn.execute(
        "SELECT result FROM tailoring_cache WHERE posting_id=? AND cv_version=?",
        (posting_id, version),
    ).fetchone()
    if row is None:
        return ""
    try:
        res = json.loads(row["result"])
    except ValueError:
        return ""
    lines: list[str] = []
    for k in res.get("keywords", []) or []:
        if isinstance(k, dict) and isinstance(k.get("keyword"), str):
            lines.append(f"- keyword: {k['keyword']}")
    for e in res.get("emphasize", []) or []:
        if isinstance(e, dict) and isinstance(e.get("text"), str):
            lines.append(f"- emphasize: {e['text']}")
    if not lines:
        return ""
    return "Verified points from the tailoring step:\n" + "\n".join(lines) + "\n"


async def draft(
    conn: sqlite3.Connection,
    client: Any,
    contact_id: int,
    cfg: AppConfig,
    clock: Clock,
    budget: LlmBudget,
) -> DraftResult | DraftError:
    cv = current_cv(conn)
    if cv is None:
        return DraftError("no_cv")
    contact = conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
    if contact is None:
        return DraftError("no_contact")
    from datetime import datetime

    if datetime.fromisoformat(contact["expires_at"]) <= clock.now():
        return DraftError("no_contact")
    posting = conn.execute("SELECT * FROM postings WHERE id=?", (contact["posting_id"],)).fetchone()
    if posting is None:
        return DraftError("no_contact")
    version = cv["version"]
    lock = _locks.setdefault((contact_id, version), asyncio.Lock())
    async with lock:
        hit = _cached(conn, contact_id, version)
        if hit is not None:
            return hit
        oc = OutreachConfig.from_cfg(cfg)
        posting_block = _posting_block(posting)
        contact_block = wrap_untrusted("contact", _contact_text(contact))
        ctype = contact["contact_type"] if contact["contact_type"] in GUIDANCE else "other"
        base_user = (
            f"Today is {clock.now().date().isoformat()}.\n"
            f"Contact type: {ctype}\nGuidance for {ctype}: {GUIDANCE[ctype]}\n"
            + wrap_untrusted("cv", cv["text"])
            + "\n"
            + posting_block
            + "\n"
            + contact_block
            + "\n"
            + _tailoring_context(conn, contact["posting_id"], version)
        )
        system = [
            {
                "type": "text",
                "text": PROMPT_PATH.read_text(),
                "cache_control": {"type": "ephemeral"},
            }
        ]
        try:
            skills = json.loads(cv["sections"]).get("skills", [])
        except (ValueError, AttributeError):
            skills = []
        skills = [s for s in skills if isinstance(s, str)]

        verdict = None
        draft_dict: dict = {}
        user_text = base_user
        for attempt in range(2):
            res = await call_json(
                client,
                model=oc.tailor_model,
                system_blocks=system,
                user_text=user_text,
                schema=DraftOut,
                max_tokens=MAX_TOKENS,
                budget=budget,
                purpose=PURPOSE,
            )
            if res.value is None:
                return DraftError(res.error_kind or "api")
            draft_dict = res.value.model_dump()
            verdict = verify_draft(draft_dict, cv["text"], posting_block, contact_block, skills)
            if verdict.ok:
                break
            log.info(
                "draft unsupported contact=%s attempt=%s reason=%s",
                contact_id,
                attempt,
                verdict.reason,
            )
            terms = ", ".join(verdict.unsupported) or "none"
            user_text = (
                base_user
                + f"\nYour previous draft failed verification (reason: {verdict.reason}). "
                f"Unsupported terms: {terms}. Write it again with 3 to 5 sentences, a hook quote "
                "copied verbatim from the posting or contact, the ask copied verbatim into the "
                "body, and only facts from the CV."
            )
        if verdict is None or not verdict.ok:
            return DraftError("unsupported")
        hook = draft_dict["hook"]["quote"].strip()
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO outreach_drafts (contact_id, posting_id, cv_version, body,"
                " hook, ask, removed_claims, model, prompt_version, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    contact_id,
                    contact["posting_id"],
                    version,
                    verdict.body,
                    hook,
                    draft_dict["ask"].strip(),
                    verdict.removed,
                    oc.tailor_model,
                    prompt_version(),
                    clock.now().isoformat(),
                ),
            )
        log.info(
            "draft done contact=%s cv_version=%s removed=%s", contact_id, version, verdict.removed
        )
        return DraftResult(
            contact_id,
            contact["posting_id"],
            version,
            verdict.body,
            hook,
            draft_dict["ask"].strip(),
            verdict.removed,
            False,
        )
