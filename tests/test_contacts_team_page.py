import re
from pathlib import Path

from src.contacts.parsers.team_page import parse_team_page

FIX = Path(__file__).parent / "fixtures/contacts"


def load(name):
    return (FIX / name).read_text("utf-8")


def pairs(got):
    return [(c.name, c.role) for c in got]


def test_schema_fixture():
    got = parse_team_page(load("team_page_schema.html"), "https://www.fake.example/team")
    assert pairs(got) == [
        ("Ada Placeholder", "Technical Recruiter"),
        ("Boris Fixture", "Head of Research"),
        ("Carol Sample", "Co-founder & CEO"),
    ]
    assert got[0].note == "listed on fake.example team page"


def test_cards_fixture():
    got = parse_team_page(load("team_page_cards.html"), "https://fake.example/about")
    assert pairs(got) == [
        ("Dana Mockson", "Engineering Manager"),
        ("Eli Testwell", "Talent Acquisition Partner"),
        ("Fay Dummy", "Research Scientist"),
        ("Hana Faker", "Founding Engineer"),
    ]


def test_organizer_fixture():
    got = parse_team_page(load("organizer_page.html"), "https://fakehack.example/")
    assert pairs(got) == [
        ("Ivan Organizerov", "Lead Organizer"),
        ("Jana Placeholder", "Sponsorship Coordinator"),
    ]
    assert all(c.note == "listed as organizer on fakehack.example" for c in got)


def test_nav_only_yields_nothing():
    html = "<nav><h3>About Us</h3><p>Engineer stories</p><h3>Contact Sales</h3><p>Manager</p></nav>"
    assert parse_team_page(html, "https://x.example/") == []


def test_garbage_and_empty():
    assert parse_team_page("<<<>>><div", "https://x.example/") == []
    assert parse_team_page("", "https://x.example/") == []
    assert parse_team_page("\x00\x01 not html", "") == []


def test_no_emails_or_digit_runs():
    for name in ("team_page_schema.html", "team_page_cards.html", "organizer_page.html"):
        for c in parse_team_page(load(name), "https://x.example/"):
            blob = f"{c.name} {c.role} {c.note}"
            assert "@" not in blob
            assert not re.search(r"\d{7,}", blob)


def test_max_20_and_dedupe():
    cards = "".join(
        f"<h3>Name{chr(97 + i)} Surname</h3><p>Engineer</p>".replace("Name", "Nam")
        for i in range(30)
    )
    # names like "Nama Surname" differ by letter; only 26 distinct, all capital-first
    got = parse_team_page(f"<div>{cards}</div>", "https://x.example/")
    assert len(got) == 20
    assert len({c.name for c in got}) == 20
