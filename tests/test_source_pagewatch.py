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
from src.sources.pagewatch import PageWatchSource, extract_text

FX = Path(__file__).parent / "fixtures"
V1 = (FX / "page_v1.html").read_text()
V2 = (FX / "page_v2.html").read_text()
URL = "https://example.org/internships"


def _cfg(selector=None, enabled=True):
    page = {"url": URL, "name": "Example Fellowship", "kind": "fellowship"}
    if selector:
        page["css_selector"] = selector
    return AppConfig.from_dict({"sources": {"pagewatch": {"enabled": enabled, "pages": [page]}}})


class Harness:
    def __init__(self, cfg, body=V1, status=200):
        self.conn = dbm.connect(":memory:")
        dbm.migrate(self.conn)
        self.body = body
        self.status = status
        self.cfg = cfg
        self.clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))

    def run(self):
        def handler(req):
            if req.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nAllow: /\n")
            return httpx.Response(self.status, text=self.body)

        async def nosleep(_):
            pass

        http = build_client(
            self.conn, self.clock, transport=httpx.MockTransport(handler), sleep=nosleep
        )
        ctx = SourceContext(
            http=http,
            settings=Settings.from_env(
                {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}
            ),
            cfg=self.cfg,
            clock=self.clock,
            log=logging.getLogger("test"),
        )
        return ctx


async def _fetch(h):
    # the response cache is shared via the DB; clear it so a new body is seen
    h.conn.execute("DELETE FROM http_cache")
    return await run_source(PageWatchSource(), h.run())


async def test_first_sight_then_unchanged_then_changed():
    h = Harness(_cfg())
    r = await _fetch(h)
    assert r.ok and len(r.items) == 1
    p = r.items[0]
    assert p.title == "Example Fellowship"
    assert p.kind == Kind.FELLOWSHIP
    assert p.external_id.startswith(URL + ":") and len(p.external_id) == len(URL) + 13
    assert "internship program" in p.description

    assert (await _fetch(h)).items == []

    h.body = V2
    r = await _fetch(h)
    assert len(r.items) == 1
    assert r.items[0].title == "Example Fellowship: page updated"
    assert "Apply by 15 December" in r.items[0].description
    assert (await _fetch(h)).items == []


async def test_script_and_whitespace_changes_ignored():
    h = Harness(_cfg())
    assert len((await _fetch(h)).items) == 1
    h.body = V1.replace("aaa111", "zzz999").replace("<p>", "\n  <p>  ")
    assert (await _fetch(h)).items == []


def test_selector_applied():
    assert extract_text(V1, ".deadlines") == "Applications for 2027 internships open in August."
    assert "Footer" not in extract_text(V1)
    assert "Home" not in extract_text(V1)  # <main> beats <body>
    assert "Footer" in extract_text("<body><p>x</p><footer>Footer</footer></body>")
    # unknown selector falls back to <main>
    assert extract_text(V1, ".nope").startswith("Internships")


async def test_selector_ignores_changes_outside():
    h = Harness(_cfg(".deadlines"))
    assert len((await _fetch(h)).items) == 1
    h.body = V1.replace("<h1>Internships</h1>", "<h1>Internships 2</h1>")
    assert (await _fetch(h)).items == []


async def test_http_error_returns_empty():
    h = Harness(_cfg(), status=500)
    r = await _fetch(h)
    assert not r.ok and r.error and r.items == []
    r = await _fetch(Harness(_cfg(), status=403))
    assert not r.ok and r.error and r.items == []


async def test_unchanged_page_is_ok_and_empty():
    h = Harness(_cfg())
    assert (await _fetch(h)).ok
    r = await _fetch(h)
    assert r.ok and r.items == []


async def test_malformed_and_empty_pages():
    for body in ("", "<script>x</script>", "<html></html>"):
        h = Harness(_cfg(), body=body)
        r = await _fetch(h)
        assert r.ok and r.items == []


async def test_robots_disallow_skips_page():
    h = Harness(_cfg())

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        raise AssertionError("page must not be fetched")

    async def nosleep(_):
        pass

    ctx = h.run()
    ctx.http = build_client(h.conn, h.clock, transport=httpx.MockTransport(handler), sleep=nosleep)
    res = await run_source(PageWatchSource(), ctx)
    assert not res.ok and res.error and res.items == []


def test_registered_and_enabled():
    assert "pagewatch" in REGISTRY
    s = Settings.from_env(
        {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}
    )
    assert PageWatchSource().enabled(s, _cfg())
    assert not PageWatchSource().enabled(s, _cfg(enabled=False))
    assert not PageWatchSource().enabled(s, AppConfig.from_dict({}))


