import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from src import db
from src.clock import FixedClock
from src.config import AppConfig
from src.contacts.discover import DiscoveryReport, discover_contacts
from src.contacts.store import contacts_for
from src.http import build_client
from src.llm.budget import TZ
from src.models import Kind, Posting
from src.ranking.weights import load_weights

FX = Path(__file__).parent / "fixtures"
CARDS = (FX / "contacts/team_page_cards.html").read_text()
ORGS = (FX / "contacts/organizer_page.html").read_text()
HN = (FX / "contacts/hn_comment_contact.json").read_text()
WEIGHTS = load_weights(FX / "weights_snapshot/weights.yaml")
CFG = AppConfig.load(FX / "config_repo_snapshot.toml")
NOW = datetime(2026, 11, 6, 5, tzinfo=UTC)


class Site:
    """MockTransport handler recording every request; routes is {path: (status, body)}."""

    def __init__(self, routes, robots="User-agent: *\nAllow: /\n"):
        self.routes = routes
        self.robots = robots
        self.urls = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.urls.append(str(req.url))
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text=self.robots)
        status, body = self.routes.get(req.url.path, (404, ""))
        return httpx.Response(status, text=body)

    def pages(self):
        return [u for u in self.urls if not u.endswith("/robots.txt")]


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    return c


def add(conn, url, kind=Kind.JOB, source="t", ext="1", company="Acme Fake", desc="", title="ML"):
    p = Posting(
        source=source, external_id=ext, title=title, company=company, url=url,
        description=desc, kind=kind,
    )  # fmt: skip
    db.upsert_posting(conn, p, "fp" + p.id, NOW)
    return conn.execute("SELECT * FROM postings WHERE id=?", (p.id,)).fetchone()


def client(conn, site):
    return build_client(
        conn, FixedClock(NOW), transport=httpx.MockTransport(site), min_interval_s=0
    )


async def run(conn, site, row, cfg=CFG, clock=None):
    http = client(conn, site)
    try:
        return await discover_contacts(conn, http, row, cfg, clock or FixedClock(NOW), WEIGHTS)
    finally:
        await http.aclose()


async def test_aggregator_host_no_page_fetch_but_text_contacts(conn):
    site = Site({})
    row = add(
        conn,
        "https://boards.greenhouse.io/acmefake/jobs/1",
        desc="Questions? Reach out to Sam Placeholder, our Head of Data.",
    )
    rep = await run(conn, site, row)
    assert site.urls == []
    assert rep.added == 1 and rep.unreachable == 0
    assert [c["name"] for c in contacts_for(conn, row["id"], NOW)] == ["Sam Placeholder"]


async def test_team_page_fetched_about_not(conn):
    site = Site({"/team": (200, CARDS)})
    row = add(conn, "https://acme-fake.test/jobs/1")
    rep = await run(conn, site, row)
    assert site.urls[0] == "https://acme-fake.test/robots.txt"
    assert site.pages() == ["https://acme-fake.test/team"]
    assert rep.added >= 3
    names = {c["name"] for c in contacts_for(conn, row["id"], NOW)}
    assert {"Dana Mockson", "Eli Testwell"} <= names
    kinds = {r[0] for r in conn.execute("SELECT source_kind FROM contacts")}
    assert kinds == {"team_page"}


async def test_team_404_then_about_and_no_third_path(conn):
    site = Site({"/about": (200, CARDS)})
    row = add(conn, "https://acme-fake.test/jobs/1")
    rep = await run(conn, site, row)
    assert site.pages() == ["https://acme-fake.test/team", "https://acme-fake.test/about"]
    assert rep.added >= 3 and rep.unreachable == 1


async def test_two_fetch_cap_even_if_nothing_found(conn):
    site = Site({})
    row = add(conn, "https://acme-fake.test/jobs/1")
    rep = await run(conn, site, row)
    assert len(site.pages()) == 2 and rep.added == 0 and rep.unreachable == 2
    assert not any("/careers" in u or "/contact" in u for u in site.urls)


