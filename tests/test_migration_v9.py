import sqlite3

from src import db

TS = "2026-11-01T00:00:00+06:00"


def _v8_file(path):
    c = sqlite3.connect(str(path))
    for s in db._MIGRATIONS[:8]:
        for stmt in s.split(";"):
            if stmt.strip():
                c.execute(stmt)
    c.execute("PRAGMA user_version=8")
    c.execute(
        "INSERT INTO postings (id, source, external_id, title, url, fingerprint,"
        " status, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("p1", "hn", "1", "T", "https://x.example/1", "fp1", "scored", TS, TS),
    )
    c.commit()
    c.close()


def test_v8_to_v9_adds_candidates_and_is_idempotent(tmp_path):
    path = tmp_path / "v8.db"
    _v8_file(path)
    conn = db.connect(path)
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
    assert conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 1
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(board_candidates)")]
    assert cols == ["source", "token", "status", "checked_at", "company", "discovered_from"]
    conn.execute(
        "INSERT INTO board_candidates VALUES ('lever','acme','valid',?,'Acme','posting:p1')", (TS,)
    )
    conn.commit()
    db.migrate(conn)
    assert conn.execute("SELECT COUNT(*) FROM board_candidates").fetchone()[0] == 1
    assert not conn.execute(
        "SELECT name FROM sqlite_master WHERE name='discovery_spend'"
    ).fetchone()
