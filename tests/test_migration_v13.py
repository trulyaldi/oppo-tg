import sqlite3

import pytest

from src import db

TS = "2026-11-01T00:00:00+06:00"
MAPPED = {
    "job": "job",
    "internship": "internship",
    "startup": "startup_role",
    "other": "other",
    "hackathon": "hackathon",
}


def _v12_db(path):
    c = sqlite3.connect(str(path))
    for s in db._MIGRATIONS[:12]:
        for stmt in s.split(";"):
            if stmt.strip():
                c.execute(stmt)
    c.execute("PRAGMA user_version=12")
    for i, kind in enumerate(MAPPED):
        c.execute(
            "INSERT INTO postings (id, source, external_id, title, url, fingerprint, kind,"
            " status, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                f"p{i}",
                "hn",
                str(i),
                "T",
                f"https://x.example/{i}",
                f"fp{i}",
                kind,
                "scored",
                TS,
                TS,
            ),
        )
        c.execute(
            "INSERT INTO scores (posting_id, base_score, adjusted_score, reason, model,"
            " prompt_version, scored_at) VALUES (?,?,?,?,?,?,?)",
            (f"p{i}", 7.0, 7.0, "r", "m", "v", TS),
        )
    c.commit()
    c.close()


def test_v12_to_v13_backfills_and_is_idempotent(tmp_path):
    path = tmp_path / "v12.db"
    _v12_db(path)
    conn = db.connect(path)
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 13
    before = [tuple(r) for r in conn.execute("SELECT * FROM postings ORDER BY id")]
    for i, kind in enumerate(MAPPED):
        row = conn.execute("SELECT * FROM postings WHERE id=?", (f"p{i}",)).fetchone()
        assert row["opportunity_type"] == MAPPED[kind] and row["type_source"] == "kind"
        assert row["first_seen_at"] == TS and row["status"] == "scored"
        assert row["language"] is None and row["repost_key"] is None
        sc = conn.execute("SELECT * FROM scores WHERE posting_id=?", (f"p{i}",)).fetchone()
        assert sc["contract"] == 1 and sc["reason_generic"] == 0
    names = {r["name"] for r in conn.execute("PRAGMA index_list(postings)")}
    assert "idx_postings_repost_key" in names
    db.migrate(conn)
    assert [tuple(r) for r in conn.execute("SELECT * FROM postings ORDER BY id")] == before


def test_fresh_db_goes_straight_to_v13(tmp_path):
    conn = db.connect(tmp_path / "new.db")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(triage_results)")}
    assert {"type", "language"} <= cols


def _one(tmp_path):
    from datetime import UTC, datetime

    from src.models import Kind, Posting

    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    p = Posting(
        source="hn",
        external_id="1",
        title="T",
        company="C",
        url="https://x.example/1",
        kind=Kind.STARTUP,
    )
    db.upsert_posting(conn, p, "fp", datetime(2026, 11, 1, tzinfo=UTC))
    return conn, p.id


def test_upsert_sets_type_on_insert_only(tmp_path):
    conn, pid = _one(tmp_path)
    row = conn.execute("SELECT * FROM postings WHERE id=?", (pid,)).fetchone()
    assert row["opportunity_type"] == "startup_role" and row["type_source"] == "kind"


def test_set_opportunity_type_precedence(tmp_path):
    conn, pid = _one(tmp_path)

    def get():
        r = conn.execute(
            "SELECT opportunity_type t, type_source s FROM postings WHERE id=?", (pid,)
        )
        r = r.fetchone()
        return r["t"], r["s"]

    assert db.set_opportunity_type(conn, pid, "summer_school", "triage")
    assert get() == ("summer_school", "triage")
    assert not db.set_opportunity_type(conn, pid, "job", "kind")
    assert not db.set_opportunity_type(conn, pid, "job", "config")
    assert get() == ("summer_school", "triage")
    assert db.set_opportunity_type(conn, pid, "grant", "scorer")
    assert not db.set_opportunity_type(conn, pid, "job", "triage")
    assert get() == ("grant", "scorer")
    with pytest.raises(ValueError):
        db.set_opportunity_type(conn, pid, "bogus", "scorer")
    with pytest.raises(ValueError):
        db.set_opportunity_type(conn, pid, "job", "nobody")
