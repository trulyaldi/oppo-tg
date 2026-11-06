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
