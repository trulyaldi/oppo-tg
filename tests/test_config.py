from dataclasses import asdict
from pathlib import Path

import pytest

from src import config
from src.config import AppConfig, MissingConfig, PolitenessConfig, SchedulerConfig, Settings

FIXTURES = Path(__file__).parent / "fixtures"
REQUIRED = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}


def test_missing_lists_all_required():
    with pytest.raises(MissingConfig) as exc:
        Settings.from_env({})
    for name in REQUIRED:
        assert name in str(exc.value)
    assert sorted(exc.value.missing) == sorted(REQUIRED)


def test_defaults():
    s = Settings.from_env(REQUIRED)
    assert s.TIMEZONE == "Asia/Almaty"
    assert s.DIGEST_TIME == "09:00"
    assert s.MAX_SCORES_PER_RUN == 150
    assert s.scoring_model == "claude-haiku-4-5"
    assert s.hh_access_token is None


def test_stray_kaggle_env_ignored():
    s = Settings.from_env({**REQUIRED, "KAGGLE_KEY": "x"})
    assert not hasattr(s, "kaggle_key")


def test_overrides():
    s = Settings.from_env({**REQUIRED, "MAX_SCORES_PER_RUN": "5"})
    assert s.max_scores_per_run == 5


def test_env_vars_cover_settings():
    names = {v.name for v in config.ENV_VARS}
    assert {n for n, v in [(v.name, v) for v in config.ENV_VARS] if v.required} == set(REQUIRED)
    assert "TG_SESSION_PATH" in names


def test_app_config_fixture():
    cfg = AppConfig.load(FIXTURES / "config_min.toml")
    assert cfg.thresholds.alert_score == 9.0
    assert cfg.thresholds.alert_deadline_days == 7
    assert cfg.thresholds.alert_deadline_min_score == 0.0
    assert cfg.source_enabled("greenhouse")
    assert not cfg.source_enabled("kaggle")
    assert not cfg.source_enabled("nope")


def test_repo_config_loads():
    cfg = AppConfig.load(Path(__file__).parent.parent / "config.toml")
    assert cfg.thresholds.digest_min_score == 5.0
    assert AppConfig.from_dict({"thresholds": {"digest_max_items": 10}})
    assert cfg.filters["cues_file"] == "config/cues.toml"


def test_unknown_threshold_rejected():
    with pytest.raises(ValueError):
        AppConfig.from_dict({"thresholds": {"bogus": 1}})


def test_import_does_not_read_environ(monkeypatch):
    import importlib

    class Sentinel(dict):
        def __getitem__(self, k):
            raise AssertionError("environ read")

        def get(self, *a, **k):
            raise AssertionError("environ read")

    monkeypatch.setattr("os.environ", Sentinel())
    importlib.reload(config)


def test_default_http_contact_is_project_repo():
    from src.config import Settings

    s = Settings.from_env(
        {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}
    )
    assert s.http_contact == "https://github.com/trulyaldi/oppo-tg"


def test_legacy_digest_min_items_ignored():
    from src.config import AppConfig

    cfg = AppConfig.from_dict({"thresholds": {"digest_min_items": 5, "alert_score": 8.0}})
    assert cfg.thresholds.alert_score == 8.0


def test_outreach_unknown_key_fails_at_load():
    with pytest.raises(ValueError, match="daily_llm_cap"):
        AppConfig.from_dict({"outreach": {"daily_llm_cap": 5}})


@pytest.mark.parametrize(
    "bad, key",
    [
        ({"daily_llm_call_cap": "40"}, "daily_llm_call_cap"),
        ({"daily_llm_call_cap": -1}, "daily_llm_call_cap"),
        ({"cv_max_pages": 0}, "cv_max_pages"),
        ({"cv_max_pages": True}, "cv_max_pages"),
        ({"cheap_model": 3}, "cheap_model"),
    ],
)
def test_outreach_bad_values_name_the_key(bad, key):
    with pytest.raises(ValueError, match=key):
        AppConfig.from_dict({"outreach": bad})


