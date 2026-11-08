import sqlite3

import pytest

from src import db


def test_v14_to_v15_adds_candidates_and_is_idempotent(tmp_path):
    path = tmp_path / "v14.db"
    c = sqlite3.connect(str(path))
    for s in db._MIGRATIONS[:14]:
        for stmt in s.split(";"):
            if stmt.strip():
                c.execute(stmt)
    c.execute("INSERT INTO tg_entities (handle, status) VALUES ('kept_chan', 'ok')")
    c.execute("PRAGMA user_version=14")
    c.commit()
    c.close()
    conn = db.connect(path)
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION >= 15
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(tg_channel_candidates)")]
    assert cols == [
        "handle",
        "title",
        "subscribers",
        "job_ratio",
        "samples",
        "found_by",
        "found_at",
        "status",
    ]
    assert conn.execute("SELECT handle FROM tg_entities").fetchone()["handle"] == "kept_chan"
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_status_check_and_samples_default():
    conn = db.connect(":memory:")
    db.migrate(conn)
    conn.execute(
        "INSERT INTO tg_channel_candidates (handle, found_at, status) VALUES ('a_chan','t','new')"
    )
    row = conn.execute("SELECT samples FROM tg_channel_candidates").fetchone()
    assert row["samples"] == "[]"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tg_channel_candidates (handle, found_at, status)"
            " VALUES ('b_chan','t','joined')"
        )