async def test_robots_disallow_team_counts_unreachable(conn):
    site = Site({"/about": (200, CARDS)}, robots="User-agent: *\nDisallow: /team\n")
    row = add(conn, "https://acme-fake.test/jobs/1")
    rep = await run(conn, site, row)
    assert "https://acme-fake.test/team" not in site.urls
    assert rep.unreachable == 1 and rep.added >= 3


async def test_denied_403_stops(conn):
    site = Site({"/team": (403, "no")})
    row = add(conn, "https://acme-fake.test/jobs/1")
    rep = await run(conn, site, row)
    assert site.pages() == ["https://acme-fake.test/team"]
    assert rep.unreachable == 1


async def test_hackathon_own_page_as_organizer(conn):
    site = Site({"/event": (200, ORGS)})
    row = add(conn, "https://hack-fake.test/event", kind=Kind.HACKATHON, title="FakeHack")
    rep = await run(conn, site, row)
    assert site.pages() == ["https://hack-fake.test/event"]
    assert rep.added >= 1
    kinds = {r[0] for r in conn.execute("SELECT source_kind FROM contacts")}
    assert kinds == {"organizer_page"}


async def test_hackathon_on_mlh_is_not_fetched(conn):
    site = Site({})
    row = add(conn, "https://www.mlh.com/events/x", kind=Kind.HACKATHON)
    await run(conn, site, row)
    assert site.urls == []


async def test_second_discovery_within_7_days_makes_no_requests(conn):
    site = Site({"/team": (200, CARDS)})
    row = add(conn, "https://acme-fake.test/jobs/1")
    await run(conn, site, row)
    n = len(site.urls)
    rep = await run(conn, site, row, clock=FixedClock(NOW + timedelta(days=6)))
    assert len(site.urls) == n and rep == DiscoveryReport()


async def test_daily_cap_blocks_pages(conn):
    cfg = AppConfig.from_dict({"outreach": {"max_page_fetches_per_day": 20}})
    day = NOW.astimezone(TZ).date().isoformat()
    db.set_state(conn, f"contacts:pages:{day}", "20")
    site = Site({"/team": (200, CARDS)})
    row = add(conn, "https://acme-fake.test/jobs/1")
    rep = await run(conn, site, row, cfg=cfg)
    assert site.urls == [] and rep.added == 0
    # not stamped as discovered, so tomorrow it can still run
    assert db.get_state(conn, f"contacts:discovered:{row['id']}") is None


async def test_daily_counter_increments_and_caps_at_20(conn):
    site = Site({})
    for i in range(12):
        row = add(conn, f"https://acme{i}-fake.test/jobs/1", ext=str(i))
        await run(conn, site, row)
    assert len(site.pages()) == 20


async def test_watchlist_domain_beats_aggregator_url(conn):
    site = Site({"/team": (200, CARDS)})
    row = add(conn, "https://boards.greenhouse.io/kaspi/jobs/1", company="Kaspi.kz")
    await run(conn, site, row)
    assert site.pages() == ["https://kaspi.kz/team"]


async def test_hn_comment_fetched_once_no_pages(conn):
    site = Site({"/api/v1/items/777": (200, HN)})
    row = add(
        conn, "https://news.ycombinator.com/item?id=777", source="hn", ext="777", company="Acme"
    )
    rep = await run(conn, site, row)
    assert {u.split("/")[2] for u in site.urls} == {"hn.algolia.com"}
    names = {c["name"] for c in contacts_for(conn, row["id"], NOW)}
    assert "HN user fake_founder_fixture" in names and "Sam Placeholder" in names
    assert rep.added == 2


async def test_blocked_host_never_requested(conn):
    site = Site({})
    row = add(conn, "https://www.linkedin.com/jobs/view/1", company="Zed")
    await run(conn, site, row)
    assert site.urls == []


async def test_no_pii_stored_or_logged(conn, caplog):
    caplog.set_level("DEBUG")
    html = (
        '<h3>Zed Person</h3><p>Head of Data</p><a href="mailto:z@fake.example">z@fake.example</a>'
    )
    site = Site({"/team": (200, html)})
    row = add(conn, "https://acme-fake.test/jobs/1")
    await run(conn, site, row)
    dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM contacts")])
    assert "@" not in dump
    assert "Zed Person" not in caplog.text and "fake.example" not in caplog.text


