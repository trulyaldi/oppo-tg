"""FIX-32: a number found only in the posting cannot be claimed as the user's own (D-157)."""

import json
from pathlib import Path

import pytest

from src.outreach.verify import verify_draft, verify_tailoring

FX = Path(__file__).parent / "fixtures"
P = json.loads((FX / "entity_probes.json").read_text())
CV = (FX / "cv_fake.txt").read_text()
POSTING = "We need 2+ years of Python experience and 3 years with C++. Summer 2026 start."
CONTACT = "Sam Doe, Engineering Manager at Acme Fake"
SKILLS = ["Python", "C++", "Rust", "SQL"]
QUOTE = "We need 2+ years of Python experience"
GREET = "Hi Sam, I am excited about Acme Fake's work on research tooling."
INTENT = "I would love to join the research team at Acme Fake."


def kept(sentence, ask="Could we have a 15-minute call?", posting=POSTING):
    body = f"{GREET} {sentence} {INTENT} {ask}"
    d = {
        "body": body,
        "ask": ask,
        "hook": {"text": "h", "quote": QUOTE, "from": "posting"},
        "claims": [],
    }
    v = verify_draft(d, CV, posting, CONTACT, SKILLS)
    return v.ok and sentence in v.body


@pytest.mark.parametrize(
    "sentence",
    [
        "I have 2 years of Python experience.",
        "I have used C++ for 3 years.",
        "I have 2+ years of Python.",
        "I have three years of C++ experience.",
        "My 3 years with C++ shaped how I write parsers.",
    ],
)
def test_posting_number_as_own_experience_dropped_from_drafts(sentence):
    assert not kept(sentence)


@pytest.mark.parametrize(
    "sentence",
    [
        "I reduced replay time by 30% with a C++ parser.",
        "The posting mentions 3 years of C++.",
        "Your team wants 2+ years of Python experience.",
        "I made the Dean's list in 2024 and 2025.",
        "I am excited about the Summer 2026 start.",
        "I study Computer Science and graduate in 2026.",
    ],
)
def test_honest_numbers_kept_in_drafts(sentence):
    assert kept(sentence)


def test_ask_call_length_kept_and_other_ask_numbers_not():
    assert kept("I enjoy replay tooling.", ask="Could I have a 15-minute call?")
    assert kept("I enjoy replay tooling.", ask="Could I have a fifteen-minute call?")


def test_call_length_exemption_does_not_cover_other_sentences():
    assert not kept("I have 15 years of Python.", ask="Could we have a 15-minute call?")


def gap(how):
    res = {"gaps": [{"requirement": "x", "posting_evidence": "", "how_to_address": how}]}
    return verify_tailoring(res, CV, POSTING, SKILLS)


@pytest.mark.parametrize(
    "how",
    [
        "Mention your 3 years of C++.",
        "Say you have 2 years of Python experience.",
        "Say you've used C++ for three years.",
    ],
)
def test_gap_claiming_posting_number_removed(how):
    v, removed = gap(how)
    assert not v["gaps"] and removed == 1


@pytest.mark.parametrize(
    "how",
    [
        "The role asks for 3 years of C++; point to the C++ parser.",
        "Point to your C++ parser; the posting wants 3 years.",
        "Mention the 30% speedup from your C++ parser.",
        "Not in your CV; try a small side project.",
    ],
)
def test_gap_naming_posting_number_without_user_claim_kept(how):
    v, removed = gap(how)
    assert len(v["gaps"]) == 1 and removed == 0


def test_fail_closed_cost_is_recorded():
    # "I noticed the posting mentions 3 years of C++." is dropped: first person plus a
    # posting-only number. Documented in D-157.
    assert not kept("I noticed the posting mentions 3 years of C++.")


# ---- FIX-36: imperative gap tips (D-162) ----

TAILOR_POSTING = (
    "Acme Fake is hiring a research tooling intern. We need 2+ years of Python experience "
    "and 3 years with C++. The program runs for 10 weeks in Summer 2026 and serves thousands "
    "of analysts."
)


def _gap_kept(how):
    reply = {
        "gaps": [
            {"requirement": "Python experience", "posting_evidence": "", "how_to_address": how}
        ],
    }
    v, removed = verify_tailoring(reply, CV, TAILOR_POSTING, SKILLS)
    return bool(v["gaps"]) and removed == 0


@pytest.mark.parametrize(
    "how",
    [
        "Mention 2+ years of Python experience.",
        "Stress 2+ years of Python experience.",
        "Highlight 2+ years of Python across the internship and projects.",
        "Note 3 years of C++ from projects and internship.",
        "Show 2+ years of Python through the harness and engine.",
        "Claim 3 years of C++ experience.",
        "Point to two years of Python work at Sample Labs.",
        "Present the Sample Labs internship as 10 weeks long.",
        "Frame the harness as serving thousands of analysts.",
        "Mention 2+ years of Python, since the role asks for it.",
    ],
)
def test_imperative_gap_with_posting_number_removed(how):
    assert not _gap_kept(how)


@pytest.mark.parametrize(
    "how",
    [
        "The role asks for 3 years of C++; point to the C++ parser.",
        "Your CV shows one internship; the role wants 2+ years, so stress project depth.",
        "Point to the C++ parser that cut replay time by 30%.",
        "Highlight the Summer 2025 internship at Sample Labs.",
    ],
)
def test_attributed_or_cv_numbers_in_gap_kept(how):
    assert _gap_kept(how)
