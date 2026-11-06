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
CFG = AppConfig.load(Path(__file__).parent / "fixtures" / "config_repo_snapshot.toml")
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


def _p(title, kind=Kind.JOB, source="t", **kw):
    return Posting(
        source=source, external_id=title, title=title, company="Acme",
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


@pytest.mark.parametrize("title", ["Senior Internal Tools Engineer", "Head of International Sales"])
def test_internal_does_not_exempt_senior_only(title):
    assert hard_filter(_p(title), CFG, TODAY) == "senior_only"


def test_senior_intern_still_exempt():
    assert hard_filter(_p("Senior Software Engineer Intern", Kind.INTERNSHIP), CFG, TODAY) is None


def test_internal_communications_not_relevant():
    assert not relevance_ok(_p("Internal Communications Manager"), CFG)


@pytest.mark.parametrize(
    "title", ["Intern", "Internship", "Interns", "Summer Intern", "Quant Research Intern"]
)
def test_real_internships_relevant(title):
    assert relevance_ok(_p(title, Kind.INTERNSHIP), CFG)


def _run(posts):
    conn = db.connect(":memory:")
    db.migrate(conn)
    for p in posts:
        db.upsert_posting(conn, p, fingerprint(p), NOW)
    apply_filters(conn, CFG, FixedClock(NOW))
    return {
        (r["source"], r["title"]): (r["status"], r["filter_reason"])
        for r in conn.execute("SELECT * FROM postings")
    }


def test_pagewatch_items_skip_relevance():
    titles = ["IMC Careers", "IMC Careers: page updated", "Optiver Jobs"]
    got = _run([_p(t, Kind.INTERNSHIP, source="pagewatch") for t in titles])
    assert all(got[("pagewatch", t)] == ("new", None) for t in titles)


def test_russian_titles_relevant():
    titles = [
        "Аналитик данных",
        "Стажер-аналитик данных",
        "Python-разработчик",
        "Аналитик данных (стажировка)",
    ]
    posts = [_p(t, source="hh") for t in titles[:3]] + [
        _p(titles[3], Kind.INTERNSHIP, source="telegram")
    ]
    got = _run(posts)
    assert all(v == ("new", None) for v in got.values())


def test_irrelevant_still_filtered_via_apply_filters():
    titles = ["Office Manager", "Account Executive", "Internal Communications Manager"]
    got = _run([_p(t, source="hh") for t in titles + ["Менеджер по продажам"]])
    assert all(v == ("filtered", "irrelevant") for v in got.values())
    assert len(got) == 4


@pytest.mark.parametrize(
    "title", ["Junior/Senior Data Scientist", "Senior Graduate Analyst", "Lead Trainee Program"]
)
def test_junior_markers_waive_senior_only(title):
    assert hard_filter(_p(title), CFG, TODAY) is None


def test_senior_only_still_applies():
    assert hard_filter(_p("Senior Quant Researcher"), CFG, TODAY) == "senior_only"


def test_unpaid_fellowship_passes():
    p = _p("Research Fellowship", Kind.FELLOWSHIP, description="This is an unpaid fellowship")
    assert hard_filter(p, CFG, TODAY) is None


@pytest.mark.parametrize(
    "title",
    [
        "Старший аналитик данных",
        "Ведущий разработчик Python",
        "Руководитель отдела аналитики данных",
        "Главный инженер",
        "Senior Data Scientist (стаж от 5 лет)",
    ],
)
def test_russian_senior_titles_filtered(title):
    assert hard_filter(_p(title), CFG, TODAY) == "senior_only"


def test_russian_trainee_waives_senior_only():
    assert hard_filter(_p("Старший стажер-аналитик"), CFG, TODAY) is None
    assert hard_filter(_p("Ведущий стажёр-аналитик"), CFG, TODAY) is None


def test_russian_juniors_still_new():
    titles = ["Стажер-аналитик данных", "Аналитик данных (стажировка)", "Аналитик данных"]
    got = _run([_p(t, source="hh") for t in titles])
    assert all(v == ("new", None) for v in got.values())


def test_apprentice_irrelevant_scientist_relevant():
    got = _run([_p(t, source="hh") for t in ["Ученик повара", "Ученик продавца-консультанта"]])
    assert all(v == ("filtered", "irrelevant") for v in got.values())
    for t in ["Учёный-физик", "Ученый по данным", "Ученые данных"]:
        assert relevance_ok(_p(t), CFG)


def test_published_at_never_affects_stale_filter():
    """Stale depends on posted_at only (D-185): a fresh published_at cannot rescue an old one."""
    old = datetime(2026, 1, 1, tzinfo=UTC)
    fresh = datetime(2026, 11, 5, tzinfo=UTC)
    base = dict(source="t", external_id="1", title="Data Intern", company="A", kind=Kind.INTERNSHIP)
    p = Posting(url="https://e.com/1", posted_at=old, published_at=fresh, **base)
    q = Posting(url="https://e.com/1", posted_at=old, **base)
    assert hard_filter(p, CFG, TODAY) == hard_filter(q, CFG, TODAY) == "stale"
    r = Posting(url="https://e.com/1", posted_at=fresh, published_at=old, **base)
    assert hard_filter(r, CFG, TODAY) is None


def _ai_ml_cfg():
    cfg = AppConfig.load(Path(__file__).parent / "fixtures" / "config_repo_snapshot.toml")
    cfg.filters["positive_title_keywords"] = ["ai", "ml"]
    return cfg


@pytest.mark.parametrize(
    "title",
    ["Retail Assistant", "Email Specialist", "Maintenance Technician", "HTML Coder", "Chairperson"],
)
def test_ai_ml_substring_rejected(title):
    assert not relevance_ok(_p(title), _ai_ml_cfg())


@pytest.mark.parametrize(
    "title", ["AI Engineer", "ML/AI Intern", "Gen-AI Researcher", "ML-Ops Intern"]
)
def test_ai_ml_whole_word_accepted(title):
    assert relevance_ok(_p(title), _ai_ml_cfg())
