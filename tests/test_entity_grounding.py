"""FIX-15: invented employers, schools and degrees are caught with no number, inflation
word or skill term in the fabricated text (D-140)."""

import json
from datetime import UTC, datetime
from pathlib import Path

from src import db
from src.clock import FixedClock
from src.config import OutreachConfig
from src.cv.parse import parse_cv_sections
from src.llm.budget import LlmBudget
from src.outreach.verify import (
    entities,
    inflation_stems,
    numbers,
    skill_terms,
    verify_draft,
    verify_tailoring,
)
from tests.test_cv_parse import Fake as ParseFake

FX = Path(__file__).parent / "fixtures"
P = json.loads((FX / "entity_probes.json").read_text())
CV = (FX / "cv_fake.txt").read_text()
POST, CONTACT = P["posting"], P["contact"]
BAD = ("Citadel", "MIT", "Stanford", "Jane Street", "Optiver", "5 years", "Яндекс")
ASK = P["legit"]["ask_call"]


def draft(sentences, ask=ASK):
    return {
        "body": " ".join(sentences),
        "hook": {"text": "h", "quote": P["hook_quote"], "from": "posting"},
        "ask": ask,
        "claims": [],
    }


def run(sentences, ask=ASK):
    return verify_draft(draft(sentences, ask), CV, POST, CONTACT, ["Python"])


def test_fixture_marked_and_probes_have_no_old_tricks():
    assert "FAKE FIXTURE" in P["_note"] and "FAKE FIXTURE" in CV
    for s in P["fabricated_draft_sentences"][:2] + [P["tailoring"]["gap"]]:
        n = s.casefold()
        assert not numbers(n) and not inflation_stems(n) and not skill_terms(n)


def test_draft_invented_employer_and_school_dropped():
    v = run([P["greeting"], *P["fabricated_draft_sentences"][:2], P["legit"]["was_intern"], ASK])
    assert v.ok and v.removed == 2
    assert "Sample Labs" in v.body and not any(b in v.body for b in BAD)
    assert {"Citadel", "MIT"} <= set(v.unsupported)


def test_draft_past_claim_at_posting_company_dropped_but_intent_kept():
    fab = P["fabricated_draft_sentences"]
    ask = P["legit"]["ask_question"]
    v = run([P["greeting"], fab[2], fab[3], P["legit"]["intent"], ask], ask)
    assert v.ok and v.removed == 2
    assert "previous internship" not in v.body and "Stanford" not in v.body
    assert "Acme Fake" in v.body and v.body.count("Acme Fake") == 3
    assert "Acme" in v.unsupported


def test_cyrillic_invented_employer_dropped():
    v = run([P["greeting"], P["fabricated_draft_sentences"][4], P["legit"]["was_intern"], ASK])
    assert v.ok and v.removed == 1 and "Яндекс" not in v.body


def test_cyrillic_honest_inflection_kept():
    cv = CV + "\nЯндекс - стажёр\n"
    s = [P["greeting"], "Я стажировался в Яндексе.", P["legit"]["was_intern"], ASK]
    v = verify_draft(draft(s), cv, POST, CONTACT, ["Python"])
    assert v.ok and v.removed == 0


def test_ask_number_exemption_is_call_length_only():
    ask = "Having built trading systems for 5 years, could we have a short call?"
    v = run([P["greeting"], P["legit"]["was_intern"], P["legit"]["intent"], ask], ask)
    assert not v.ok and v.reason == "ask dropped" and "5" in v.unsupported
    assert "5 years" not in v.body
    assert run([P["greeting"], P["legit"]["was_intern"], P["legit"]["intent"], ASK]).ok
    short = "Could we do 15 min?"
    assert run([P["greeting"], P["legit"]["was_intern"], P["legit"]["intent"], short], short).ok


def test_legit_sentences_pass():
    ask = P["legit"]["ask_question"]
    v = run([P["greeting"], P["legit"]["was_intern"], P["legit"]["intent"], ask], ask)
    assert v.ok and v.removed == 0 and v.unsupported == []


def test_entities_extraction():
    assert entities("Hi Sam, I studied at MIT with OpenAI and Jane Street.") == [
        "Sam",
        "MIT",
        "OpenAI",
        "Jane",
        "Street",
    ]
    assert entities("Excited to apply. I use Python in March on Monday.") == []


def test_tailoring_invented_entities_removed():
    t = P["tailoring"]
    res = {
        "fit": {"level": "partial", "points": t["fit"] + [t["honest"]]},
        "emphasize": t["emphasize"] + [t["honest"]],
        "gaps": [{"requirement": "x", "posting_evidence": "", "how_to_address": t["gap"]}],
    }
    out, removed = verify_tailoring(res, CV, POST, ["Python"])
    blob = json.dumps(out, ensure_ascii=False)
    assert removed == 3 and not any(b in blob for b in BAD)
    assert len(out["fit"]["points"]) == 1 and len(out["emphasize"]) == 1
    assert out["gaps"] == []


