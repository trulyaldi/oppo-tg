import os
import tomllib
from datetime import UTC, datetime

import httpx
import pytest

from src.clock import FixedClock
from src.config import AppConfig, DiscoveryConfig, Settings
from src.discovery import store
from src.sources.base import SourceContext, source_targets
from src.sources.greenhouse import GreenhouseSource

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_hostile_company_round_trips_and_is_clipped(tmp_path):
    path = tmp_path / "b.toml"
    company = 'a"b\n[x]\\c' + "z" * 10_000
    assert store.add(path, "greenhouse", "acme", company, NOW)
    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert parsed["board"][0]["company"] == company[:120]
    assert len(parsed["board"]) == 1
    assert store.load(path)[0].company == company[:120]


def test_control_chars_round_trip(tmp_path):
    path = tmp_path / "b.toml"
    store.add(path, "lever", "x", "t\x00\x07\x7f é", NOW)
    assert store.load(path)[0].company == "t\x00\x07\x7f é"


def test_failed_write_keeps_old_file(tmp_path, monkeypatch):
    path = tmp_path / "b.toml"
    store.add(path, "greenhouse", "one", "One", NOW)
    before = path.read_bytes()

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.add(path, "greenhouse", "two", "Two", NOW)
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_file_loads_empty_and_add_raises(tmp_path):
    path = tmp_path / "b.toml"
    path.write_text("[[board]\nsource = ", encoding="utf-8")
    assert store.load(path) == []
    with pytest.raises(store.StoreCorrupt):
        store.add(path, "greenhouse", "x", "X", NOW)
    assert path.read_text(encoding="utf-8").startswith("[[board]\n")


def test_tombstone_is_known_and_not_active(tmp_path):
    path = tmp_path / "b.toml"
    store.add(path, "greenhouse", "acme", "Acme", NOW)
    store.tombstone(path, "greenhouse", "acme")
    assert store.is_known(path, "greenhouse", "acme")
    assert store.active_boards(path, "greenhouse") == []
    assert not store.add(path, "greenhouse", "acme", "Acme", NOW)
    store.tombstone(path, "lever", "never-seen")
    assert store.is_known(path, "lever", "never-seen")


def test_missing_file(tmp_path):
    assert store.load(tmp_path / "none.toml") == []
    assert not store.is_known(tmp_path / "none.toml", "greenhouse", "x")


def _http():
    from src import db as dbm
    from src.http import build_client

    conn = dbm.connect(":memory:")
    dbm.migrate(conn)
    return build_client(conn, FixedClock(NOW), transport=httpx.MockTransport(lambda r: None))


def _ctx(tmp_path, boards, **disc):
    cfg = AppConfig.from_dict(
        {
            "sources": {"greenhouse": {"enabled": True, "boards": boards}},
            "discovery": {"boards_file": str(tmp_path / "b.toml"), **disc},
        }
    )
    settings = Settings.from_env(
        {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}
    )
    return SourceContext(http=_http(), settings=settings, cfg=cfg, clock=FixedClock(NOW))


def test_greenhouse_targets_include_discovered_as_tier2(tmp_path):
    path = tmp_path / "b.toml"
    store.add(path, "greenhouse", "found", "Found Inc", NOW)
    store.add(path, "greenhouse", "gone", "Gone", NOW)
    store.tombstone(path, "greenhouse", "gone")
    store.add(path, "lever", "other", "Other", NOW)
    ts = source_targets(GreenhouseSource(), _ctx(tmp_path, ["cfgco"]))
    assert [(t.key, t.origin, t.tier) for t in ts] == [
        ("cfgco", "config", ""),
        ("found", "discovered", "tier2"),
    ]
    assert ts[1].company == "Found Inc"


def test_config_token_wins_over_discovered(tmp_path):
    store.add(tmp_path / "b.toml", "greenhouse", "Acme", "Acme", NOW)
    ts = source_targets(GreenhouseSource(), _ctx(tmp_path, ["acme"]))
    assert [(t.key, t.origin) for t in ts] == [("acme", "config")]


def test_discovery_disabled_adds_nothing(tmp_path):
    store.add(tmp_path / "b.toml", "greenhouse", "found", "F", NOW)
    ts = source_targets(GreenhouseSource(), _ctx(tmp_path, ["a"], enabled=False))
    assert [t.key for t in ts] == ["a"]


def test_discovery_config_defaults_and_validation():
    d = DiscoveryConfig()
    assert (d.max_validations_per_day, d.max_new_boards_per_day) == (40, 20)
    assert (d.negative_cache_days, d.run_time) == (30, "05:30")
    assert d.boards_file == "data/discovered_boards.toml"
    with pytest.raises(ValueError):
        DiscoveryConfig(run_time="25:00")
    with pytest.raises(ValueError):
        AppConfig.from_dict({"discovery": {"bogus": 1}})
