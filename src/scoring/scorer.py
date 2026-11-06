"""LLM scorer (D-11, D-12, D-39). The client is injected, so tests use a fake."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from src import db
from src.clock import Clock
from src.llm.budget import LlmBudget
from src.models import Kind, Posting, ScoreResult
from src.scoring.parse import ScoreParseError, parse_score_response
from src.scoring.prompt import build_system_blocks, build_user_message, prompt_version

log = logging.getLogger(__name__)

MAX_TOKENS = 400
CONCURRENCY = 4
TZ = ZoneInfo("Asia/Almaty")
CORRECTION = (
    "Your previous reply was not valid: {error}\n"
    "Reply again with only the JSON object from the output contract, no other text."
)


class BudgetExhausted(Exception):
    """The daily call cap (or the baseline sub-cap) stopped a call."""


def row_to_posting(row: sqlite3.Row) -> Posting:
    remote = row["remote"]
    return Posting(
        source=row["source"],
        external_id=row["external_id"],
        title=row["title"],
        company=row["company"],
        url=row["url"],
        location=row["location"],
        remote=None if remote is None else bool(remote),
        description=row["description"],
        posted_at=datetime.fromisoformat(row["posted_at"]) if row["posted_at"] else None,
        deadline=date.fromisoformat(row["deadline"]) if row["deadline"] else None,
        kind=Kind(row["kind"]),
        raw_tags=tuple(json.loads(row["raw_tags"] or "[]")),
    )


def _text(response: Any) -> str:
    return "".join(b.text for b in response.content if getattr(b, "type", None) == "text")


class Scorer:
    def __init__(
        self,
        client: Any,
        model: str,
        conn: sqlite3.Connection,
        clock: Clock,
        max_calls: int,
        budget: LlmBudget | None = None,
        baseline_budget: LlmBudget | None = None,
        triage: bool = False,
    ) -> None:
        self.triage = triage  # only `triaged` postings are pending when triage is on (D-196)
        self.budget = budget  # purposes score + score_baseline, the daily cap (D-184)
        self.baseline_budget = baseline_budget  # purpose score_baseline, the sub-cap (D-188)
        self.client = client
        self.model = model
        self.conn = conn
        self.clock = clock
        self.max_calls = max_calls
        self.calls = 0
        self.last_error: str | None = None  # most recent call failure, for the failure alert
        self.version = prompt_version()

    @property
    def budget_left(self) -> bool:
        return self.calls < self.max_calls

    def _examples(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "like": db.recent_feedback_examples(self.conn, "like", 20),
            "dislike": db.recent_feedback_examples(self.conn, "dislike", 20),
        }

    def _reserve_daily(self, baseline: bool) -> None:
        if self.budget is None:
            return
        sub = self.baseline_budget
        if baseline and sub is not None and sub.used() >= sub.cap:
            raise BudgetExhausted("baseline")
        if not self.budget.reserve("score_baseline" if baseline else "score"):
            raise BudgetExhausted("daily")

    async def _call(self, system: list[dict], messages: list[dict], baseline: bool = False) -> str:
        self._reserve_daily(baseline)
        self.calls += 1  # reserved before awaiting, so concurrent tasks respect the cap
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=messages,
        )
        return _text(response)

    async def score(
        self,
        posting: Posting,
        examples: dict[str, list[dict[str, Any]]] | None = None,
        baseline: bool = False,
    ) -> ScoreResult | None:
        """Score one posting; None on any failure or cap. Retries once on a parse failure."""
        try:
            return await self.score_or_cap(posting, examples, baseline)
        except BudgetExhausted:
            return None

    async def score_or_cap(
        self,
        posting: Posting,
        examples: dict[str, list[dict[str, Any]]] | None = None,
        baseline: bool = False,
    ) -> ScoreResult | None:
        """Like score, but raises BudgetExhausted when a cap stops a call (not a failure)."""
        try:
            system = build_system_blocks(self._examples() if examples is None else examples)
            today = self.clock.now().astimezone(TZ).date()
            messages: list[dict] = [{"role": "user", "content": build_user_message(posting, today)}]
            if not self.budget_left:
                return None
            raw = await self._call(system, messages, baseline)
            try:
                return parse_score_response(raw)
            except ScoreParseError as e:
                first_error = e
            if not self.budget_left:
                return None
            messages += [
                {"role": "assistant", "content": raw or "(empty reply)"},
                {"role": "user", "content": CORRECTION.format(error=str(first_error)[:300])},
            ]
            return parse_score_response(await self._call(system, messages, baseline))
        except BudgetExhausted:
            raise
        except ScoreParseError:
            self.last_error = "model reply was not valid JSON twice in a row"
            log.warning("score parse failed twice for %s", posting.id)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {str(e)[:200]}"
            log.exception("scoring call failed for %s", posting.id)
        return None

    async def score_pending(self, limit: int) -> dict[str, int]:
        rows = db.pending_for_scoring(self.conn, limit, self.triage)
        examples = self._examples()
        sem = asyncio.Semaphore(CONCURRENCY)
        stats = {"scored": 0, "errors": 0, "skipped": 0, "capped": 0}

        async def one(row: sqlite3.Row) -> None:
            async with sem:
                if not self.budget_left:
                    stats["skipped"] += 1
                    return
                try:
                    posting = row_to_posting(row)
                except Exception:
                    log.exception("bad posting row %s", row["id"])
                    db.record_score_error(self.conn, row["id"])
                    stats["errors"] += 1
                    return
                try:
                    result = await self.score_or_cap(posting, examples, bool(row["baseline"]))
                except BudgetExhausted:
                    stats["capped"] += 1
                    return
                if result is None:
                    db.record_score_error(self.conn, row["id"])
                    stats["errors"] += 1
                else:
                    db.save_score(
                        self.conn, row["id"], result, self.model, self.version, self.clock.now()
                    )
                    stats["scored"] += 1

        await asyncio.gather(*(one(r) for r in rows))
        return stats
