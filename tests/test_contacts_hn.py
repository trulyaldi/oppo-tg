import json
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx

from src import db as dbm
from src.clock import FixedClock
from src.contacts.classify import classify_role
from src.contacts.parsers.hn import fetch_hn_contacts, parse_hn_comment
from src.http import PoliteClient

FIX = json.loads(
    (Path(__file__).parent / "fixtures/contacts/hn_comment_contact.json").read_text("utf-8")
)


def make(handler):
    conn = dbm.connect(":memory:")
    dbm.migrate(conn)

    async def sleep(d):
        pass

    return PoliteClient(
        db=conn,
        clock=FixedClock(datetime(2026, 1, 1, tzinfo=UTC)),
        transport=httpx.MockTransport(handler),
        min_interval_s=0,
        sleep=sleep,
    )


def test_fixture_two_contacts():
    got = parse_hn_comment(FIX)
    assert [(c.name, c.role) for c in got] == [
        ("HN user fake_founder_fixture", "co-founder"),
        ("Sam Placeholder", "Head of Data"),
    ]
    assert got[0].note == "HN Who is hiring poster"
    assert classify_role(got[0].role)[0] == "founder"
    assert classify_role(got[1].role)[0] == "hiring_manager"
    for c in got:
        for f in (c.name, c.role, c.note):
            assert "@" not in f and not re.search(r"\d{4}", f)


def test_roles():
    def role(t):
        return parse_hn_comment({"author": "u1", "text": t})[0].role

    assert role("Foo | Dev. I'm the CTO here") == "CTO"
    assert role("Founder here, hiring") == "founder"
    assert role("I'm hiring for my team") == "hiring manager"
    assert role("Привет, я основатель") == "основатель"
    assert role("Just a post") == "poster"


def test_bad_inputs():
    assert parse_hn_comment({"author": "u", "text": None}) == []
    assert parse_hn_comment({"author": "u"}) == []
    assert parse_hn_comment("not a dict") == []
    assert parse_hn_comment(None) == []
    assert parse_hn_comment({"author": "bad name!<x>", "text": "hello"}) == []


async def test_fetch_ok_host_only():
    hosts = []

    def handler(req):
        hosts.append(req.url.host)
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, json=FIX)

    got = await fetch_hn_contacts(make(handler), "99000001")
    assert len(got) == 2
    assert set(hosts) == {"hn.algolia.com"}


async def test_fetch_500_and_malformed():
    def h500(req):
        return httpx.Response(404 if req.url.path == "/robots.txt" else 500)

    assert await fetch_hn_contacts(make(h500), "1") == []

    def hbad(req):
        return httpx.Response(404 if req.url.path == "/robots.txt" else 200, text="{not json")

    assert await fetch_hn_contacts(make(hbad), "1") == []
    assert await fetch_hn_contacts(make(hbad), "../x") == []
