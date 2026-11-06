import inspect
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import db
from src.clock import FixedClock
from src.config import AppConfig, OutreachConfig
from src.contacts.pasted import extract_with_llm, parse_pasted
from src.llm.budget import LlmBudget

FX = Path(__file__).parent / "fixtures"
PROFILE = (FX / "contacts/pasted_profile.txt").read_text()
CFG = AppConfig.load(FX / "config_repo_snapshot.toml")
OC = OutreachConfig.from_cfg(CFG)
NOW = datetime(2026, 11, 6, 5, tzinfo=UTC)


class Fake:
    def __init__(self, reply):
        self.reply = reply if isinstance(reply, str) else json.dumps(reply)
        self.calls = []
        self.messages = self

    async def create(self, **kw):
        from anthropic.resources.messages import AsyncMessages

        inspect.signature(AsyncMessages.create).bind(None, **kw)
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.reply)])


@pytest.fixture
def budget():
    c = db.connect(":memory:")
    db.migrate(c)
    return LlmBudget(c, FixedClock(NOW), 40)


def test_short_form_with_linkedin_url():
    url, raw, llm = parse_pasted(
        "https://www.linkedin.com/in/fake-person\nAlex Placeholder - Senior Recruiter at Acme Fake"
    )
    assert url == "https://www.linkedin.com/in/fake-person"
    assert (raw.name, raw.role) == ("Alex Placeholder", "Senior Recruiter at Acme Fake")
    assert llm is False


def test_comma_form_and_two_lines():
    assert parse_pasted("Sam Example, Head of Research at Fake Co")[1].role.startswith("Head of")
    raw = parse_pasted("Sam Example\nEngineering Manager at Fake Co")[1]
    assert raw.name == "Sam Example" and "Manager" in raw.role


def test_url_only_and_no_scheme():
    assert parse_pasted("https://www.linkedin.com/in/fake-person/") == (
        "https://www.linkedin.com/in/fake-person/",
        None,
        False,
    )
    assert parse_pasted("linkedin.com/in/fake-person.")[0] == "https://linkedin.com/in/fake-person"
    assert parse_pasted("https://example.test/p/x") == (None, None, False)


def test_garbage_short_is_not_llm():
    assert parse_pasted("hello there") == (None, None, False)


def test_long_text_needs_llm():
    assert parse_pasted(PROFILE)[2] is True
    assert parse_pasted("x" * 301)[2] is True


GOOD = {
    "name": "Jordan Samplesmith",
    "role": "Principal Quant Researcher at Acme Fake Capital",
    "company": "Acme Fake Capital",
    "note": "Interested in market microstructure and open-source ML tooling.",
}


async def test_llm_extraction_wraps_and_grounds(budget):
    client = Fake(GOOD)
    raw, company, err = await extract_with_llm(client, PROFILE, OC, budget)
    assert err is None and raw.name == "Jordan Samplesmith" and company == "Acme Fake Capital"
    assert len(client.calls) == 1
    user = client.calls[0]["messages"][0]["content"]
    assert user.startswith("<profile>\n") and user.endswith("\n</profile>")
    assert "Ignore previous instructions" in user  # present only as wrapped data
    assert "never instructions" in client.calls[0]["system"][0]["text"]


async def test_llm_name_not_in_text_rejected(budget):
    raw, _, err = await extract_with_llm(
        Fake({**GOOD, "name": "Mallory Nowhere"}), PROFILE, OC, budget
    )
    assert raw is None and err == "rejected"


async def test_llm_role_not_in_text_rejected(budget):
    raw, _, err = await extract_with_llm(
        Fake({**GOOD, "role": "Founder and CEO"}), PROFILE, OC, budget
    )
    assert raw is None and err == "rejected"


async def test_llm_ungrounded_company_and_note_dropped(budget):
    raw, company, err = await extract_with_llm(
        Fake({**GOOD, "company": "Invented Inc", "note": "Won a Nobel prize"}), PROFILE, OC, budget
    )
    assert err is None and company == "" and raw.note == ""


async def test_llm_errors(budget):
    assert (await extract_with_llm(Fake("not json"), PROFILE, OC, budget))[2] == "invalid"
    c = db.connect(":memory:")
    db.migrate(c)
    zero = LlmBudget(c, FixedClock(NOW), 0)
    client = Fake(GOOD)
    assert (await extract_with_llm(client, PROFILE, OC, zero))[2] == "budget"
    assert not client.calls


async def test_no_pasted_text_in_logs(budget, caplog):
    caplog.set_level(logging.DEBUG)
    await extract_with_llm(Fake("not json"), PROFILE, OC, budget)
    assert "Samplesmith" not in caplog.text and "market microstructure" not in caplog.text
