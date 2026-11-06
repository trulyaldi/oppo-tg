import sqlite3

from src import db

TS = "2026-11-01T00:00:00+06:00"
TS2 = "2026-11-05T00:00:00+06:00"
ROWS = [
    ("greenhouse", "imc:123", "https://x.example/gh"),
    ("rss", "HN Jobs:x", "https://x.example/rss"),
    ("pagewatch", "pw1", "https://x.example/page"),
    ("hn", "999", "https://x.example/hn"),
    ("telegram_channels", "chan:5", "https://t.me/chan/5"),
]
WANT = {
    "greenhouse": "imc",
    "rss": "HN Jobs",
    "pagewatch": "https://x.example/page",
    "hn": "",
    "telegram_channels": "chan",
}


def _v7_file(path):
    c = sqlite3.connect(str(path))
    for s in db._MIGRATIONS[:7]:
        for stmt in s.split(";"):
            if stmt.strip():
                c.execute(stmt)
    c.execute("PRAGMA user_version=7")
    for src, ext, url in ROWS:
        c.execute(
            "INSERT INTO postings (id, source, external_id, title, url, fingerprint,"
            " status, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (src + ext, src, ext, "T", url, "fp" + ext, "scored", TS, TS2),
        )
    c.execute("INSERT INTO deliveries VALUES ('hn999','digest','1',5,?)", (TS,))
    c.execute("INSERT INTO page_snapshots VALUES ('https://x.example/snap','h',?)", (TS2,))
    c.execute("INSERT INTO source_runs (source, started_at, ok) VALUES ('hh',?,1)", (TS,))
    c.execute("INSERT INTO source_runs (source, started_at, ok) VALUES ('greenhouse',?,1)", (TS,))
    c.commit()
    c.close()


def _snapshot(conn):
    return [
        [tuple(r) for r in conn.execute(q)]
        for q in (
            "SELECT * FROM postings ORDER BY id",
            "SELECT * FROM target_state ORDER BY source, target",
            "SELECT * FROM deliveries",
        )
    ]


def test_v7_to_v8_backfill_and_idempotent(tmp_path):
    path = tmp_path / "v7.db"
    _v7_file(path)
    conn = db.connect(path)
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    for r in conn.execute("SELECT * FROM postings"):
        assert r["target"] == WANT[r["source"]]
        assert r["baseline"] == 1
        assert r["first_seen_at"] == TS and r["last_seen_at"] == TS2
        assert r["content_hash"] == "" and r["source_published_at"] is None
    states = {(r["source"], r["target"]): r for r in conn.execute("SELECT * FROM target_state")}
    assert set(WANT.items()) <= set(states)
    for k in WANT.items():
        assert states[k]["baselined_at"] == TS
        assert states[k]["last_ok_at"] == TS2
    assert ("pagewatch", "https://x.example/snap") in states
    assert ("hh", "") in states and states[("hh", "")]["baselined_at"] == TS
    assert ("greenhouse", "") not in states
    assert conn.execute("SELECT latency_s FROM deliveries").fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM host_state").fetchone()[0] == 0
    idx = {r["name"] for r in conn.execute("PRAGMA index_list(postings)")}
    assert {"idx_postings_first_seen", "idx_postings_source_target"} <= idx
    before = _snapshot(conn)
    db.migrate(conn)
    assert _snapshot(conn) == before


def test_fresh_db_is_v8():
    conn = db.connect(":memory:")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 8
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(postings)")}
    assert {"target", "source_published_at", "content_hash", "baseline"} <= cols
    assert conn.execute("SELECT COUNT(*) FROM target_state").fetchone()[0] == 0
