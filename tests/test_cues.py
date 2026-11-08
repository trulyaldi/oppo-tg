"""KF-05: cue file loader and matcher."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.cues import CueError, Cues, compile_entry, load_cues, match_any, parse_cues_text, which

ROOT = Path(__file__).resolve().parent.parent
MUST_DROP = [
    "Maintenance Technician",
    "Email Marketing Manager",
    "Domain Expert",
    "Retail Assistant",
    "Chairperson",
    "Air Astana cabin crew",
    "Стажер-бухгалтер",
    "Senior ML Engineer",
    "Водитель-курьер",
    "Менеджер по продажам",
]
MUST_KEEP = [
    "ML Intern",
    "AI Engineer (Junior)",
    "Quant Research Intern",
    "Стажер аналитик данных",
    "Деректер талдаушы (тағылымдама)",
    "Python бағдарламашы",
    "Machine Learning Research Assistant",
    "Летняя школа по физике",
    "Хакатон Astana",
]
FIXTURE = Path(__file__).parent / "fixtures" / "cue_titles.json"


@pytest.fixture(scope="module")
def cues() -> Cues:
    return load_cues(ROOT / "config" / "cues.toml")


def _one(entries: str, lang: str = "en"):
    return parse_cues_text(f"[negative]\n{lang} = [{entries}]\n")["negative"]


def test_expected_tables_present(cues: Cues):
    for name in (
        "types.internship", "types.hackathon", "fields.ml", "fields.cpp", "locations.kz",
        "negative", "seniority", "junior", "protect", "spam", "unpaid", "hard_blockers",
        "hh.internships", "telegram_discovery",
    ):  # fmt: skip
        assert name in cues.groups
    assert len(cues.names("types")) == 14
    assert len(cues.names("fields")) == 24


def test_positive_cue_counts(cues: Cues):
    assert cues.positive_count() >= 150
    assert cues.positive_count("ru") >= 35
    assert cues.positive_count("kk") >= 35
    assert len(cues.positive_terms()) >= 150


@pytest.mark.parametrize(
    "item", json.loads(FIXTURE.read_text(encoding="utf-8")), ids=lambda i: i["title"]
)
def test_title_fixture(cues: Cues, item):
    got = cues.title_decision(item["title"])
    exp = item["expected"]
    assert got == exp if ":" in exp else got.split(":")[0] == exp


def test_fixture_shape():
    items = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert len(items) >= 60
    for lang in ("en", "ru", "kk"):
        assert sum(1 for i in items if i["lang"] == lang) >= 15
    titles = {i["title"]: i["expected"] for i in items}
    for t in MUST_DROP:
        assert titles[t].startswith("drop")
    for t in MUST_KEEP:
        assert titles[t] == "keep"


@pytest.mark.parametrize("text", ["maintenance", "email", "domain", "chairperson", "Air Astana"])
def test_short_tokens_do_not_match_inside_words(cues: Cues, text: str):
    assert not match_any(cues["fields.ai"], text)
    assert not match_any(cues["fields.ml"], text)
    g = _one('"ai", "ma", "ml"')
    assert which(g, text) == []
    assert which(g, "AI and ML") == ["ai", "ml"]
    assert which(g, "ma") == ["ma"]


def test_cpp_vs_csharp(cues: Cues):
    assert match_any(cues["fields.cpp"], "C++ developer")
    assert not match_any(cues["fields.cpp"], "C# developer")
    assert not match_any(cues["fields.cpp"], "C developer")


@pytest.mark.parametrize("word", ["стажёр", "стажер", "Стажёром", "стажировка"])
def test_yo_folding_and_stem(cues: Cues, word: str):
    assert match_any(cues["types.internship"], word)


def test_intern_regex_keeps_internal_and_international_out(cues: Cues):
    g = cues["types.internship"]
    assert match_any(g, "Intern")
    assert match_any(g, "Summer internships")
    assert not match_any(g, "Internal Auditor")
    assert not match_any(g, "International Relations")


def test_stem_and_phrase_matching():
    g = _one('"машинн* обучени*", "back end"', "ru")
    assert which(g, "Машинное обучение") == ["машинн* обучени*"]
    assert which(g, "машинного обучения") == ["машинн* обучени*"]
    assert which(g, "Back-end") == ["back end"]
    assert which(g, "backend") == []


def test_kazakh_inflection(cues: Cues):
    assert match_any(cues["types.internship"], "Тағылымдамадан өту")
    assert match_any(cues["fields.ml"], "машиналық оқытуды үйрену")
    assert match_any(cues["fields.software"], "Бағдарламашыға")
    assert match_any(cues["locations.kz"], "Қарағандыда")
    assert match_any(cues["locations.kz"], "қашықтан жұмыс")


def test_locations_and_remote_phrasing(cues: Cues):
    kz = cues["locations.kz"]
    assert match_any(kz, "Nur-Sultan")
    assert match_any(kz, "Remote (KZ)")
    assert match_any(kz, "Удалённо, Казахстан")
    assert not match_any(kz, "Remote, Germany")


def test_hard_blockers_and_spam(cues: Cues):
    assert match_any(cues["hard_blockers"], "Requires Security Clearance")
    assert match_any(cues["hard_blockers"], "Только граждане РФ")
    assert match_any(cues["spam"], "Earn $500 per day")
    assert not match_any(cues["spam"], "Earn a degree")


def test_expand_and_group_terms(cues: Cues):
    out = cues.expand("hackathon")
    assert out[0] == "hackathon"
    assert {"datathon", "хакатон", "ideathon"} <= set(out)
    assert cues.expand("zzzz") == ["zzzz"]
    assert "стажир*" not in cues.group_terms("types.internship", plain_only=True)
    assert "стажир*" in cues.group_terms("types.internship")


def test_hh_settings(cues: Cues):
    assert cues.settings["hh.max_terms_per_query"] == 12


@pytest.mark.parametrize(
    "text, needle",
    [
        ('[negative]\nen = ["ai*"]', "ai*"),
        ('[negative]\nen = ["ml*"]', "ml*"),
        ('[negative]\nen = ["re:(unclosed"]', "re:(unclosed"),
        ('[bogus]\nen = ["x"]', "bogus"),
        ('[negative]\nde = ["sales"]', "de"),
        ('[types.job]\nen = "vacancy"', "types.job"),
        ('[negative]\nen = [""]', "''"),
        ('[negative]\nen = ["se*nior"]', "se*nior"),
        ('[locations.nowhere]\nen = ["x"]', "locations.nowhere"),
    ],
)
def test_load_errors_name_the_entry(text: str, needle: str):
    with pytest.raises(CueError) as exc:
        parse_cues_text(text)
    assert needle in str(exc.value)


def test_invalid_toml_is_a_cue_error():
    with pytest.raises(CueError):
        parse_cues_text("[negative")


def test_compile_entry_kinds():
    assert compile_entry("word").kind == "word"
    assert compile_entry("stem*").kind == "stem"
    assert compile_entry("re:^x$").kind == "re"


def test_nothing_else_in_src_uses_cues():
    for path in (ROOT / "src").rglob("*.py"):
        if path.name == "cues.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert "src.cues" not in text and "cues.toml" not in text, path
