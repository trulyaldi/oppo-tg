"""Daily LLM call cap for outreach, counted per Asia/Almaty day (D-98). Counts only, no content."""

from __future__ import annotations

import sqlite3
from zoneinfo import ZoneInfo

from src.clock import Clock

TZ = ZoneInfo("Asia/Almaty")

# Every purpose outreach spends calls on; its cap counts only these (D-184, D-196).
OUTREACH_PURPOSES = ("parse_cv", "extract_contact", "tailor", "draft")


class LlmBudget:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock,
        cap: int,
        purposes: tuple[str, ...] | None = None,
    ) -> None:
        self.purposes = purposes
        self.conn = conn
        self.clock = clock
        self.cap = cap

    def _day(self) -> str:
        return self.clock.now().astimezone(TZ).date().isoformat()

    def used(self) -> int:
        sql = "SELECT COALESCE(SUM(calls), 0) FROM llm_usage WHERE day=?"
        args: list[str] = [self._day()]
        if self.purposes is not None:
            sql += f" AND purpose IN ({','.join('?' * len(self.purposes))})"
            args += self.purposes
        row = self.conn.execute(sql, args).fetchone()
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
