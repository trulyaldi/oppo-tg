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


def kcls(company, otype, region="kz", conf=0.9, startup=None, kind=Kind.JOB, hint=None):
    c = classify_employer(company, kind, region, conf, startup, W, hint, otype)
    return c.category, c.source


def test_kz_program_is_exclusive():
    # a grant at a KZ startup is a program, never stacked (D-248)
    assert kcls("Acme Robotics", "grant", startup=True) == ("kz_program", "llm")
    assert kcls("Arbuz.kz", "hackathon") == ("kz_program", "watchlist")
    assert kcls("Kaspi.kz", "accelerator") == ("kz_program", "watchlist")
    assert kcls("Acme Robotics", "summer_school", region="unknown", conf=0, hint="kz") == (
        "kz_program",
        "hint",
    )
    # kind fallback when the type is unknown
    assert kcls("Acme Robotics", None, kind=Kind.HACKATHON) == ("kz_program", "llm")


def test_kz_non_program_types():
    assert kcls("Arbuz.kz", "job") == ("kz_startup", "watchlist")
    assert kcls("Kaspi.kz", "research") == ("kz_employer", "watchlist")
    assert kcls("Acme Robotics", "startup_role", startup=False) == ("kz_startup", "llm")
    assert kcls("Acme Robotics", "bogus") == ("kz_employer", "llm")


def test_program_type_does_not_change_intl():
    assert kcls("Citadel", "grant") == ("intl_top_tier", "watchlist")
    assert kcls("Acme Robotics", "grant", region="intl", conf=0.9) == ("intl_other", "llm")


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


def ll_cls(company, language, location, region="unknown", conf=0.0, kind=Kind.JOB, **kw):
    c = classify_employer(
        company, kind, region, conf, None, W, language=language, location=location, **kw
    )
    return c.category, c.source


def test_lang_loc_russian_almaty_is_kz():
    assert ll_cls("Nobody Ltd", "ru", "Алматы") == ("kz_employer", "lang_loc")


def test_lang_loc_english_stays_unknown():
    assert ll_cls("Nobody Ltd", "en", "Almaty") == ("unknown", "none")


def test_lang_loc_kazakh_astana_is_kz():
    assert ll_cls("Nobody Ltd", "kk", "Астана") == ("kz_employer", "lang_loc")


def test_lang_loc_russian_moscow_stays_unknown():
    assert ll_cls("Nobody Ltd", "ru", "Москва") == ("unknown", "none")


def test_lang_loc_watchlist_intl_wins():
    assert ll_cls("Citadel", "ru", "Алматы") == ("intl_top_tier", "watchlist")


def test_lang_loc_confident_intl_wins():
    assert ll_cls("Nobody Ltd", "ru", "Алматы", "intl", 0.9) == ("intl_other", "llm")


def test_lang_loc_program_type():
    c = classify_employer(
        "Nobody", Kind.JOB, "unknown", 0.0, None, W,
        opportunity_type="hackathon", language="ru", location="Алматы",
    )  # fmt: skip
    assert (c.category, c.source) == ("kz_program", "lang_loc")
