import sqlite3

import pytest

from src import db

TS = "2026-11-01T00:00:00+06:00"


def test_v9_to_v10_adds_tg_channels_and_is_idempotent(tmp_path):
    path = tmp_path / "v9.db"
    c = sqlite3.connect(str(path))
    for s in db._MIGRATIONS[:9]:
        for stmt in s.split(";"):
            if stmt.strip():
                c.execute(stmt)
    c.execute("PRAGMA user_version=9")
    c.execute(
        "INSERT INTO postings (id, source, external_id, title, url, fingerprint,"
        " status, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("p1", "hn", "1", "T", "https://x.example/1", "fp1", "scored", TS, TS),
    )
    c.commit()
    c.close()
    conn = db.connect(path)
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 1
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(tg_channels)")]
    assert cols == ["handle", "state", "updated_at"]
    conn.execute("INSERT INTO tg_channels VALUES ('job_kz','added',?)", (TS,))
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO tg_channels VALUES ('x_chan','bogus',?)", (TS,))
    db.migrate(conn)
    assert conn.execute("SELECT COUNT(*) FROM tg_channels").fetchone()[0] == 1
