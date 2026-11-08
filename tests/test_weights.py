import logging
import re
import shutil
from pathlib import Path

import pytest
import yaml

from src.config import AppConfig
from src.ranking.weights import (
    BUILTIN_WEIGHTS,
    WeightsError,
    bucket,
    load_weights,
    load_weights_or_default,
)

ROOT = Path(__file__).resolve().parent.parent
SNAP = ROOT / "tests" / "fixtures" / "weights_snapshot"


@pytest.fixture
def wdir(tmp_path):
    d = tmp_path / "w"
    shutil.copytree(SNAP, d)
    return d


def _edit(path: Path, fn):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_snapshot_values():
    w = load_weights(SNAP / "weights.yaml")
    assert w.adjustments.model_dump() == {
        "intl_top_tier": 0.5,
        "intl_other": 0.0,
        "kz_employer": 1.0,
        "kz_startup": 1.5,
        "kz_program": 1.0,
        "intl_onsite_visa_barrier": -0.5,
    }
    q = w.digest_quotas
    assert (q.kz, q.intl, q.best_remaining) == (6, 3, 1)
    assert w.region_min_confidence == 0.7
    assert len(w.intl_top_tier) == 14
    assert len(w.kz_employers) == 17
    assert w.version != "builtin" and len(w.version) == 12
    assert w.flags.kz and w.flags.intl


def test_repo_weights_validate():
    assert load_weights(ROOT / "config" / "weights.yaml").version != "builtin"


def test_snapshot_matches_repo_content():
    for rel in ("weights.yaml", "watchlists/intl_top_tier.yaml", "watchlists/kz_employers.yaml"):
        assert (SNAP / rel).read_bytes() == (ROOT / "config" / rel).read_bytes()


def test_snapshot_config_points_at_fixtures():
    cfg = AppConfig.load(ROOT / "tests" / "fixtures" / "config_repo_snapshot.toml")
    p = (ROOT / cfg.ranking["weights_file"]).resolve()
    assert "tests/fixtures" in p.as_posix()
    assert p.exists()
    real = AppConfig.load(ROOT / "config.toml")
    assert real.ranking["weights_file"] == "config/weights.yaml"
    assert cfg.outreach["cheap_model"] == "claude-haiku-4-5"


def test_missing_adjustment_key(wdir):
    _edit(wdir / "weights.yaml", lambda d: d["adjustments"].pop("kz_startup"))
    with pytest.raises(WeightsError, match="kz_startup"):
        load_weights(wdir / "weights.yaml")


def test_missing_kz_program_key(wdir):
    _edit(wdir / "weights.yaml", lambda d: d["adjustments"].pop("kz_program"))
    with pytest.raises(WeightsError, match="kz_program"):
        load_weights(wdir / "weights.yaml")


def test_unknown_key(wdir):
    _edit(wdir / "weights.yaml", lambda d: d.update(bogus=1))
    with pytest.raises(ValueError, match="bogus"):
        load_weights(wdir / "weights.yaml")
    _edit(wdir / "weights.yaml", lambda d: (d.pop("bogus"), d["adjustments"].update(oops=1)))
    with pytest.raises(WeightsError, match="oops"):
        load_weights(wdir / "weights.yaml")


def test_negative_quota(wdir):
    _edit(wdir / "weights.yaml", lambda d: d["digest_quotas"].update(kz=-1))
    with pytest.raises(ValueError, match="kz"):
        load_weights(wdir / "weights.yaml")


def test_all_zero_quota(wdir):
    _edit(
        wdir / "weights.yaml",
        lambda d: d.update(digest_quotas={"kz": 0, "intl": 0, "best_remaining": 0}),
    )
    with pytest.raises(ValueError, match="digest_quotas|best_remaining"):
        load_weights(wdir / "weights.yaml")


def test_missing_watchlist_file(wdir):
    (wdir / "watchlists" / "kz_employers.yaml").unlink()
    with pytest.raises(WeightsError, match="kz_employers"):
        load_weights(wdir / "weights.yaml")


def test_duplicate_alias_across_files(wdir):
    def add(d):
        d["employers"].append({"name": "Other", "aliases": ["kaspi"], "domain": None})

    _edit(wdir / "watchlists" / "intl_top_tier.yaml", add)
    with pytest.raises(WeightsError, match="(?i)kaspi"):
        load_weights(wdir / "weights.yaml")


def test_load_or_default_missing_and_invalid(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        w = load_weights_or_default(tmp_path / "nope.yaml")
    assert w is BUILTIN_WEIGHTS and w.version == "builtin"
    bad = tmp_path / "bad.yaml"
    bad.write_text(": : :", encoding="utf-8")
    assert load_weights_or_default(bad, logging.getLogger("t")).version == "builtin"
    assert BUILTIN_WEIGHTS.intl_top_tier == [] and BUILTIN_WEIGHTS.kz_employers == []


def test_version_changes_with_one_byte(wdir):
    before = load_weights(wdir / "weights.yaml").version
    f = wdir / "watchlists" / "intl_top_tier.yaml"
    f.write_bytes(f.read_bytes() + b"#")
    after = load_weights(wdir / "weights.yaml").version
    assert before != after


def test_bucket():
    assert bucket("kz_employer") == bucket("kz_startup") == bucket("kz_program") == "kz"
    assert bucket("intl_top_tier") == bucket("intl_other") == "intl"
    assert bucket("unknown") is None and bucket(None) is None


def test_no_unsafe_yaml_load_in_src():
    for py in (ROOT / "src").rglob("*.py"):
        assert not re.search(r"yaml\.load\(", py.read_text(encoding="utf-8")), py


def test_deps_importable():
    import pypdf  # noqa: F401
    import yaml  # noqa: F401
