import pytest
from pydantic import ValidationError

from src.models import Kind, Posting, ScoreResult


def mk(**kw):
    base = {"source": "greenhouse", "external_id": "1", "title": "t", "company": "c"}
    base.update(kw)
    base.setdefault("url", "https://example.com/1")
    return Posting(**base)


def test_id_stable_hex16():
    p = mk()
    assert len(p.id) == 16
    int(p.id, 16)
    assert p.id == mk().id


def test_id_differs():
    assert mk().id != mk(external_id="2").id
    assert mk().id != mk(source="lever").id


@pytest.mark.parametrize("url", ["ftp://x.com/a", "javascript:alert(1)", "", "https://"])
def test_bad_url(url):
    with pytest.raises(ValueError):
        mk(url=url)


def test_description_truncated_and_defaults():
    p = mk(description="x" * 9000)
    assert len(p.description) == 8000
    assert p.kind is Kind.OTHER


@pytest.mark.parametrize("s", [0, 10, 5.5])
def test_score_ok(s):
    assert ScoreResult(score=s, reason="r").score == s


@pytest.mark.parametrize("s", [11, -1])
def test_score_bad(s):
    with pytest.raises(ValidationError):
        ScoreResult(score=s, reason="r")


def test_opportunity_types_and_kind_map():
    from src.models import PROGRAM_TYPES, Kind, OpportunityType, kind_to_type

    assert len(OpportunityType) == 12
    assert {t.value for t in PROGRAM_TYPES} == {
        "grant", "scholarship", "fellowship", "hackathon", "competition", "accelerator",
        "summer_school",
    }  # fmt: skip
    assert kind_to_type(Kind.STARTUP) is OpportunityType.STARTUP_ROLE
    assert kind_to_type("research") is OpportunityType.RESEARCH
    assert kind_to_type("weird") is OpportunityType.OTHER
    for k in Kind:
        assert kind_to_type(k) in OpportunityType
