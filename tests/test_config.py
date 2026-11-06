from pathlib import Path

import pytest

from src import config
from src.config import AppConfig, MissingConfig, Settings

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
    assert "positive_title_keywords" in cfg.filters


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
