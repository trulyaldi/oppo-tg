import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src import db
from src.clock import FixedClock
from src.config import AppConfig, CvReviewConfig
from src.cues import load_cues
from src.cv.market import market_summary

FX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).parent.parent
NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)
CUES = load_cues(ROOT / "config" / "cues.toml")
CFG = CvReviewConfig()


def _load(conn, rows):
    for r in rows:
        ts = (NOW - timedelta(days=r["days_ago"])).isoformat()
        conn.execute(
            "INSERT INTO postings (id, source, external_id, title, url, description, fingerprint,"
            " status, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r["id"], "t", r["id"], r["title"], "http://x/" + r["id"], r["description"],
             r["id"], "scored", ts, ts),
        )  # fmt: skip
        conn.execute(
            "INSERT INTO scores (posting_id, base_score, adjusted_score, reason, model,"
            " prompt_version, scored_at, employer_category) VALUES (?,?,?,?,?,?,?,?)",
            (r["id"], r["score"], r["score"], "r", "m", "v", ts, r["category"]),
        )  # fmt: skip
        if r["liked"]:
            conn.execute(
                "INSERT INTO reactions (posting_id, action, active, updated_at)"
                " VALUES (?, 'like', 1, ?)",
                (r["id"], ts),
            )
    conn.commit()


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "oppo.db")
    db.migrate(c)
    _load(c, json.loads((FX / "market_sample.json").read_text(encoding="utf-8")))
    yield c
    c.close()


def _by_label(bucket):
    return {label: (count, share) for label, count, share in bucket.groups}


def test_fixture_market_buckets(conn):
    m = market_summary(conn, CUES, CvReviewConfig(top_n=50), FixedClock(NOW))
    assert (m.kz.n_postings, m.intl.n_postings) == (12, 9)
    assert m.kz.enough and m.intl.enough
    kz = _by_label(m.kz)
    assert kz["python"] == (9, 0.75)  # kz1-8 plus the liked kz12; repeats count once
    assert kz["sql"] == (9, 0.75)
    assert kz["english"] == (5, round(5 / 12, 4))
    assert kz["degree_master"] == (5, round(5 / 12, 4))
    assert kz["experience_years"] == (6, 0.5)
    assert kz["kazakh"][0] == 1  # the liked posting below the threshold counts
    intl = _by_label(m.intl)
    assert intl["python"] == (6, round(6 / 9, 4))
    assert intl["degree_phd"] == (5, round(5 / 9, 4))
    assert intl["experience_years"] == (5, round(5 / 9, 4))
    assert intl["cpp"] == (3, round(3 / 9, 4))
    assert intl["degree_bachelor"] == (3, round(3 / 9, 4))
    assert intl["docker"] == (1, round(1 / 9, 4))
    assert "kazakh" not in intl


def test_top_groups_sorted_and_capped(conn):
    m = market_summary(conn, CUES, CvReviewConfig(top_n=3), FixedClock(NOW))
    assert len(m.kz.groups) == 3 and len(m.intl.groups) == 3
    counts = [c for _, c, _ in m.kz.groups]
    assert counts == sorted(counts, reverse=True)
    assert all(0 < share <= 1 for _, _, share in m.kz.groups)


def test_threshold_and_window(conn):
    m = market_summary(conn, CUES, CvReviewConfig(market_min_score=6.0), FixedClock(NOW))
    assert (m.kz.n_postings, m.intl.n_postings) == (13, 10)  # unknown and old stay out
    m = market_summary(conn, CUES, CvReviewConfig(market_days=40), FixedClock(NOW))
    assert m.kz.n_postings == 13


def test_inactive_like_does_not_count(conn):
    conn.execute("UPDATE reactions SET active=0")
    conn.commit()
    m = market_summary(conn, CUES, CFG, FixedClock(NOW))
    assert (m.kz.n_postings, m.intl.n_postings) == (11, 8)


def test_small_bucket_not_enough(conn):
    ids = ",".join(f"'kz{i}'" for i in range(1, 9))
    conn.execute(f"DELETE FROM scores WHERE posting_id IN ({ids})")
    conn.commit()
    m = market_summary(conn, CUES, CFG, FixedClock(NOW))
    assert m.kz.n_postings == 4 and not m.kz.enough
    assert m.intl.enough


def test_empty_database(tmp_path):
    c = db.connect(tmp_path / "e.db")
    db.migrate(c)
    m = market_summary(c, CUES, CFG, FixedClock(NOW))
    assert m.kz.groups == [] and not m.kz.enough and m.intl.n_postings == 0


def test_config_defaults_and_validation():
    cfg = AppConfig.load(FX / "config_repo_snapshot.toml")
    assert CvReviewConfig.from_cfg(cfg) == CvReviewConfig(30, 7.0, 10, 5)
    for bad in ({"top_n": 0}, {"market_days": True}, {"market_min_score": 11}, {"nope": 1}):
        with pytest.raises(ValueError):
            AppConfig.from_dict({"cv_review": bad})


def test_no_llm_import():
    text = (ROOT / "src" / "cv" / "market.py").read_text(encoding="utf-8")
    assert "anthropic" not in text and "call_json" not in text
