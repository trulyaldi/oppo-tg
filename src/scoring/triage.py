"""Cheap relevance triage before full scoring (D-196). The client is injected (a fake in tests)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from src import db
from src.clock import Clock
from src.config import TriageConfig
from src.llm.budget import LlmBudget
from src.llm.json_call import call_json
from src.llm.untrusted import wrap_untrusted
from src.models import Posting
from src.scoring.scorer import row_to_posting

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
MAX_TOKENS = 150
CONCURRENCY = 4
REASON_MAX = 120
PURPOSES = ("triage", "triage_baseline")


class TriageVerdict(BaseModel):
    """Exactly `relevant` (a real bool) and `reason`; anything else is an invalid reply."""

    model_config = ConfigDict(extra="forbid")

    relevant: StrictBool
    reason: str = Field(default="")

    @field_validator("reason", mode="before")
    @classmethod
    def _clip(cls, v: Any) -> Any:
        return v.strip()[:REASON_MAX] if isinstance(v, str) else v


def triage_prompt() -> str:
    return (HERE / "triage.md").read_text(encoding="utf-8").strip()


def triage_prompt_version() -> str:
    return hashlib.sha256((HERE / "triage.md").read_bytes()).hexdigest()[:12]


def build_triage_message(p: Posting, max_desc_chars: int) -> str:
    """Title, company, location and a description prefix, all inside the untrusted wrapper."""
    body = (
        f"Title: {p.title}\nCompany: {p.company}\nLocation: {p.location or 'unknown'}\n\n"
        f"{p.description[:max_desc_chars]}"
    )
    return wrap_untrusted("posting", body)


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
                    )
                    db.apply_triage_verdict(self.conn, row["id"], verdict.relevant, verdict.reason)
                    stats["yes" if verdict.relevant else "no"] += 1

        await asyncio.gather(*(one(r) for r in rows))
        return stats
