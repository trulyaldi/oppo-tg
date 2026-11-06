"""FIX-31: written-out numbers are checked like digits (D-125 addendum)."""

import json
from pathlib import Path

import pytest

from src.outreach.verify import (
    call_length_numbers,
    claim_ok,
    number_words,
    numbers,
    verify_draft,
)

FX = Path(__file__).parent / "fixtures"
P = json.loads((FX / "entity_probes.json").read_text())
CV = (FX / "cv_fake.txt").read_text()
POST, CONTACT = P["posting"], P["contact"]
SKILLS = ["Python", "C++", "Rust", "SQL"]
ASK = "Could we have a 15-minute call?"
SL = "Sample Labs - Research Intern, Summer 2025"
HARN = "Built a latency benchmark harness in Python for order book replay"


def run_one(sentence, ask=ASK):
    body = f"{P['greeting']} {sentence} I would love to join the research team at Acme Fake. {ask}"
    d = {
        "body": body,
        "ask": ask,
        "hook": {"text": "h", "quote": P["hook_quote"], "from": "posting"},
        "claims": [],
    }
    v = verify_draft(d, CV, POST, CONTACT, SKILLS)
    return v.ok and sentence in v.body


@pytest.mark.parametrize(
    "sentence",
    [
        "I built a harness used by thousands of traders.",
        "My parser cut replay time in half.",
        "I reduced replay time tenfold with a C++ parser.",
        "I mentored five interns at Sample Labs.",
        "My parser made replay twice as fast.",
        "I doubled replay speed with a C++ parser.",
        "Мой парсер ускорил replay вдвое.",
        "Я помог тысячам трейдеров.",
    ],
)
def test_invented_written_metrics_dropped_from_drafts(sentence):
    assert not run_one(sentence)


@pytest.mark.parametrize(
    "text",
    [
        "Stress that the harness served thousands of users",
        "Stress your two years of Python experience",
    ],
)
def test_invented_written_metrics_dropped_from_claims(text):
    assert not claim_ok(text, SL, CV, SKILLS)
    assert not claim_ok(text, HARN, CV, SKILLS)


def test_gap_with_invented_written_number_removed():
    from src.outreach.verify import verify_tailoring

    res = {
        "gaps": [
            {
                "requirement": "x",
                "posting_evidence": "",
                "how_to_address": "Mention your five years of C++.",
            }
        ]
    }
    v, removed = verify_tailoring(res, CV, POST, SKILLS)
    assert not v["gaps"] and removed == 1


def test_written_number_matches_digit_in_evidence():
    assert (
        claim_ok(
            "Stress the thirty percent replay speedup",
            "Reduced replay time by 30% with a C++ parser",
            CV,
            SKILLS,
        )
        is False
    )  # "30%" and "30" differ: fail closed
    assert numbers("two years") == numbers("2 years")
    assert numbers("пять лет") == {"5"}


def test_leaves_one_and_ordinals_alone():
    assert number_words("one thing first third-year second") == set()
    assert run_one("I'm a third-year Computer Science student.")
    assert run_one("One thing I enjoy is research tooling.")


def test_fold_and_plurals():
    assert number_words("tenfold threefold") == {"tenfold", "threefold"}
    assert number_words("hundreds dozens millions") == {"hundred", "dozen", "million"}
    assert number_words("fold") == set()


def test_call_length_words_allowed_in_ask():
    assert call_length_numbers("Could we have a fifteen-minute call?") == {"15"}
    assert call_length_numbers("Could we have a 15-minute call?") == {"15"}


def test_honest_sentences_still_kept():
    assert run_one("I reduced replay time by 30% with a C++ parser.")
    assert run_one(
        "At Sample Labs I built a latency benchmark harness in Python for order book replay."
    )
    assert run_one("I made the Dean's list in 2024 and 2025.")
