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
ENV = {
    "TELEGRAM_BOT_TOKEN": "t",
    "TELEGRAM_CHAT_ID": "1",
    "ANTHROPIC_API_KEY": "k",
    "HTTP_CONTACT": "me@example.com",
}


CUES_TWO = str(Path(__file__).parent / "fixtures" / "cues_hh_two.toml")


def _cfg(cues_file=CUES_TWO):
    """Default: two one-term groups, q1 and q2. None uses the repo cue file."""
    filters = {"cues_file": cues_file} if cues_file else {}
    return AppConfig.from_dict(
        {"sources": {"hh": {"enabled": True, "area": 40}}, "filters": filters}
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
    assert first.published_at == first.posted_at
    assert "highlighttext" not in first.description and "Python" in first.description
    assert [p.remote for p in res.items][:2] == [False, True]
    assert res.items[1].kind == Kind.JOB
    assert res.items[3].kind == Kind.INTERNSHIP  # "Intern" in title
    api = [r for r in requests if r.url.path == "/vacancies"]
    assert len(api) == 2
    assert api[0].headers["HH-User-Agent"].startswith("oppo-tg/")
    assert api[0].headers["Authorization"] == "Bearer tok"
    p = api[0].url.params
    assert p["area"] == "40" and p["per_page"] == "100" and p["page"] == "0"
    assert p["order_by"] == "publication_time"


async def test_no_token_means_no_auth_header():
    ctx, requests = _ctx(httpx.Response(200, text=FIXTURE), _cfg())
    await run_source(HHSource(), ctx)
    api = [r for r in requests if r.url.path == "/vacancies"]
    assert "Authorization" not in api[0].headers


async def test_duplicate_across_queries_once():
    ctx, requests = _ctx(httpx.Response(200, text=FIXTURE), _cfg())
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
    assert src.disabled_reason(tok, _cfg()) is None


async def test_auth_and_user_agent_errors_exact_text_and_header():
    from src.sources.hh import UA_ERROR, token_error

    for status, text in ((400, UA_ERROR), (401, token_error(401)), (403, token_error(403))):
        ctx, requests = _ctx(httpx.Response(status, json={}), _cfg(), token="tok")
        res = await run_source(HHSource(), ctx)
        assert not res.ok and res.error == f"SourceUnavailable: {text}"
        assert len([r for r in requests if r.url.path == "/vacancies"]) == 1
    assert "set HTTP_CONTACT to your email" in UA_ERROR
    assert "user tokens expire after 14 days" in token_error(401)


async def test_user_agent_uses_contact():
    ctx, requests = _ctx(httpx.Response(200, text=FIXTURE), _cfg(), token="tok")
    ctx.settings = Settings.from_env(
        dict(ENV, HH_ACCESS_TOKEN="tok", HTTP_CONTACT="me@example.com")
    )
    await run_source(HHSource(), ctx)
    api = [r for r in requests if r.url.path == "/vacancies"]
    assert api[0].headers["HH-User-Agent"] == "oppo-tg/0.1 (me@example.com)"


async def test_one_notice_per_day():
    from datetime import timedelta

    from src.sources.hh import notify_auth_error_once

    class Bot:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat, text, **kw):
            self.sent.append(text)

    ctx, _ = _ctx(httpx.Response(401, json={}), _cfg(), token="tok")
    conn = ctx.http.db
    bot = Bot()
    assert not await notify_auth_error_once(bot, 1, conn, ctx.clock)  # nothing pending
    await run_source(HHSource(), ctx)
    assert await notify_auth_error_once(bot, 1, conn, ctx.clock)
    await run_source(HHSource(), ctx)
    assert not await notify_auth_error_once(bot, 1, conn, ctx.clock)  # same day
    later = FixedClock(ctx.clock.now() + timedelta(days=1))
    assert await notify_auth_error_once(bot, 1, conn, later)
    assert len(bot.sent) == 2 and "HH_ACCESS_TOKEN" in bot.sent[0]


def _api(requests):
    return [r for r in requests if r.url.path == "/vacancies"]


async def test_repo_cues_bound_cyrillic_kazakh_and_no_stems():
    ctx, requests = _ctx(httpx.Response(200, text=FIXTURE), _cfg(None), token="tok")
    await run_source(HHSource(), ctx)
    api = _api(requests)
    assert len(api) == 4
    assert all(r.url.params["area"] == "40" for r in api)
    assert all(r.headers["Authorization"] == "Bearer tok" for r in api)
    assert all(r.headers["HH-User-Agent"] == "oppo-tg/0.1 (me@example.com)" for r in api)
    first = api[0].url.params["text"]  # types.internship
    assert " OR " in first
    assert any("а" <= ch <= "я" for ch in first)
    assert "*" not in "".join(r.url.params["text"] for r in api)
    assert "тағылымдама" in first  # kk term
    assert "internship" in first


async def test_groups_rotate_across_runs():
    ctx, requests = _ctx(httpx.Response(200, text=FIXTURE), _cfg(None), token="tok")
    await run_source(HHSource(), ctx)
    await run_source(HHSource(), ctx)
    texts = [r.url.params["text"] for r in _api(requests)]
    # 6 groups, 4 per run: run 2 starts at group 4 and wraps to groups 0 and 1, which are still
    # in the response cache, so only groups 4 and 5 hit the transport.
    assert len(texts) == 6 and len(set(texts)) == 6
    assert "junior" in texts[5] and "analyst" in texts[4]


def test_build_query_quotes_cuts_and_interleaves():
    from src.cues import parse_cues_text
    from src.sources.hh import build_query

    cues = parse_cues_text(
        '[types.job]\nen = ["data science", "quant", "x1", "x2"]\n'
        'ru = ["аналитик*", "аналитик данных"]\nkk = ["талдаушы"]\n'
    )
    q = build_query(cues, "types.job", 4)
    assert q == '"data science" OR "аналитик данных" OR талдаушы OR quant'
    assert "*" not in build_query(cues, "types.job", 12)


def test_contact_without_at_disables_source():
    src = HHSource()
    env = {k: v for k, v in ENV.items() if k != "HTTP_CONTACT"}
    s = Settings.from_env(dict(env, HH_ACCESS_TOKEN="x"))
    reason = src.disabled_reason(s, _cfg())
    assert reason == "set HTTP_CONTACT to your email (hh requires it in the User-Agent)"
    assert not src.enabled(s, _cfg())
    # the token check stays first
    assert src.disabled_reason(Settings.from_env(env), _cfg()) == "needs HH_ACCESS_TOKEN"


def test_leftover_queries_key_rejected():
    import pytest

    with pytest.raises(ValueError, match="moved to config/cues.toml"):
        AppConfig.from_dict({"sources": {"hh": {"enabled": True, "queries": ["a"]}}})
