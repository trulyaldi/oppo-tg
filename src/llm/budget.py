"""Daily LLM call cap for outreach, counted per Asia/Almaty day (D-98). Counts only, no content."""

from __future__ import annotations

import sqlite3
from zoneinfo import ZoneInfo

from src.clock import Clock

TZ = ZoneInfo("Asia/Almaty")


class LlmBudget:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, cap: int) -> None:
        self.conn = conn
        self.clock = clock
        self.cap = cap

    def _day(self) -> str:
        return self.clock.now().astimezone(TZ).date().isoformat()

    def used(self) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(calls), 0) FROM llm_usage WHERE day=?", (self._day(),)
        ).fetchone()
        return int(row[0])

    def reserve(self, purpose: str) -> bool:
        """Count one call before it is made; False (and no count) when the cap is reached."""
        with self.conn:
            if self.used() >= self.cap:
                return False
            self.conn.execute(
                "INSERT INTO llm_usage (day, purpose, calls) VALUES (?, ?, 1)"
                " ON CONFLICT(day, purpose) DO UPDATE SET calls = calls + 1",
                (self._day(), purpose),
            )
        return True
