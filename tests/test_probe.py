import io
from datetime import UTC, datetime

import pytest

from src import db as dbm
from src import main as main_mod
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.http import build_client
from src.models import Posting
from src.pipeline import probe
from src.sources import base

ENV = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}


def make_fake(name, n=1, family=None, creds=(), enabled=True, fail=False):
    class Fake:
        description = "fake"

        def enabled(self, settings, cfg):
            return enabled

        async def fetch(self, ctx):
            if fail:
                raise RuntimeError("boom")
            return [
                Posting(
                    source=name,
                    external_id=str(i),
                    title="t",
                    company="c",
                    url=f"https://x/{name}/{i}",
                )
                for i in range(n)
            ]

    Fake.name = name
    if family:
        Fake.family = family
    if creds:
        Fake.credentials = creds
    return Fake


async def _probe(monkeypatch, fakes):
    monkeypatch.setattr(base, "REGISTRY", {f.name: f for f in fakes})
    conn = dbm.connect(":memory:")
    dbm.migrate(conn)
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    http = build_client(conn, clock)
    out = io.StringIO()
    code = await probe.run_probe(
        http, Settings.from_env(ENV, require_secrets=False), AppConfig(), clock, out
    )
    await http.aclose()
    return code, out.getvalue()


async def test_six_families_exit_0(monkeypatch):
    fakes = [make_fake(f"s{i}") for i in range(6)]
    code, text = await _probe(monkeypatch, fakes)
    assert code == 0
    assert len(text.strip().splitlines()) == 1 + 6 + 1
    assert text.strip().endswith("Counted families with items: 6 (need 6)")


async def test_five_families_exit_1(monkeypatch):
    code, text = await _probe(monkeypatch, [make_fake(f"s{i}") for i in range(5)])
    assert code == 1
    assert "Counted families with items: 5 (need 6)" in text


async def test_family_counts_once_and_needs_items(monkeypatch):
    fakes = [make_fake(f"a{i}", family="ats") for i in range(3)]
    fakes += [make_fake(f"s{i}") for i in range(4)]
    fakes += [make_fake("empty", n=0), make_fake("bad", fail=True)]
    code, text = await _probe(monkeypatch, fakes)
    assert code == 1
    assert "Counted families with items: 5 (need 6)" in text
    assert "bad  counted  no  0" in text


async def test_credential_gated_not_counted(monkeypatch):
    fakes = [make_fake(f"s{i}") for i in range(5)]
    fakes.append(make_fake("gated", creds=("TOKEN",)))
    fakes.append(make_fake("off", enabled=False))
    code, text = await _probe(monkeypatch, fakes)
    assert code == 1
    assert "gated  credentials: TOKEN  yes  1" in text
    assert "off  disabled:" in text


def test_main_probe_needs_no_secrets(monkeypatch, tmp_path):
    async def stub(http, settings, cfg, clock, out):
        return 0

    monkeypatch.setattr(probe, "run_probe", stub)
    assert main_mod.main(["probe", "--db", str(tmp_path / "p.db")], environ={}) == 0


def test_help_lists_probe(capsys):
    with pytest.raises(SystemExit):
        main_mod.main(["--help"], environ={})
    assert "probe" in capsys.readouterr().out


def test_probe_rerun_is_repeatable(monkeypatch, tmp_path):
    import json
    from pathlib import Path

    import httpx

    import src.sources  # noqa: F401  (registers sources)
    from src.sources.jobicy import JobicySource
    from src.sources.pagewatch import PageWatchSource

    fixtures = Path(__file__).parent / "fixtures"
    page = (fixtures / "page_v1.html").read_text()
    jobs = json.loads((fixtures / "jobicy_jobs.json").read_text())
    cfg_path = tmp_path / "c.toml"
    cfg_path.write_text(
        "[sources.pagewatch]\nenabled = true\n[[sources.pagewatch.pages]]\n"
        'url = "https://p.test/careers"\nname = "P"\nkind = "internship"\n'
        '[sources.jobicy]\nenabled = true\nindustries = ["data-science"]\ntags = []\n'
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.host == "jobicy.com":
            return httpx.Response(200, json=jobs)
        return httpx.Response(200, text=page, headers={"content-type": "text/html"})

    real_build = main_mod.build_client

    def fake_build(conn, clock, **kw):
        async def nosleep(_s):
            return None

        return real_build(conn, clock, transport=httpx.MockTransport(handler), sleep=nosleep, **kw)

    monkeypatch.setattr(main_mod, "build_client", fake_build)
    monkeypatch.setattr(
        base,
        "REGISTRY",
        {"pagewatch": PageWatchSource, "jobicy": JobicySource},
    )
    env = {"OPPO_CONFIG": str(cfg_path)}
    db_path = tmp_path / "probe.db"
    outs = []
    for _ in range(2):
        buf = io.StringIO()
        monkeypatch.setattr("sys.stdout", buf)
        code = main_mod.main(["probe", "--db", str(db_path)], environ=env)
        outs.append((code, buf.getvalue()))
    monkeypatch.undo()
    (c1, t1), (c2, t2) = outs
    assert c1 == c2
    line1 = next(x for x in t1.splitlines() if x.startswith("pagewatch"))
    line2 = next(x for x in t2.splitlines() if x.startswith("pagewatch"))
    assert line1 == line2
    assert int(line1.split()[-1]) >= 1
    assert sum(1 for u in seen if u.startswith("https://jobicy.com/api/jobs")) == 1
    conn = dbm.connect(str(db_path))
    hosts = {r["host"] for r in conn.execute("SELECT host FROM robots_cache")}
    conn.close()
    assert {"p.test", "jobicy.com"} <= hosts
