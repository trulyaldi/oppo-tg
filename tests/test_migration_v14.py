import sqlite3

import pytest

from src import db


def test_v13_to_v14_adds_tg_entities_and_is_idempotent(tmp_path):
    path = tmp_path / "v13.db"
    c = sqlite3.connect(str(path))
    for s in db._MIGRATIONS[:13]:
        for stmt in s.split(";"):
            if stmt.strip():
                c.execute(stmt)
    c.execute("PRAGMA user_version=13")
    c.commit()
    c.close()
    conn = db.connect(path)
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION >= 14
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(tg_entities)")]
    assert cols == [
        "handle",
        "channel_id",
        "access_hash",
        "title",
        "status",
        "resolved_at",
        "last_polled_at",
    ]
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_status_check_constraint():
    conn = db.connect(":memory:")
    db.migrate(conn)
    conn.execute("INSERT INTO tg_entities (handle, status) VALUES ('a_chan', 'ok')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO tg_entities (handle, status) VALUES ('b_chan', 'banned')")
