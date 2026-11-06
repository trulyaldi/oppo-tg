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
