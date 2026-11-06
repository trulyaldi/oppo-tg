import json
from pathlib import Path

from src.outreach.verify import split_sentences, v1, verify_draft, verify_tailoring

FX = Path(__file__).parent / "fixtures"
CV = (FX / "cv_bait_fake.txt").read_text()
POSTING = json.loads((FX / "posting_fabrication_bait.json").read_text())
POST_TEXT = POSTING["title"] + "\n" + POSTING["description"]
SKILLS = ["Python", "SQL"]


def load(name):
    return json.loads((FX / name).read_text())


def all_strings(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from all_strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from all_strings(x)


def test_fixtures_are_fake():
    assert "FAKE FIXTURE" in CV


def test_fabricated_reply_is_cleaned():
    out, removed = verify_tailoring(load("tailor_reply_fabricated.json"), CV, POST_TEXT, SKILLS)
    assert removed == 6
    assert out["fit"]["points"] == []
    assert [e["text"] for e in out["emphasize"]] == [
        "Data internship with forecasting experiments",
        "Python and SQL data cleaning",
    ]
    assert [k["keyword"] for k in out["keywords"]] == ["Python"]
    assert out["reorder"] == []
    assert {g["requirement"] for g in out["gaps"]} == {"C++", "kdb+"}
    forbidden = ["Grandmaster", "12 engineers", "3 years of Python", "5 years"]
    checked = []
    for sec in ("emphasize", "cut", "reorder", "keywords"):
        checked += list(all_strings(out[sec]))
    checked += list(all_strings(out["fit"]))
    checked += [g["how_to_address"] for g in out["gaps"]]
    for s in checked:
        assert not any(f in s for f in forbidden), s


def test_honest_reply_unchanged():
    raw = load("tailor_reply_honest.json")
    out, removed = verify_tailoring(raw, CV, POST_TEXT, SKILLS)
    assert removed == 0
    assert out == raw


def test_malformed_input_does_not_raise():
    out, removed = verify_tailoring(
        {"fit": "x", "emphasize": [1, {"text": 5}], "gaps": None}, CV, POST_TEXT
    )
    assert out["emphasize"] == [] and removed == 1


def test_split_sentences():
    s = split_sentences("Hi Dr. Smith. I study physics. Could we talk? Thanks!")
    assert s == ["Hi Dr. Smith.", "I study physics.", "Could we talk?", "Thanks!"]


def test_v1_normalization_and_min_length():
    assert v1(
        "  WORKED with a “team” of   researchers ", 'worked with a "team" of researchers on x'
    )
    assert not v1("short", "short text here")
    assert not v1("", CV)
    assert not v1("not in the cv at all", CV)


def test_russian_inflation_removed():
    cv = "Участвовал в проекте по анализу данных вместе с командой студентов"
    res = {
        "emphasize": [
            {"text": "руководил командой", "cv_evidence": "Участвовал в проекте по анализу данных"},
            {"text": "анализ данных", "cv_evidence": "проекте по анализу данных"},
        ]
    }
    out, removed = verify_tailoring(res, cv, "posting")
    assert removed == 1 and [e["text"] for e in out["emphasize"]] == ["анализ данных"]


def test_inflation_word_boundaries():
    cv = "Maintained the ledger reports and wondered about features in the header files"
    res = {
        "emphasize": [
            {"text": "ledger reports", "cv_evidence": "Maintained the ledger reports and wondered"}
        ]
    }
    out, removed = verify_tailoring(res, cv, "p")
    assert removed == 0 and out["emphasize"]


def test_keyword_missing_from_posting_just_removed():
    res = {"keywords": [{"keyword": "Rust", "cv_evidence": "x" * 10, "posting_evidence": "y" * 10}]}
    out, removed = verify_tailoring(res, CV, POST_TEXT, SKILLS)
    assert removed == 1 and out["keywords"] == [] and out["gaps"] == []


def test_cv_skills_extend_lexicon():
    cv = "Built things with Zorblax for a client project last year"
    res = {
        "emphasize": [
            {"text": "Zorblax client work", "cv_evidence": "Built things with Zorblax for a client"}
        ]
    }
    assert verify_tailoring(res, cv, "p", ["Zorblax"])[1] == 0
    bad = {
        "emphasize": [
            {"text": "Zorblax client work", "cv_evidence": "Built things with a client project"}
        ]
    }
    assert verify_tailoring(bad, cv, "p", ["Zorblax"])[1] == 1


# ---- drafts ----

CONTACT = "Jo Recruiter, Technical Recruiter, runs the data hiring team"


def draft(
    body,
    ask="Could we talk for 15 minutes?",
    quote="strong C++ and kdb+",
    frm="posting",
    claims=None,
):
    return {
        "body": body,
        "hook": {"text": "h", "quote": quote, "from": frm},
        "ask": ask,
        "claims": claims or [],
    }


GOOD = (
    "I saw your posting for the quant developer role. "
    "I cleaned and joined survey datasets with Python and SQL during an internship. "
    "I would like to learn more about your tooling. "
    "Could we talk for 15 minutes?"
)


def test_draft_ok():
    v = verify_draft(draft(GOOD), CV, POST_TEXT, CONTACT, SKILLS)
    assert v.ok and v.removed == 0 and v.body == GOOD


def test_draft_fabricated_sentences_dropped_or_failed():
    body = GOOD.replace(
        "I would like to learn more about your tooling.",
        "I led a team of 12 engineers writing C++ for kdb+.",
    )
    v = verify_draft(draft(body), CV, POST_TEXT, CONTACT, SKILLS)
    assert v.ok and v.removed == 1
    assert "12 engineers" not in v.body and "led" not in v.body.split()
    assert "12" in v.unsupported and "led" in v.unsupported
    # without the hook quote covering C++/kdb+, the skill terms are flagged too
    v2 = verify_draft(
        draft(body, quote="Python and SQL for research"), CV, POST_TEXT, CONTACT, SKILLS
    )
    assert "c++" in v2.unsupported and "kdb+" in v2.unsupported


def test_draft_extra_fabricated_sentence_dropped_but_ok():
    body = GOOD + " I am a Kaggle grandmaster."
    v = verify_draft(draft(body), CV, POST_TEXT, CONTACT, SKILLS)
    assert v.ok and "grandmaster" not in v.body.lower() and v.removed == 1


def test_draft_hook_must_be_quoted():
    assert (
        verify_draft(draft(GOOD, quote="a quote that is not there"), CV, POST_TEXT, CONTACT).reason
        == "hook"
    )
    assert verify_draft(
        draft(GOOD, quote="technical recruiter", frm="contact"), CV, POST_TEXT, CONTACT
    ).ok
    assert verify_draft(draft(GOOD, frm="nowhere"), CV, POST_TEXT, CONTACT).reason == "hook"


def test_draft_ask_and_length():
    assert verify_draft(draft(GOOD, ask="Call me maybe?"), CV, POST_TEXT, CONTACT).reason == "ask"
    assert verify_draft(draft("One. Two."), CV, POST_TEXT, CONTACT).reason == "length"


def test_draft_bad_claim_counted():
    c = [{"text": "Led the research team", "cv_evidence": "Worked with a team of researchers"}]
    v = verify_draft(draft(GOOD, claims=c), CV, POST_TEXT, CONTACT, SKILLS)
    assert v.ok and v.removed == 1


def test_draft_contact_info_sentences_dropped():
    for evil in [
        "Contact me at evil@phish.example or https://phish.example/cv.",
        "Reach me on +7 701 234 56 78 any time.",
        "My profile is at phish.example/cv today.",
        "Message @phisher on Telegram.",
        "See www.phish.example for details.",
    ]:
        v = verify_draft(draft(GOOD + " " + evil), CV, POST_TEXT, CONTACT, SKILLS)
        assert v.ok and v.removed == 1 and v.body == GOOD, evil
        assert "contact-info" in v.unsupported
    # below 3 sentences after dropping -> not ok (regenerate path)
    short = (
        "I saw your posting for the quant developer role. Could we talk for 15 minutes? "
        "Mail evil@phish.example."
    )
    v = verify_draft(draft(short), CV, POST_TEXT, CONTACT, SKILLS)
    assert not v.ok and v.reason == "too few" and v.removed >= 1


def test_draft_year_ranges_and_plain_text_not_contact_info():
    from src.outreach.verify import has_contact_info

    assert not has_contact_info("I worked on C++ and kdb+ from 2019-2023 in Node.js.")
    assert not has_contact_info("I used 12345 rows and e.g. Python.")
    assert has_contact_info("Call 8 (701) 234-56-78")


def test_draft_malformed():
    assert not verify_draft("x", CV, POST_TEXT).ok
    assert not verify_draft({"body": 1}, CV, POST_TEXT).ok


# ---- FIX-30: tailoring output carries no URL, email, handle or phone (D-155) ----


def test_fix30_contact_info_removed_from_tailoring():
    import json
    from pathlib import Path

    from src.outreach.verify import claim_ok, verify_tailoring

    fx = Path(__file__).parent / "fixtures"
    cv = (fx / "cv_fake.txt").read_text()
    ev = "Sample Labs - Research Intern, Summer 2025"
    for t in (
        "Highlight your Sample Labs work, details at https://phish.example/cv",
        "Highlight your Sample Labs work, write evil@phish.example",
        "Highlight your Sample Labs work, ping @phisher",
        "Highlight your Sample Labs work, call +7 701 234 56 78",
    ):
        assert not claim_ok(t, ev, cv, [])
    res = {
        "fit": {
            "level": "partial",
            "points": [{"text": "see www.phish.example", "cv_evidence": ev}],
        },
        "emphasize": [
            {"text": "Highlight your Sample Labs work, evil@phish.example", "cv_evidence": ev}
        ],
        "gaps": [
            {
                "requirement": "x",
                "posting_evidence": "",
                "how_to_address": "Email your CV to hr@phish.example",
            }
        ],
    }
    out, removed = verify_tailoring(res, cv, "Acme Fake is hiring.", [])
    assert removed == 3 and out["gaps"] == [] and out["emphasize"] == []
    assert "phish" not in json.dumps(out)


# ---- FIX-38: "Lead with ..." is advice, not an inflation claim (D-164) ----

CV_FAKE = (FX / "cv_fake.txt").read_text()
SL_EV = "Sample Labs - Research Intern, Summer 2025"
SPEEDUP_EV = "Reduced replay time by 30% with a C++ parser"


def _claim(text, ev):
    from src.outreach.verify import claim_ok

    return claim_ok(text, ev, CV_FAKE, ["Python", "C++"])


def test_lead_with_claims_kept():
    assert _claim("Lead with the Sample Labs research internship", SL_EV)
    assert _claim("Lead with the 30% replay speedup", SPEEDUP_EV)
    assert _claim("Leading with the Sample Labs research internship", SL_EV)


def test_lead_with_gap_and_reorder_kept():
    reply = {
        "gaps": [
            {
                "requirement": "Python experience",
                "posting_evidence": "",
                "how_to_address": "The posting wants 2+ years of Python; lead with the "
                "benchmark harness and the matching engine.",
            }
        ],
        "reorder": [{"text": "Lead with Experience, then Projects", "cv_evidence": SL_EV}],
    }
    v, removed = verify_tailoring(
        reply, CV_FAKE, "We need 2+ years of Python experience.", ["Python", "C++"]
    )
    assert removed == 0 and v["gaps"] and v["reorder"]


def test_real_lead_claims_still_inflation():
    from src.outreach.verify import inflation_stems

    for s in (
        "i led a team of engineers at sample labs.",
        "i was the lead engineer at sample labs.",
        "i lead the research team at sample labs.",
        "team lead with five reports",
    ):
        assert inflation_stems(s), s
    assert not inflation_stems("lead with the benchmark harness")
