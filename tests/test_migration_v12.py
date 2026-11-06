import sqlite3

from src import db

TS = "2026-11-01T00:00:00+06:00"


def test_v11_to_v12_adds_triage_and_is_idempotent(tmp_path):
    path = tmp_path / "v11.db"
    c = sqlite3.connect(str(path))
    for s in db._MIGRATIONS[:11]:
        for stmt in s.split(";"):
            if stmt.strip():
                c.execute(stmt)
    c.execute("PRAGMA user_version=11")
    c.execute(
        "INSERT INTO postings (id, source, external_id, title, url, fingerprint,"
        " status, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("p1", "hn", "1", "T", "https://x.example/1", "fp1", "scored", TS, TS),
    )
    c.commit()
    c.close()
    conn = db.connect(path)
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 12
    row = conn.execute("SELECT * FROM postings WHERE id='p1'").fetchone()
    assert row["triage_attempts"] == 0 and row["status"] == "scored"
    conn.execute(
        "INSERT INTO triage_results VALUES ('p1','h',1,'r','m','v',?)",
        (TS,),
    )
    conn.commit()
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
    assert conn.execute("SELECT COUNT(*) FROM triage_results").fetchone()[0] == 1
    conn.execute("DELETE FROM postings WHERE id='p1'")  # cascade, with foreign keys on
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM triage_results").fetchone()[0] == 0
