"""D-52 source count rule, offline (PLAN 1.5.1)."""

from pathlib import Path

import src.sources  # noqa: F401  (import registers all sources)
from src.config import AppConfig, Settings
from src.sources.base import (
    MIN_SOURCE_FAMILIES,
    REGISTRY,
    count_report,
    source_credentials,
)

ROOT = Path(__file__).resolve().parent.parent
BASE_ENV = {
    "TELEGRAM_BOT_TOKEN": "x",
    "TELEGRAM_CHAT_ID": "1",
    "ANTHROPIC_API_KEY": "y",
}


def _report(extra=None):
    # Deliberately reads the real config.toml: DoD guard for >= 6 sources (FIX-06).
    cfg = AppConfig.load(ROOT / "config.toml")
    return count_report(Settings.from_env({**BASE_ENV, **(extra or {})}), cfg)


def test_every_module_is_registered():
    mods = {
        p.stem
        for p in (ROOT / "src" / "sources").glob("*.py")
        if p.stem not in {"base", "__init__", "telegram_live", "telegram_ro"}  # helpers
    }
    assert mods <= set(REGISTRY)


def test_ats_family_and_exclusions():
    r = _report()
    assert r.counted["ats"] == ["ashby", "greenhouse", "lever"]
    assert "devpost" in r.disabled
    assert "hh" in r.credential_gated
    counted = {n for names in r.counted.values() for n in names}
    assert not counted & {"devpost", "hh"}


def test_minimum_families():
    r = _report()
    assert len(r.counted) >= MIN_SOURCE_FAMILIES
    assert set(r.counted) == {
        "ats",
        "hn",
        "rss",
        "pagewatch",
        "mlh",
        "jobicy",
        "remoteok",
        "arbeitnow",
    }


def test_counted_have_tests_and_no_credentials():
    r = _report()
    for names in r.counted.values():
        for n in names:
            assert (ROOT / "tests" / f"test_source_{n}.py").exists(), n
            assert source_credentials(REGISTRY[n]()) == ()


def test_credentials_do_not_change_count():
    assert _report({"HH_ACCESS_TOKEN": "tok"}).counted == _report().counted
