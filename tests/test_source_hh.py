import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx

from src import db as dbm
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.http import build_client
from src.models import Kind
from src.sources.base import REGISTRY, SourceContext, run_source
from src.sources.hh import HHSource, parse_vacancies

FIXTURE = (Path(__file__).parent / "fixtures" / "hh_vacancies.json").read_text()
ENV = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}


def _cfg(queries=("a",)):
    return AppConfig.from_dict(
        {"sources": {"hh": {"enabled": True, "area": 40, "queries": list(queries)}}}
    )


def _ctx(handler_resp, cfg, token=None):
    conn = dbm.connect(":memory:")
    dbm.migrate(conn)
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    requests = []

    def handler(req):
        requests.append(req)
        if req.url.path == "/robots.txt":
            return httpx.Response(404, json={"errors": []})
        return handler_resp

    async def nosleep(_):
        pass

    http = build_client(conn, clock, transport=httpx.MockTransport(handler), sleep=nosleep)
    env = dict(ENV, **({"HH_ACCESS_TOKEN": token} if token else {}))
    ctx = SourceContext(
        http=http, settings=Settings.from_env(env), cfg=cfg, clock=clock, log=logging.getLogger("t")
    )
    return ctx, requests


async def test_fixture_parses_and_request_shape():
    ctx, requests = _ctx(httpx.Response(200, text=FIXTURE), _cfg(), token="tok")
    res = await run_source(HHSource(), ctx)
    assert res.ok and len(res.items) == 5
    first = res.items[0]
    assert first.title == "Стажёр Data Scientist"
    assert first.company == "Kaspi.kz"
    assert first.location == "Алматы"
    assert first.kind == Kind.INTERNSHIP
    assert first.url == "https://hh.kz/vacancy/100000001"
    assert first.posted_at is not None and first.posted_at.tzinfo is not None
    assert "highlighttext" not in first.description and "Python" in first.description
    assert [p.remote for p in res.items][:2] == [False, True]
    assert res.items[1].kind == Kind.JOB
    assert res.items[3].kind == Kind.INTERNSHIP  # "Intern" in title
    api = [r for r in requests if r.url.path == "/vacancies"]
    assert len(api) == 1
    assert api[0].headers["HH-User-Agent"].startswith("oppo-tg/")
    assert api[0].headers["Authorization"] == "Bearer tok"
    assert api[0].url.params["area"] == "40" and api[0].url.params["per_page"] == "50"


async def test_no_token_means_no_auth_header():
    ctx, requests = _ctx(httpx.Response(200, text=FIXTURE), _cfg())
    await run_source(HHSource(), ctx)
    api = [r for r in requests if r.url.path == "/vacancies"]
    assert "Authorization" not in api[0].headers


async def test_duplicate_across_queries_once():
    ctx, requests = _ctx(httpx.Response(200, text=FIXTURE), _cfg(["a", "b"]))
    res = await run_source(HHSource(), ctx)
    assert len([r for r in requests if r.url.path == "/vacancies"]) == 2
    assert len(res.items) == 5


def test_word_boundary_intern():
    data = {
        "items": [
            {
                "id": "1",
                "name": "Internal Tools Developer",
                "alternate_url": "https://hh.kz/vacancy/1",
                "experience": {"id": "between1And3"},
            }
        ]
    }
    (p,) = parse_vacancies(data)
    assert p.kind == Kind.JOB and p.company == ""


async def test_empty_malformed_and_errors():
    for resp in (
        httpx.Response(200, text=json.dumps({"items": []})),
        httpx.Response(200, text="{not json"),
        httpx.Response(200, text="[1,2]"),
        httpx.Response(403, json={"errors": [{"type": "forbidden"}]}),
        httpx.Response(500, text="boom"),
    ):
        ctx, _ = _ctx(resp, _cfg())
        res = await run_source(HHSource(), ctx)
        assert res.items == []
        assert res.ok == (resp.status_code == 200 and resp.text != "{not json")
        if not res.ok:
            assert res.error
    assert parse_vacancies({"items": [{"id": "1"}, "junk", None]}) == []


def test_registered_and_disabled_without_token():
    assert "hh" in REGISTRY
    src = HHSource()
    no_tok = Settings.from_env(ENV)
    tok = Settings.from_env(dict(ENV, HH_ACCESS_TOKEN="x"))
    assert not src.enabled(no_tok, _cfg())
    assert src.disabled_reason(no_tok, _cfg()) == "needs HH_ACCESS_TOKEN"
    assert src.enabled(tok, _cfg())
    assert not src.enabled(tok, _cfg([]))
