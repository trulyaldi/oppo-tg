"""Workable source tests (FC-18, D-173, D-215).

tests/fixtures/workable_account.json is HAND-WRITTEN from the documented response shape
(D-173 / Workable help-centre docs), NOT captured from a live response. A real-response
check is deferred to the final smoke test or to the user.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx

from src import db as dbm
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.discovery.extract import find_board_refs
from src.http import build_client
from src.models import Kind
from src.sources.base import REGISTRY, SourceContext, ValidationUnavailable, run_source
from src.sources.workable import WorkableSource, parse_jobs

FIXTURE = (Path(__file__).parent / "fixtures" / "workable_account.json").read_text()
SETTINGS = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}


def _cfg(accounts, names=None, boards_file="/nonexistent/boards.toml"):
    return AppConfig.from_dict(
        {
            "sources": {"workable": {"enabled": True, "accounts": accounts, "names": names or {}}},
            "discovery": {"boards_file": boards_file},
        }
    )


def _ctx(routes, cfg):
    conn = dbm.connect(":memory:")
    dbm.migrate(conn)
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    requests = []

    def handler(req):
        requests.append(req)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        r = routes[req.url.path.split("/")[3]]
        return r(req) if callable(r) else r

    async def nosleep(_):
        pass

    http = build_client(conn, clock, transport=httpx.MockTransport(handler), sleep=nosleep)
    ctx = SourceContext(
        http=http,
        settings=Settings.from_env(SETTINGS),
        cfg=cfg,
        clock=clock,
        log=logging.getLogger("test"),
    )
    return ctx, requests


async def test_fixture_parses():
    ctx, requests = _ctx(
        {"acme": httpx.Response(200, text=FIXTURE)}, _cfg(["acme"], {"acme": "Acme"})
    )
    res = await run_source(WorkableSource(), ctx)
    assert res.ok
    first, intern, third = res.items
    assert len(res.items) == 3  # the entry without url/code is skipped
    assert first.source == "workable"
    assert first.external_id == "acme:A1B2C3D4E5"
    assert first.company == "Acme"
    assert first.url == "https://apply.workable.com/j/A1B2C3D4E5"
    assert first.location == "Almaty, Kazakhstan"
    assert first.remote is None
    assert first.kind == Kind.JOB
    assert first.published_at == datetime(2026, 10, 20, tzinfo=UTC)
    assert "ML models" in first.description and "Python" in first.description
    assert "<" not in first.description
    assert intern.kind == Kind.INTERNSHIP
    assert intern.remote is True and intern.location == "Remote"
    assert intern.published_at == datetime(2026, 10, 25, tzinfo=UTC)  # created_at fallback
    assert third.location == "Berlin, Germany"
    paths = [r.url.path for r in requests if r.url.path != "/robots.txt"]
    assert paths == ["/api/accounts/acme"]
    assert requests[-1].url.params["details"] == "true"


def test_company_falls_back_to_payload_name_then_slug():
    (p, *_) = parse_jobs(__import__("json").loads(FIXTURE), "acme", "")
    assert p.company == "Acme Robotics"
    (q,) = parse_jobs(
        {"jobs": [{"title": "T", "shortcode": "X", "url": "https://apply.workable.com/j/X"}]},
        "s",
        "",
    )
    assert q.company == "s"


def test_non_apply_host_urls_are_skipped():
    job = {"title": "T", "shortcode": "X", "url": "https://evil.example/j/X"}
    assert parse_jobs({"jobs": [job]}, "s", "") == []


async def test_empty_and_malformed():
    ctx, _ = _ctx({"acme": httpx.Response(200, text='{"name": "A", "jobs": []}')}, _cfg(["acme"]))
    res = await run_source(WorkableSource(), ctx)
    assert res.ok and res.items == []
    ctx, _ = _ctx({"acme": httpx.Response(200, text="{not json")}, _cfg(["acme"]))
    res = await run_source(WorkableSource(), ctx)
    assert not res.ok and res.error and res.items == []
    assert parse_jobs({"jobs": "x"}, "s", "") == []
    assert parse_jobs([], "s", "") == []
    assert parse_jobs({"jobs": [None, "junk", {"title": "x"}]}, "s", "") == []


async def test_http_errors_and_isolation():
    ctx, _ = _ctx({"bad": httpx.Response(500, text="boom")}, _cfg(["bad"]))
    res = await run_source(WorkableSource(), ctx)
    assert not res.ok and res.error and res.items == []
    ctx, _ = _ctx({"bad": httpx.Response(404, text="no")}, _cfg(["bad"]))
    res = await run_source(WorkableSource(), ctx)
    assert not res.ok and res.items == []
    ctx, _ = _ctx(
        {"bad": httpx.Response(500, text="boom"), "acme": httpx.Response(200, text=FIXTURE)},
        _cfg(["acme", "bad"]),
    )
    res = await run_source(WorkableSource(), ctx)
    assert res.ok and len(res.items) == 3


def test_registered_and_enabled(tmp_path):
    assert "workable" in REGISTRY
    src = WorkableSource()
    s = Settings.from_env(SETTINGS)
    assert src.family == "ats"
    assert src.enabled(s, _cfg(["a"]))
    assert not src.enabled(s, _cfg([]))
    f = tmp_path / "boards.toml"
    from src.discovery import store

    store.add(f, "workable", "found", "Found")
    cfg = _cfg([], boards_file=str(f))
    assert src.enabled(s, cfg)
    ctx, _ = _ctx({}, cfg)
    (t,) = src.targets(ctx)
    assert (t.key, t.origin, t.tier) == ("found", "discovered", "tier2")


async def test_validate():
    src = WorkableSource()
    ctx, requests = _ctx({"acme": httpx.Response(200, text=FIXTURE)}, _cfg([]))
    assert await src.validate(ctx, "acme") == (True, "Acme Robotics")
    assert [r.url.host for r in requests if r.url.path != "/robots.txt"] == ["www.workable.com"]
    ctx, _ = _ctx({"nope": httpx.Response(404, text="")}, _cfg([]))
    assert await src.validate(ctx, "nope") == (False, "")
    ctx, _ = _ctx({"odd": httpx.Response(200, text="[]")}, _cfg([]))
    assert await src.validate(ctx, "odd") == (False, "")
    ctx, _ = _ctx({"down": httpx.Response(503, text="")}, _cfg([]))
    try:
        await src.validate(ctx, "down")
    except ValidationUnavailable:
        pass
    else:
        raise AssertionError("expected ValidationUnavailable")


def test_extractor_url_resolves_to_validatable_target():
    refs = find_board_refs("Apply at https://apply.workable.com/Acme/j/ABC123/ today")
    assert [(r.source, r.token) for r in refs] == [("workable", "acme")]
    assert callable(getattr(REGISTRY["workable"](), "validate", None))
