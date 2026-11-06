import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from src import db
from src.clock import FixedClock
from src.config import AppConfig
from src.models import Kind, Posting
from src.pipeline.dedupe import fingerprint
from src.pipeline.filters import apply_filters, hard_filter, relevance_ok

CASES = json.loads((Path(__file__).parent / "fixtures" / "filter_cases.json").read_text())
CFG = AppConfig.load(Path(__file__).parent.parent / "config.toml")
TODAY = date(2026, 11, 6)
NOW = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)


def make(case: dict) -> Posting:
    return Posting(
        source="t",
        external_id=case["name"],
        title=case["title"],
        company="Acme",
        url="https://example.com/x",
        description=case.get("description", ""),
        kind=Kind(case["kind"]),
        deadline=date.fromisoformat(case["deadline"]) if case.get("deadline") else None,
        posted_at=datetime.fromisoformat(case["posted_at"]) if case.get("posted_at") else None,
    )


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_hard_filter_cases(case):
    assert hard_filter(make(case), CFG, TODAY) == case["expect"]


def _p(title, kind=Kind.JOB, **kw):
    return Posting(
        source="t", external_id=title, title=title, company="Acme",
        url="https://example.com/x", kind=kind, **kw,
    )  # fmt: skip


@pytest.mark.parametrize("title", ["Office Manager", "Account Executive"])
def test_irrelevant_titles(title):
    assert not relevance_ok(_p(title), CFG)
    assert not relevance_ok(_p(title, Kind.INTERNSHIP), CFG)


@pytest.mark.parametrize(
    "title", ["Quant Intern", "Summer Internship", "ML Platform", "Research Scientist"]
)
def test_relevant_titles(title):
    assert relevance_ok(_p(title), CFG)


def test_short_keyword_whole_word_only():
    assert not relevance_ok(_p("Retail Assistant"), CFG)  # "ai" inside words must not match


def test_non_job_kinds_skip_relevance():
    assert relevance_ok(_p("Office Manager", Kind.HACKATHON), CFG)
    assert relevance_ok(_p("Welcome Party", Kind.FELLOWSHIP), CFG)


def test_apply_filters_sets_status():
    conn = db.connect(":memory:")
    db.migrate(conn)
    posts = [
        _p("Senior Quant Researcher"),
        _p("Office Manager"),
        _p("Quant Intern", Kind.INTERNSHIP),
        _p("Hack Night", Kind.HACKATHON),
    ]
    for p in posts:
        db.upsert_posting(conn, p, fingerprint(p), NOW)
    counts = apply_filters(conn, CFG, FixedClock(NOW))
    assert counts == {"senior_only": 1, "irrelevant": 1}
    got = {
        r["title"]: (r["status"], r["filter_reason"])
        for r in conn.execute("SELECT * FROM postings")
    }
    assert got["Senior Quant Researcher"] == ("filtered", "senior_only")
    assert got["Office Manager"] == ("filtered", "irrelevant")
    assert got["Quant Intern"] == ("new", None)
    assert got["Hack Night"] == ("new", None)
