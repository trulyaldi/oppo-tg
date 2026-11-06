import sqlite3
from datetime import UTC, datetime

from src import db
from src.clock import FixedClock
from src.config import AppConfig, OutreachConfig
from src.llm.budget import LlmBudget


def mk(tmp_path):
    c = db.connect(tmp_path / "b.db")
    db.migrate(c)
    return c


def test_cap_shared_and_resets_at_almaty_midnight(tmp_path):
    conn = mk(tmp_path)
    clock = FixedClock(datetime(2026, 11, 1, 18, 0, tzinfo=UTC))  # 23:00 Almaty
    b = LlmBudget(conn, clock, 3)
    assert b.reserve("cv_parse") and b.reserve("tailor") and b.reserve("cv_parse")
    assert not b.reserve("draft") and b.used() == 3
    clock.set(datetime(2026, 11, 1, 19, 30, tzinfo=UTC))  # 00:30 next Almaty day
    assert b.used() == 0 and b.reserve("draft")


def test_survives_reconnect(tmp_path):
    conn = mk(tmp_path)
    clock = FixedClock(datetime(2026, 11, 1, 5, 0, tzinfo=UTC))
    LlmBudget(conn, clock, 2).reserve("a")
    conn.close()
    conn2 = db.connect(tmp_path / "b.db")
    b = LlmBudget(conn2, clock, 2)
    assert b.used() == 1 and b.reserve("a") and not b.reserve("a")


def test_migration_v3_on_v2_db_with_rows(tmp_path):
    path = tmp_path / "v2.db"
    c = sqlite3.connect(str(path))
    c.executescript(db.SCHEMA_1)
    c.close()
    conn = db.connect(path)
    conn.execute("PRAGMA user_version=1")
    for stmt in db.SCHEMA_2.split(";"):
        if stmt.strip():
            conn.execute(stmt)
    conn.execute("PRAGMA user_version=2")
    conn.execute("INSERT INTO state VALUES ('k','v')")
    conn.commit()
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
    assert conn.execute("SELECT value FROM state WHERE key='k'").fetchone()[0] == "v"
    assert conn.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == 0


def test_outreach_config_defaults_and_snapshot():
    assert OutreachConfig.from_cfg(AppConfig()).daily_llm_call_cap == 40
    from pathlib import Path

    snap = Path(__file__).parent / "fixtures" / "config_repo_snapshot.toml"
    assert OutreachConfig.from_cfg(AppConfig.load(snap)) == OutreachConfig()
    root = Path(__file__).parent.parent / "config.toml"
    assert OutreachConfig.from_cfg(AppConfig.load(root)) == OutreachConfig()


def test_models_known_to_sdk():
    import typing

    from anthropic.types import Model

    known = {a for a in typing.get_args(Model) if isinstance(a, str)} or {
        x for a in typing.get_args(Model) for x in typing.get_args(a)
    }
    d = OutreachConfig()
    assert d.cheap_model in known and d.tailor_model in known
