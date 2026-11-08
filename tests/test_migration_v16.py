import sqlite3

import pytest

from src import db


def test_v15_to_v16_adds_cv_reviews_and_is_idempotent(tmp_path):
    path = tmp_path / "v15.db"
    c = sqlite3.connect(str(path))
    for s in db._MIGRATIONS[:15]:
        for stmt in s.split(";"):
            if stmt.strip():
                c.execute(stmt)
    c.execute("INSERT INTO tg_entities (handle, status) VALUES ('kept_chan', 'ok')")
    c.execute("PRAGMA user_version=15")
    c.commit()
    c.close()
    conn = db.connect(path)
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION >= 16
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(cv_reviews)")]
    assert cols == [
        "cv_version",
        "prompt_version",
        "model",
        "market_json",
        "summary_json",
        "report_md",
        "removed",
        "questions",
        "created_at",
    ]
    assert conn.execute("SELECT handle FROM tg_entities").fetchone()["handle"] == "kept_chan"
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_primary_key_is_version_and_prompt():
    conn = db.connect(":memory:")
    db.migrate(conn)
    row = ("1", "p", "m", "{}", "{}", "r", 0, 0, "t")
    sql = "INSERT INTO cv_reviews VALUES (?,?,?,?,?,?,?,?,?)"
    conn.execute(sql, (1, *row[1:]))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (1, *row[1:]))
    conn.execute(sql, (1, "p2", *row[2:]))
