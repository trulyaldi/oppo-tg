import sqlite3

from src import db

TS = "2026-11-01T00:00:00+06:00"


def _v1_file(path):
    c = sqlite3.connect(str(path))
    c.executescript(db.SCHEMA_1)
    c.execute("PRAGMA user_version=1")
    for pid, sc in (("a", 7.5), ("b", 3.0)):
        c.execute(
            "INSERT INTO postings (id, source, external_id, title, url, fingerprint,"
            " status, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, "t", pid, "T", "https://x.example", "fp" + pid, "scored", TS, TS),
        )
        c.execute(
            "INSERT INTO scores (posting_id, score, reason, model, prompt_version, scored_at)"
            " VALUES (?,?,?,?,?,?)",
            (pid, sc, "r", "m", "v", TS),
        )
    c.execute("INSERT INTO reactions VALUES ('a','like',1,?)", (TS,))
    c.execute("INSERT INTO deliveries VALUES ('a','digest','1',5,?)", (TS,))
    c.commit()
    c.close()


def test_v1_to_v2_preserves_data(tmp_path):
    path = tmp_path / "v1.db"
    _v1_file(path)
    conn = db.connect(path)
    db.migrate(conn)
    db.migrate(conn)  # idempotent
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    rows = {r["posting_id"]: r for r in conn.execute("SELECT * FROM scores")}
    for pid, sc in (("a", 7.5), ("b", 3.0)):
        r = rows[pid]
        assert r["base_score"] == r["adjusted_score"] == sc
        assert r["employer_category"] == "unknown"
        assert r["weights_version"] is None
        assert r["employer_region"] == "unknown" and r["adjustment"] == 0
    assert conn.execute("SELECT COUNT(*) FROM reactions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 1


def test_fresh_db_is_v2():
    conn = db.connect(":memory:")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(scores)")}
    assert {"base_score", "adjusted_score", "weights_version"} <= cols and "score" not in cols
