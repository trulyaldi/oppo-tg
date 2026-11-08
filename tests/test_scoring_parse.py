import json
from datetime import date
from pathlib import Path

import pytest

from src.scoring.parse import ScoreParseError, parse_score_response

FIX = json.loads((Path(__file__).parent / "fixtures" / "score_responses.json").read_text())


@pytest.mark.parametrize("case", FIX["ok"], ids=lambda c: c["name"])
def test_ok(case):
    r = parse_score_response(case["text"])
    assert r.score == case["score"]
    assert r.deadline == (date.fromisoformat(case["deadline"]) if case["deadline"] else None)
    assert "\n" not in r.reason


def test_multiline_collapsed():
    case = next(c for c in FIX["ok"] if c["name"] == "multiline_reason")
    assert parse_score_response(case["text"]).reason == "line one line two"


@pytest.mark.parametrize("case", FIX["bad"], ids=lambda c: c["name"])
def test_bad(case):
    with pytest.raises(ScoreParseError):
        parse_score_response(case["text"])


def test_old_format_defaults():
    r = parse_score_response(FIX["v1_shaped"]["text"])
    assert r.employer_region == "unknown" and r.region_confidence == 0.0
    assert r.employer_is_startup is None and r.visa_barrier is None


def test_new_format_fields():
    case = FIX["new_format"]
    r = parse_score_response(case["text"])
    assert r.employer_region == case["employer_region"]
    assert r.region_confidence == case["region_confidence"]
    assert r.employer_is_startup is True and r.visa_barrier is False


@pytest.mark.parametrize("case", FIX["bad_new"], ids=lambda c: c["name"])
def test_bad_new_fields(case):
    with pytest.raises(ScoreParseError):
        parse_score_response(case["text"])


def test_prompt_contract_and_version():
    from src.scoring.prompt import build_system_blocks, prompt_version

    text = build_system_blocks()[0]["text"]
    assert "not part of the score" in text
    for f in ("employer_region", "region_confidence", "employer_is_startup", "visa_barrier"):
        assert f in text
    assert prompt_version() not in {"59e8516c95e4", "d08e378779a9"}  # before W3 and KF-14


def test_v1_shaped_reply_still_parses():
    r = parse_score_response(FIX["v1_shaped"]["text"])
    assert r.score == 8.5 and r.tags == ["quant"]
    assert r.type.value == "other" and "type" not in r.model_fields_set


def test_v2_type_and_tags_default():
    r = parse_score_response(FIX["new_format"]["text"])
    assert r.type.value == FIX["new_format"]["type"] and r.tags == []


def test_invalid_type_is_not_a_parse_failure():
    r = parse_score_response(FIX["invalid_type"]["text"])
    assert r.type.value == "other" and "type" not in r.model_fields_set


def test_long_reason_and_flags_clipped():
    r = parse_score_response(FIX["long_reason"]["text"])
    assert len(r.reason) <= 110 and r.reason.endswith("…")
    words = {"Matches", "numerical", "optimization,", "PyTorch", "and", "probability", "work"}
    assert set(r.reason[:-1].split()) <= words
    assert len(r.red_flags) == 3 and all(len(f) <= 40 for f in r.red_flags)


def test_generic_reply_parses():
    from src.scoring.reason import is_generic

    assert is_generic(parse_score_response(FIX["generic_reason"]["text"]).reason)


def test_contract_text_in_rubric():
    from src.scoring.prompt import build_system_blocks

    text = build_system_blocks()[0]["text"]
    assert "110 characters" in text and "Do not output tags" in text
    assert '"type"' in text and '"tags"' not in text