async def test_company_key_used_and_falls_back_to_name():
    from src.ranking.employer import classify_employer
    from src.ranking.weights import load_weights

    w = load_weights(FX / "weights_snapshot" / "weights.yaml")
    page = {"url": URL, "name": "IMC Careers", "kind": "internship", "company": "IMC"}
    cfg = AppConfig.from_dict({"sources": {"pagewatch": {"enabled": True, "pages": [page]}}})
    r = await _fetch(Harness(cfg))
    assert r.items[0].company == "IMC"
    assert classify_employer("IMC", "internship", "unknown", 0, None, w).category == (
        "intl_top_tier"
    )
    del page["company"]
    r = await _fetch(Harness(cfg))
    assert r.items[0].company == "IMC Careers"
    assert classify_employer("IMC Careers", "internship", "unknown", 0, None, w).category == (
        "unknown"
    )


# ---- KF-25: type key and the vetted pages --------------------------------------------------

import json  # noqa: E402
import tomllib  # noqa: E402
from urllib.parse import urlsplit  # noqa: E402

import pytest  # noqa: E402

from src import robots  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
D245_KZ = {
    "https://job.kaspi.kz/",
    "https://halykbank.kz/about/career",
    "https://kolesa.group/career/job",
    "https://btsdigital.kz/ru/career",
    "https://career.forte.kz/",
    "https://campus.epam.kz/ru",
    "https://nu.edu.kz/about/work-at-nu/jobs-at-nu/",
    "https://nu.edu.kz/students/academic-mobility-and-career/summer-schools/",
    "https://nu.edu.kz/students/academic-mobility-and-career/jobs-and-mentoring/",
    "https://issai.nu.edu.kz/",
    "https://research.nu.edu.kz/",
    "https://science-fund.kz/",
}


def _repo_pages():
    return tomllib.loads((ROOT / "config.toml").read_text())["sources"]["pagewatch"]["pages"]


def test_repo_pages_have_valid_types_and_kz_urls_match_d245():
    from src.models import OpportunityType

    pages = _repo_pages()
    assert len(pages) == 19
    for p in pages:
        assert OpportunityType(p["type"])
    urls = {p["url"] for p in pages}
    assert urls >= D245_KZ
    assert len(urls) == len(pages)
    by_name = {p["name"]: p["type"] for p in pages}
    assert by_name["CERN Summer Student Programme"] == "summer_school"
    assert by_name["ETH Student Summer Research Fellowship"] == "fellowship"
    assert by_name["Science Fund"] == "grant"


def test_every_repo_pagewatch_url_allowed_by_robots_fixture():
    hosts = json.loads((FX / "robots_pagewatch_seeds.json").read_text())["hosts"]
    for p in _repo_pages():
        parts = urlsplit(p["url"])
        entry = hosts[parts.netloc]
        if entry["status"] == 404:
            continue
        rules = robots.parse(entry["body"])
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        assert robots.allowed(rules, "oppo-tg", path), p["url"]


def test_invalid_type_fails_naming_page():
    page = {"url": URL, "name": "Bad Page", "kind": "job", "type": "nonsense"}
    with pytest.raises(ValueError, match="Bad Page"):
        AppConfig.from_dict({"sources": {"pagewatch": {"enabled": True, "pages": [page]}}})


async def test_configured_type_sets_type_source_config():
    page = {"url": URL, "name": "Sci Fund", "kind": "other", "type": "grant"}
    cfg = AppConfig.from_dict({"sources": {"pagewatch": {"enabled": True, "pages": [page]}}})
    h = Harness(cfg)
    r = await _fetch(h)
    assert r.items[0].opportunity_type == "grant"
    dbm.upsert_posting(h.conn, r.items[0], "fp", h.clock.now())
    row = h.conn.execute("SELECT opportunity_type, type_source FROM postings").fetchone()
    assert (row["opportunity_type"], row["type_source"]) == ("grant", "config")


async def test_untyped_page_keeps_kind_type_source():
    h = Harness(_cfg())
    r = await _fetch(h)
    assert r.items[0].opportunity_type is None
    dbm.upsert_posting(h.conn, r.items[0], "fp", h.clock.now())
    row = h.conn.execute("SELECT type_source FROM postings").fetchone()
    assert row["type_source"] == "kind"


def test_kz_pagewatch_classification():
    from src.ranking.employer import classify_employer
    from src.ranking.weights import load_weights

    w = load_weights(ROOT / "config" / "weights.yaml")

    def cat(company, kind, opp=None):
        return classify_employer(
            company, kind, "unknown", 0, None, w, opportunity_type=opp
        ).category

    assert cat("Halyk Bank", "job", "job") == "kz_employer"
    assert cat("Science Fund", "other", "grant") == "kz_program"
    assert cat("Фонд науки", "other", "grant") == "kz_program"
    assert cat("АО Фонд науки", "other", "grant") == "kz_program"
    assert cat("Institute of Smart Systems and Artificial Intelligence", "research") == (
        "kz_employer"
    )
    assert cat("EPAM Kazakhstan", "internship") == "unknown"
