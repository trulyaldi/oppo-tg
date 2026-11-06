from datetime import UTC, date, datetime, timedelta

import pytest

from src import db
from src.models import Kind, Posting, ScoreResult

T0 = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
TABLES = {
    "postings", "scores", "deliveries", "reactions", "reaction_log", "source_runs",
    "http_cache", "robots_cache", "state", "page_snapshots",
}  # fmt: skip


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    yield c
    c.close()


def mk(n: int = 1, **kw) -> Posting:
    return Posting(
        source="gh",
        external_id=str(n),
        title=f"Intern {n}",
        company="Acme",
        url=f"https://x.test/{n}",
        kind=Kind.INTERNSHIP,
        raw_tags=("ml",),
        deadline=date(2026, 12, 1),
        **kw,
    )


def add(conn, n=1, now=T0):
    p = mk(n)
    db.upsert_posting(conn, p, f"fp{n}", now)
    return p


def test_migrate_idempotent(tmp_path):
    c = db.connect(tmp_path / "m.db")
    db.migrate(c)
    db.migrate(c)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 4
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names >= TABLES
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_upsert_new_then_existing(conn):
    p = mk()
    assert db.upsert_posting(conn, p, "fp", T0) is True
    before = dict(db.get_posting(conn, p.id))
    later = T0 + timedelta(hours=3)
    assert db.upsert_posting(conn, p, "other-fp", later) is False
    after = dict(db.get_posting(conn, p.id))
    assert after["last_seen_at"] == later.isoformat()
    before.pop("last_seen_at")
    after.pop("last_seen_at")
    assert before == after
    assert before["status"] == "new" and before["fingerprint"] == "fp"
    assert conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 1


def test_status_and_scoring_flow(conn):
    ps = [add(conn, i, T0 + timedelta(minutes=i)) for i in (1, 2, 3)]
    assert [r["id"] for r in db.pending_for_scoring(conn, 10)] == [p.id for p in reversed(ps)]
    assert len(db.pending_for_scoring(conn, 2)) == 2
    db.set_status(conn, ps[0].id, "filtered", "spam")
    row = db.get_posting(conn, ps[0].id)
    assert (row["status"], row["filter_reason"]) == ("filtered", "spam")
    assert ps[0].id not in [r["id"] for r in db.pending_for_scoring(conn, 10)]

    for _ in range(3):
        db.record_score_error(conn, ps[1].id)
    assert db.get_posting(conn, ps[1].id)["status"] == "score_error"
    assert ps[1].id not in [r["id"] for r in db.pending_for_scoring(conn, 10)]

    res = ScoreResult(score=8.5, reason="good", deadline=date(2026, 11, 20), tags=["ml"])
    db.save_score(conn, ps[2].id, res, "m", "v1", T0)
    assert db.get_posting(conn, ps[2].id)["status"] == "scored"
    un = db.undelivered_scored(conn)
    assert [r["id"] for r in un] == [ps[2].id]
    assert un[0]["score"] == 8.5 and un[0]["llm_deadline"] == "2026-11-20"
    db.record_delivery(conn, ps[2].id, "digest", "1", 5, T0)
    assert db.undelivered_scored(conn) == []


def test_record_delivery_once(conn):
    p = add(conn)
    assert db.is_delivered(conn, p.id) is False
    assert db.record_delivery(conn, p.id, "digest", "42", 7, T0) is True
    assert db.record_delivery(conn, p.id, "alert", "42", 8, T0) is False
    assert db.is_delivered(conn, p.id) is True
    rows = conn.execute("SELECT * FROM deliveries").fetchall()
    assert len(rows) == 1 and rows[0]["kind"] == "digest"


def test_reactions(conn):
    p = add(conn)
    r = db.set_reaction(conn, p.id, "like", T0)
    assert r == {"like": True, "dislike": False, "save": False, "applied": False}
    r = db.set_reaction(conn, p.id, "dislike", T0 + timedelta(seconds=1))
    assert r["dislike"] and not r["like"]
    r = db.set_reaction(conn, p.id, "dislike", T0 + timedelta(seconds=2))
    assert not r["dislike"] and not r["like"]
    db.set_reaction(conn, p.id, "save", T0)
    r = db.set_reaction(conn, p.id, "applied", T0)
    assert r["save"] and r["applied"]
    assert db.active_reactions(conn, p.id) == r
    assert conn.execute("SELECT COUNT(*) FROM reaction_log").fetchone()[0] == 5
    with pytest.raises(ValueError):
        db.set_reaction(conn, p.id, "bogus", T0)


def test_recent_feedback_examples(conn):
    ids = []
    for i in range(25):
        p = add(conn, i)
        ids.append(p.id)
        db.set_reaction(conn, p.id, "like", T0 + timedelta(seconds=i))
    db.set_reaction(conn, ids[24], "dislike", T0 + timedelta(minutes=5))  # clears its like
    db.set_reaction(conn, ids[23], "like", T0 + timedelta(minutes=6))  # toggles off
    ex = db.recent_feedback_examples(conn, "like", 20)
    assert len(ex) == 20
    assert [e["posting_id"] for e in ex] == list(reversed(ids[3:23]))
    assert ex[0]["title"] == "Intern 22" and ex[0]["tags"] == ["ml"]
    assert [e["posting_id"] for e in db.recent_feedback_examples(conn, "dislike")] == [ids[24]]
    assert len(db.list_by_reaction(conn, "like", 5)) == 5


def test_state(conn):
    assert db.get_state(conn, "paused") is None
    assert db.get_state(conn, "paused", "0") == "0"
    db.set_state(conn, "paused", "1")
    db.set_state(conn, "paused", "0")
    assert db.get_state(conn, "paused") == "0"


def test_source_runs(conn):
    r1 = db.start_source_run(conn, "gh", T0)
    db.finish_source_run(conn, r1, T0, ok=False, error="boom")
    r2 = db.start_source_run(conn, "gh", T0 + timedelta(hours=1))
    db.finish_source_run(conn, r2, T0 + timedelta(hours=1), ok=True, n_items=5, n_new=2)
    r3 = db.start_source_run(conn, "hn", T0)
    runs = db.last_source_runs(conn)
    assert [r["source"] for r in runs] == ["gh", "hn"]
    assert runs[0]["ok"] == 1 and runs[0]["n_items"] == 5 and runs[0]["n_new"] == 2
    assert runs[1]["id"] == r3 and runs[1]["finished_at"] is None


def test_connect_creates_missing_parent_dirs(tmp_path):
    path = tmp_path / "missing" / "sub" / "oppo.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()
    assert path.is_file()
