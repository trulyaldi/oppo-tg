"""Cheap relevance triage before full scoring (D-196). The client is injected (a fake in tests)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from src import db
from src.clock import Clock
from src.config import TriageConfig
from src.llm.budget import LlmBudget
from src.llm.json_call import call_json
from src.llm.untrusted import wrap_untrusted
from src.models import OpportunityType, Posting
from src.scoring.scorer import row_to_posting

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
MAX_TOKENS = 300
CONCURRENCY = 4
REASON_MAX = 120
PURPOSES = ("triage", "triage_baseline")
FREE_TEXT_SOURCES = frozenset({"telegram_channels", "manual"})  # extraction is asked for these
LANGUAGES = ("en", "ru", "kk", "other")
SCAMS = ("none", "upfront_payment", "personal_contact_only", "other")
CLIPS = {"title": 120, "company": 120, "location": 80, "contact": 80}


class TriageVerdict(BaseModel):
    """`relevant` (a real bool), `reason`, `type` and `language` are required; the rest is optional.

    An invalid `type` or `language` value becomes None (the caller keeps the provisional type,
    D-247, no retry). Extraction strings are clipped, not rejected. Unknown keys are invalid.
    """

    model_config = ConfigDict(extra="forbid")

    relevant: StrictBool
    reason: str = Field(default="")
    type: OpportunityType | None
    language: Literal["en", "ru", "kk", "other"] | None
    title: str | None = None
    company: str | None = None
    location: str | None = None
    contact: str | None = None
    deadline: date | None = None
    scam: Literal["none", "upfront_payment", "personal_contact_only", "other"] = "none"

    @field_validator("reason", mode="before")
    @classmethod
    def _clip(cls, v: Any) -> Any:
        return v.strip()[:REASON_MAX] if isinstance(v, str) else v

    @field_validator("type", mode="before")
    @classmethod
    def _type(cls, v: Any) -> Any:
        try:
            return OpportunityType(v)
        except (ValueError, TypeError):
            return None

    @field_validator("language", mode="before")
    @classmethod
    def _language(cls, v: Any) -> Any:
        return v if v in LANGUAGES else None

    @field_validator("scam", mode="before")
    @classmethod
    def _scam(cls, v: Any) -> Any:
        return v if v in SCAMS else "other" if isinstance(v, str) and v.strip() else "none"

    @field_validator("title", "company", "location", "contact", mode="before")
    @classmethod
    def _clip_text(cls, v: Any, info: Any) -> Any:
        if not isinstance(v, str):
            return None
        return " ".join(v.split())[: CLIPS[info.field_name]] or None

    @field_validator("deadline", mode="before")
    @classmethod
    def _deadline(cls, v: Any) -> Any:
        try:
            return date.fromisoformat(v) if isinstance(v, str) else None
        except ValueError:
            return None


def contact_key(contact: str | None) -> str | None:
    """sha256 of the casefolded, whitespace-stripped contact, first 16 hex (raw text never kept)."""
    norm = "".join((contact or "").casefold().split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16] if norm else None


def triage_prompt() -> str:
    return (HERE / "triage.md").read_text(encoding="utf-8").strip()


def triage_prompt_version() -> str:
    return hashlib.sha256((HERE / "triage.md").read_bytes()).hexdigest()[:12]


def build_triage_message(p: Posting, max_desc_chars: int) -> str:
    """Title, company, location and a description prefix, all inside the untrusted wrapper.

    The trusted `Free-text item` line sits outside the wrapper: it says whether to extract.
    """
    free = "yes" if p.source in FREE_TEXT_SOURCES else "no"
    body = (
        f"Title: {p.title}\nCompany: {p.company}\nLocation: {p.location or 'unknown'}\n\n"
        f"{p.description[:max_desc_chars]}"
    )
    return f"Free-text item: {free}\n" + wrap_untrusted("posting", body)


class Triager:
    def __init__(
        self,
        client: Any,
        conn: sqlite3.Connection,
        clock: Clock,
        cfg: TriageConfig | None = None,
        model: str | None = None,
    ) -> None:
        self.cfg = cfg or TriageConfig()
        self.client = client
        self.conn = conn
        self.clock = clock
        self.model = model or self.cfg.model
        self.version = triage_prompt_version()
        self.budget = LlmBudget(conn, clock, self.cfg.daily_call_cap, PURPOSES)
        self.baseline_budget = LlmBudget(conn, clock, self.cfg.baseline_cap, ("triage_baseline",))
        self.last_error: str | None = None

    async def triage_posting(
        self, p: Posting, baseline: bool = False
    ) -> tuple[TriageVerdict | None, str | None]:
        """One verdict, or (None, error_kind) with error_kind budget, api or invalid."""
        if baseline and self.baseline_budget.used() >= self.baseline_budget.cap:
            return None, "budget"  # the baseline sub-cap, so new postings keep their share
        res = await call_json(
            self.client,
            model=self.model,
            system_blocks=[{"type": "text", "text": triage_prompt()}],
            user_text=build_triage_message(p, self.cfg.max_desc_chars),
            schema=TriageVerdict,
            max_tokens=MAX_TOKENS,
            budget=self.budget,
            purpose="triage_baseline" if baseline else "triage",
            sub_budget=self.baseline_budget if baseline else None,
        )
        if res.error_kind:
            self.last_error = {
                "api": "the triage API call failed, see the bot log",
                "invalid": "the triage reply was not valid JSON for the schema",
            }.get(res.error_kind, self.last_error)
            return None, res.error_kind
        return res.value, None

    async def triage_pending(self, limit: int | None = None) -> dict[str, int]:
        """Triage `new` postings (new ones before baseline). Never raises per posting."""
        rows = db.pending_for_triage(self.conn, limit or max(self.cfg.daily_call_cap, 1))
        sem = asyncio.Semaphore(CONCURRENCY)
        stats = {"yes": 0, "no": 0, "cached": 0, "exempt": 0, "errors": 0, "capped": 0}

        async def one(row: sqlite3.Row) -> None:
            async with sem:
                if row["source"] in self.cfg.exempt_sources:
                    db.set_status(self.conn, row["id"], "triaged")
                    stats["exempt"] += 1
                    return
                cached = db.get_triage_result(self.conn, row["id"], row["content_hash"])
                if cached is not None:
                    db.apply_triage_verdict(
                        self.conn, row["id"], bool(cached["relevant"]), cached["reason"]
                    )
                    if cached["type"] or cached["language"]:
                        db.apply_triage_extraction(
                            self.conn, row["id"], cached["type"], cached["language"]
                        )
                    stats["cached"] += 1
                    return
                try:
                    posting = row_to_posting(row)
                except Exception:
                    log.exception("bad posting row %s", row["id"])
                    db.record_triage_error(self.conn, row["id"])
                    stats["errors"] += 1
                    return
                verdict, error = await self.triage_posting(posting, bool(row["baseline"]))
                if error == "budget":
                    stats["capped"] += 1
                elif verdict is None:
                    db.record_triage_error(self.conn, row["id"])
                    stats["errors"] += 1
                else:
                    db.save_triage_result(
                        self.conn,
                        row["id"],
                        row["content_hash"],
                        verdict.relevant,
                        verdict.reason,
                        self.model,
                        self.version,
                        self.clock.now(),
                        verdict.type.value if verdict.type else None,
                        verdict.language,
                    )
                    db.apply_triage_verdict(self.conn, row["id"], verdict.relevant, verdict.reason)
                    extra: dict[str, str | None] = {}
                    if row["source"] in FREE_TEXT_SOURCES:
                        extra = {
                            "ex_title": verdict.title,
                            "ex_company": verdict.company,
                            "ex_location": verdict.location,
                            "ex_deadline": verdict.deadline.isoformat()
                            if verdict.deadline
                            else None,
                            "contact_key": contact_key(verdict.contact),
                            "scam": verdict.scam,
                        }
                    db.apply_triage_extraction(
                        self.conn,
                        row["id"],
                        verdict.type.value if verdict.type else None,
                        verdict.language,
                        **extra,
                    )
                    stats["yes" if verdict.relevant else "no"] += 1

        await asyncio.gather(*(one(r) for r in rows))
        return stats
