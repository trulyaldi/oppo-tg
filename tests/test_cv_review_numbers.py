"""FIX-53 fabrication traps: numbers and claims come from the CV text only (D-306)."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_cv_review import CFG, CV, SKILLS, Fake

from src import db
from src.bot.outreach_handlers import review_summary_chunks
from src.clock import FixedClock
from src.cv import review as rv
from src.cv import store

FX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 8, 5, tzinfo=UTC)
MARKET = "KZ employers (12 postings): ml 5 (42%); python 7 (58%)"
REPLY = (FX / "cv_review_reply_market_numbers.json").read_text()
FORBIDDEN = ["5 years", "42%", "95%", "Google internship"]


def test_market_numbers_and_invented_claims_do_not_survive():
    v, removed, questions = rv.verify_review(json.loads(REPLY), CV, MARKET, SKILLS)
    blob = json.dumps(v) + rv.render_report(v, removed, questions)
    for bad in FORBIDDEN:
        assert bad not in blob
    assert [c["what"] for c in v["changes"]] == [
        "Keep it to one page",
        "Move SKILLS above PROJECTS",
    ]
    # allowlisted advice numbers still pass in why and notes
    assert v["changes"][0]["why"] == "Recruiters skim 1-2 pages."
    assert v["report"]["intl_notes"] == "Use a one-page English CV."
    assert v["report"]["kz_notes"] == ""
    assert [g["gap"] for g in v["gaps"]] == ["Docker"]
    assert v["gaps"][0]["note"] == ""


def test_gap_claim_about_user_removed_but_question_path_kept():
    out = {
        "gaps": [
            {"gap": "Kubernetes", "bucket": "both", "mark": "add_if_true", "note": ""},
            {"gap": "your Google internship experience", "mark": "add_if_true"},
            {"gap": "Google internship experience", "mark": "add_if_true"},
        ]
    }
    v, removed, questions = rv.verify_review(out, CV, MARKET, SKILLS)
    assert [g["gap"] for g in v["gaps"]] == ["Kubernetes"]
    assert questions == 1 and removed >= 2


def test_cv_numbers_still_allowed_in_advice():
    out = {"changes": [{"what": "Lead with the 30% replay-time reduction", "why": "In your CV."}]}
    v, _, _ = rv.verify_review(out, CV, MARKET, SKILLS)
    assert len(v["changes"]) == 1


@pytest.fixture
def conn(tmp_path):
    store.on_new_cv_version.clear()
    store.on_forget_cv.clear()
    rv.register_hooks()
    c = db.connect(tmp_path / "d.db")
    db.migrate(c)
    store.save_cv(c, CV, json.dumps({"skills": SKILLS}), "text", "m", NOW)
    return c


def test_market_numbers_reach_no_chat_chunk(conn):
    res = asyncio.run(rv.review_cv(conn, Fake(REPLY), CFG, FixedClock(NOW)))
    everything = "\n".join(review_summary_chunks(res)) + res.report_md + json.dumps(res.summary)
    for bad in FORBIDDEN:
        assert bad not in everything