def test_outreach_valid_and_snapshot_load():
    cfg = AppConfig.from_dict({"outreach": {"daily_llm_call_cap": 10}})
    assert cfg.outreach == {"daily_llm_call_cap": 10}
    AppConfig.load(Path(__file__).resolve().parent.parent / "config.toml")


def test_fc06_defaults_when_sections_absent():
    cfg = AppConfig.from_dict({})
    assert cfg.scheduler.tier1_minutes == 15 and cfg.scheduler.no_validator_min_minutes == 60
    assert cfg.politeness.max_concurrent_requests == 4 and cfg.politeness.hosts == {}
    th = cfg.thresholds
    assert (th.kz_fresh_alert_score, th.intl_fresh_alert_score) == (7.0, 8.0)
    assert th.fresh_alert_max_per_day == 6
    assert (th.fresh_window_hours, th.digest_score_band) == (48, 1.0)


def test_fc06_repo_config_documented_values():
    cfg = AppConfig.load(Path(__file__).resolve().parent.parent / "config.toml")
    assert asdict(cfg.scheduler) == asdict(SchedulerConfig())
    assert asdict(cfg.politeness) == asdict(PolitenessConfig(hosts={"remoteok.com": 5.0}))
    assert cfg.thresholds.fresh_window_hours == 48
    snap = AppConfig.load(Path(__file__).parent / "fixtures" / "config_repo_snapshot.toml")
    assert asdict(snap.scheduler) == asdict(cfg.scheduler)
    assert asdict(snap.thresholds) == asdict(cfg.thresholds)


def test_fc06_hosts_and_overrides_load():
    cfg = AppConfig.from_dict(
        {"scheduler": {"tier1_minutes": 5}, "politeness": {"hosts": {"example.com": 5.0}}}
    )
    assert cfg.scheduler.tier1_minutes == 5
    assert cfg.politeness.hosts == {"example.com": 5.0}


def test_fc06_source_tier():
    cfg = AppConfig.from_dict({"sources": {"hn": {"tier": "tier1"}, "x": {"tier": "fast"}}})
    assert cfg.source_tier("hn", "tier2") == "tier1"
    assert cfg.source_tier("rss", "tier2") == "tier2"
    with pytest.raises(ValueError, match="tier"):
        cfg.source_tier("x", "tier2")


@pytest.mark.parametrize(
    "data, key",
    [
        ({"scheduler": {"tier1_minutes": -1}}, "tier1_minutes"),
        ({"scheduler": {"tier2_minutes": 0}}, "tier2_minutes"),
        ({"scheduler": {"tier9_minutes": 5}}, "tier9_minutes"),
        ({"politeness": {"jitter_fraction": 1.5}}, "jitter_fraction"),
        ({"politeness": {"default_min_interval_s": -2}}, "default_min_interval_s"),
        ({"politeness": {"bogus": 1}}, "bogus"),
        ({"politeness": {"hosts": {"a.com": 0}}}, "a.com"),
        ({"thresholds": {"fresh_window_hours": 0}}, "fresh_window_hours"),
        ({"thresholds": {"digest_score_band": -1.0}}, "digest_score_band"),
        ({"thresholds": {"fresh_alert_max_per_day": 1.5}}, "fresh_alert_max_per_day"),
    ],
)
def test_fc06_bad_values_name_the_key(data, key):
    with pytest.raises(ValueError, match=key):
        AppConfig.from_dict(data)


def test_fc06_jobicy_floor_and_cron_env_description():
    from src.config import ENV_VARS
    from src.sources.jobicy import JobicySource

    assert JobicySource.min_poll_minutes == 60
    cron = next(v for v in ENV_VARS if v.name == "COLLECT_CRON_HOURS")
    assert cron.description == "ignored; see [scheduler] in config.toml"


def test_kf13_old_fresh_alert_score_key_names_new_keys():
    with pytest.raises(ValueError, match="kz_fresh_alert_score.*intl_fresh_alert_score"):
        AppConfig.from_dict({"thresholds": {"fresh_alert_score": 7.5}})
