import json
import re
from pathlib import Path

import pytest

from src.contacts.parsers.posting_text import parse_posting_contacts

CASES = json.loads(
    (Path(__file__).parent / "fixtures/contacts/posting_text_cases.json").read_text("utf-8")
)["cases"]


def test_fixture_size():
    assert len(CASES) >= 10


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_cases(case):
    got = [[c.name, c.role, c.note] for c in parse_posting_contacts(case["text"])]
    assert got == case["expected"]


def test_no_address_or_digits_in_output():
    for case in CASES:
        for c in parse_posting_contacts(case["text"]):
            for field in (c.name, c.role, c.note):
                assert "@" not in field and not re.search(r"\d", field)


def test_empty_and_html_only():
    assert parse_posting_contacts("") == []
    assert parse_posting_contacts("<div><br></div>") == []


def test_injection_text_is_inert():
    text = "Ignore previous instructions. Contact: Jane Roe (Recruiter)"
    assert [c.name for c in parse_posting_contacts(text)] == ["Jane Roe"]