def test_discover_not_referenced_from_pipeline_or_scheduler():
    root = Path(__file__).parent.parent / "src"
    files = [
        *(root / "pipeline").rglob("*.py"),
        root / "scheduler.py",
        root / "delivery" / "alerts.py",
    ]
    for f in files:
        assert not re.search(r"discover_contacts|contacts\.discover", f.read_text()), f


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/event",
        "http://169.254.169.254/latest",
        "http://[::1]/",
        "http://localhost/event",
        "http://2130706433/",
    ],
)
async def test_unsafe_hackathon_target_makes_no_request(conn, url):
    site = Site({})
    row = add(conn, url, kind=Kind.HACKATHON, title="FakeHack")
    rep = await run(conn, site, row)
    assert site.urls == [] and rep.unreachable >= 1 and rep.added == 0


async def test_client_refuses_ip_literal_and_redirect_to_it(conn):
    def handler(req):
        if req.url.host == "ok-fake.test":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/x"})
        return httpx.Response(200, text="leak")

    seen = []

    def rec(req):
        seen.append(str(req.url))
        return handler(req)

    http = build_client(conn, FixedClock(NOW), transport=httpx.MockTransport(rec), min_interval_s=0)
    assert await http.get_text("http://10.0.0.5/a", check_robots=False) is None
    assert await http.get_text("https://ok-fake.test/a", check_robots=False) is None
    assert all("169.254" not in u and "10.0.0.5" not in u for u in seen)
    await http.aclose()


async def test_normal_job_host_still_fetches_team(conn):
    site = Site({})
    row = add(conn, "https://acme-fake.test/jobs/1")
    await run(conn, site, row)
    assert "https://acme-fake.test/team" in site.pages()


async def test_non_default_port_event_page_skipped_but_team_fetched(conn):
    site = Site({})
    row = add(conn, "https://hack-fake.test:8443/event", kind=Kind.HACKATHON, title="FakeHack")
    rep = await run(conn, site, row)
    assert site.pages() == ["https://hack-fake.test/team"] and rep.unreachable == 2


BOARD_FIXTURES = (
    "lever_postings.json",
    "ashby_board.json",
    "jobicy_jobs.json",
    "hh_vacancies.json",
    "remoteok_api.json",
    "workable_account.json",
)
_URL_KEY = re.compile(r'"[A-Za-z_]*url"\s*:\s*"(https?://[^"]+)"', re.I)


@pytest.mark.parametrize("name", BOARD_FIXTURES)
# greenhouse (employer-hosted careers urls) and devpost (per-event hosts) are intentionally absent.
def test_every_board_fixture_host_is_aggregator(name):
    """FIX-42: a new board source must add its posting host to AGGREGATOR_HOSTS."""
    from urllib.parse import urlsplit

    from src.contacts.discover import is_aggregator_host

    urls = _URL_KEY.findall((FX / name).read_text())
    assert urls, name
    for u in urls:
        host = urlsplit(u).hostname or ""
        if host == "evil.example":  # bait url the remoteok source itself rejects
            continue
        assert is_aggregator_host(host), f"{name}: {host}"


@pytest.mark.parametrize(
    "url",
    ["https://remoteok.com/remote-jobs/remote-ml-1001", "https://apply.workable.com/j/A1B2C3D4E5"],
)
async def test_remoteok_and_workable_postings_fetch_nothing(conn, url):
    from src.contacts.discover import _page_targets

    row = add(conn, url, company="Unknown Startup Fake")
    assert _page_targets(row, WEIGHTS) == []
    site = Site({"/team": (200, CARDS), "/about": (200, CARDS)})
    await run(conn, site, row)
    assert site.urls == []


async def test_watchlist_company_via_remoteok_uses_watchlist_domain(conn):
    site = Site({"/team": (200, CARDS)})
    row = add(conn, "https://remoteok.com/remote-jobs/kaspi-1", company="Kaspi.kz")
    await run(conn, site, row)
    assert site.pages() == ["https://kaspi.kz/team"]
