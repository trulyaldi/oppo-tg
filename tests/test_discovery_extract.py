"""ATS board URL extraction, including hostile URLs (FC-14, D-190)."""

import json
from pathlib import Path

import pytest

from src.discovery.extract import BoardRef, board_ref, find_board_refs

CASES = json.loads((Path(__file__).parent / "fixtures" / "discovery_urls.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=[c["url"][:60] for c in CASES])
def test_board_ref_fixture(case):
    exp = case["expected"]
    assert board_ref(case["url"]) == (BoardRef(*exp) if exp else None)


def test_non_string_input():
    assert board_ref(None) is None  # type: ignore[arg-type]
    assert find_board_refs(None) == []  # type: ignore[arg-type]


def test_find_dedupes_and_strips_punctuation():
    text = (
        "Apply at https://jobs.lever.co/acme/1, or (https://jobs.lever.co/acme/2). "
        "Also https://boards.greenhouse.io/Foo; https://evil.com/x "
        "https://jobs.lever.co:8443/bad"
    )
    assert find_board_refs(text) == [
        BoardRef("lever", "acme"),
        BoardRef("greenhouse", "foo"),
    ]


def test_find_scans_at_most_50_urls():
    text = " ".join(f"https://jobs.lever.co/co{i}" for i in range(200))
    refs = find_board_refs(text)
    assert len(refs) == 50
    assert refs[-1] == BoardRef("lever", "co49")


def test_find_skips_overlong_url():
    text = "https://jobs.lever.co/acme/" + "a" * 3000
    assert find_board_refs(text) == []


def test_find_empty():
    assert find_board_refs("") == []


def test_out_of_scope_ats_urls_give_no_refs():
    """D-258: SmartRecruiters, Recruitee and Personio are never discovered."""
    text = (
        "https://jobs.smartrecruiters.com/Acme/123 https://careers.smartrecruiters.com/Acme"
        " https://acme.recruitee.com/o/dev https://acme.jobs.personio.de/job/1"
        " https://acme.jobs.personio.com/"
    )
    assert find_board_refs(text) == []