def test_tailoring_gap_may_name_posting_company():
    res = {
        "gaps": [
            {
                "requirement": "tooling",
                "posting_evidence": "",
                "how_to_address": "Read about Acme Fake and its research tooling.",
            }
        ]
    }
    out, removed = verify_tailoring(res, CV, POST, ["Python"])
    assert removed == 0 and len(out["gaps"]) == 1


async def test_cv_parse_whole_word_grounding(tmp_path):
    conn = db.connect(tmp_path / "c.db")
    db.migrate(conn)
    b = LlmBudget(conn, FixedClock(datetime(2026, 11, 1, 5, tzinfo=UTC)), 10)
    cv = CV + "\nWe submitted reports on algorithms.\n"  # substrings "mit" and "go"
    r = await parse_cv_sections(ParseFake(json.dumps(P["parse_reply"])), cv, OutreachConfig(), b)
    assert r.error is None and r.dropped == 2
    assert [e.institution for e in r.sections.education] == ["Example University of Technology"]
    assert r.sections.skills == ["Python"]


# ---- FIX-26: degree and title acronyms are grounded (D-151) ----

SL = "Sample Labs - Research Intern, Summer 2025"


def test_fix26_title_and_degree_claims_removed_from_tailoring():
    from src.outreach.verify import claim_ok

    for t in (
        "I was the CTO at Sample Labs.",
        "You were CEO of Sample Labs",
        "Your MBA from Example University of Technology",
        "Your MSc from Example University of Technology",
        "Your master's degree from Example University of Technology",
    ):
        assert not claim_ok(t, SL, CV, ["Python"]), t
    res = {
        "gaps": [
            {"requirement": "a", "posting_evidence": "", "how_to_address": h}
            for h in ("Mention your MBA coursework", "Say you were CTO at Sample Labs")
        ]
    }
    out, removed = verify_tailoring(res, CV, POST, ["Python"])
    assert removed == 2 and out["gaps"] == []


def test_fix26_draft_degree_and_title_sentences_dropped():
    fab = [
        "I hold an MSc in Computer Science.",
        "I have an MBA.",
        "I was the CTO of a startup.",
        "I have a master's degree in physics.",
    ]
    for s in fab:
        v = run([P["greeting"], s, P["legit"]["was_intern"], P["legit"]["intent"], ASK])
        assert v.ok and v.removed == 1 and s not in v.body, s


def test_fix26_honest_bsc_still_passes():
    from src.outreach.verify import claim_ok

    t = "I am a BSc Computer Science student at Example University of Technology."
    assert claim_ok(t, "BSc Computer Science, 2022 - 2026", CV, ["Python"])
    v = run([P["greeting"], t, P["legit"]["was_intern"], P["legit"]["intent"], ASK])
    assert v.ok and v.removed == 0


# ---- FIX-27: remaining phrasings (D-152) ----

FIX27_DRAFT = [
    "Citadel hired me last summer.",
    "Google was where I interned.",
    "I am currently a Research Intern at Acme Fake.",
    "I'm a former intern at Acme Fake.",
    "Acme Fake was my previous employer.",
    "As a former Acme Fake intern I know the stack.",
    "My summer at Acme Fake taught me a lot.",
]


def test_fix27_each_phrasing_dropped_and_legit_kept():
    keep = [P["greeting"], P["legit"]["was_intern"], P["legit"]["intent"], ASK]
    for s in FIX27_DRAFT:
        v = run([keep[0], s, *keep[1:]])
        assert v.ok and v.removed == 1 and s not in v.body, s


def test_fix27_legit_sentences_survive():
    for s in (
        "Previously I was a research intern at Sample Labs.",
        "Recently I built tools in Python.",
        P["legit"]["intent"],
        "Could I learn more about internships at Acme Fake?",
        "Sample Labs was where I interned.",
        "Working with Python at Sample Labs taught me a lot.",
    ):
        v = run([P["greeting"], s, P["legit"]["was_intern"], P["legit"]["intent"], ASK])
        assert v.ok and v.removed == 0, s


def test_fix27_second_person_gap_at_posting_company_removed():
    res = {
        "gaps": [
            {"requirement": f"x{i}", "posting_evidence": "", "how_to_address": h}
            for i, h in enumerate(
                (
                    "Highlight your internship at Acme Fake",
                    "Highlight your Acme Fake internship",
                    "Highlight your internship at Sample Labs",
                    "Read about Acme Fake and its research tooling.",
                )
            )
        ]
    }
    out, removed = verify_tailoring(res, CV, POST, ["Python"])
    assert removed == 2
    assert [g["how_to_address"] for g in out["gaps"]] == [
        "Highlight your internship at Sample Labs",
        "Read about Acme Fake and its research tooling.",
    ]


def test_fix27_cyrillic_stem_is_bounded():
    cv = CV + "\nМаркетинг и аналитика. Яндекс - стажёр\n"
    bad = "Я работал в Яндекс Маркете."
    ok = "Я стажировался в Яндексе."
    for s, removed in ((bad, 1), (ok, 0)):
        v = verify_draft(
            draft([P["greeting"], s, P["legit"]["was_intern"], P["legit"]["intent"], ASK]),
            cv,
            POST,
            CONTACT,
            ["Python"],
        )
        assert v.ok and v.removed == removed, s
