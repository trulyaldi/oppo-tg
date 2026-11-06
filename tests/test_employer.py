from pathlib import Path

import pytest

from src.models import Kind
from src.ranking.employer import classify_employer, normalize_company
from src.ranking.weights import load_weights

W = load_weights(Path(__file__).parent / "fixtures" / "weights_snapshot" / "weights.yaml")


def cls(company, region="unknown", conf=0.0, startup=None, kind=Kind.JOB):
    c = classify_employer(company, kind, region, conf, startup, W)
    return c.category, c.source


@pytest.mark.parametrize(
    ("company", "expected"),
    [
        ("IMC Trading", ("intl_top_tier", "watchlist")),
        ("Jane Street Capital, LLC", ("intl_top_tier", "watchlist")),
        ("HRT", ("intl_top_tier", "watchlist")),
        ("The Citadel LLC", ("intl_top_tier", "watchlist")),
        ("ТОО Kolesa Group", ("kz_employer", "watchlist")),
        ("Kaspi.kz", ("kz_employer", "watchlist")),
        ("Arbuz.kz", ("kz_startup", "watchlist")),
        ("International Data Group", ("unknown", "none")),
        ("", ("unknown", "none")),
        (None, ("unknown", "none")),
    ],
)
def test_watchlists(company, expected):
    assert cls(company) == expected


def test_watchlist_beats_llm_region():
    assert cls("Kaspi", "intl", 0.99) == ("kz_employer", "watchlist")
    assert cls("Citadel", "kz", 0.99) == ("intl_top_tier", "watchlist")


def test_llm_fallback():
    assert cls("Acme Robotics", "kz", 0.9, True) == ("kz_startup", "llm")
    assert cls("Acme Robotics", "kz", 0.6, True) == ("unknown", "none")
    assert cls("Acme Robotics", "intl", 0.8) == ("intl_other", "llm")
    assert cls("Acme Robotics", "kz", 0.9, None, Kind.STARTUP) == ("kz_startup", "llm")
    assert cls("Acme Robotics", "kz", 0.9, False) == ("kz_employer", "llm")
    assert cls("", "intl", 0.8) == ("intl_other", "llm")
    assert cls("Acme Robotics", "unknown", 1.0) == ("unknown", "none")


def test_normalize():
    assert normalize_company("Kolesa Group LLP") == normalize_company("kolesa group")
    assert normalize_company("Kaspi.kz") == "kaspi.kz"
    assert normalize_company("Ёлка, ООО") == "елка"
    assert normalize_company("Inc.") == ""
    assert normalize_company("The Foo Co.") == "foo co"


def hint_cls(company, region="unknown", conf=0.0, startup=None, kind=Kind.JOB, hint="kz"):
    c = classify_employer(company, kind, region, conf, startup, W, hint)
    return c.category, c.source


def test_hint_unknown_company_is_kz():
    assert hint_cls("Nobody Ltd") == ("kz_employer", "hint")


def test_hint_low_confidence_llm_is_overridden():
    assert hint_cls("Nobody Ltd", "intl", 0.3) == ("kz_employer", "hint")


def test_hint_loses_to_confident_intl():
    assert hint_cls("Nobody Ltd", "intl", 0.9) == ("intl_other", "llm")


def test_hint_loses_to_watchlist():
    assert hint_cls("Citadel") == ("intl_top_tier", "watchlist")


def test_hint_startup():
    assert hint_cls("Nobody Ltd", kind=Kind.STARTUP) == ("kz_startup", "hint")
    assert hint_cls("Nobody Ltd", startup=True) == ("kz_startup", "hint")


def test_no_hint_stays_unknown():
    assert hint_cls("Nobody Ltd", hint=None) == ("unknown", "none")
