from datetime import UTC, datetime, timedelta

from src import db
from src.models import Posting
from src.pipeline.dedupe import fingerprint, mark_duplicates

NOW = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)


def p(source, ext, title, company="Jump Trading"):
    return Posting(
        source=source, external_id=ext, title=title, company=company, url="https://example.com/x"
    )


def test_fingerprint_normalizes_case_and_punctuation():
    a = fingerprint(p("a", "1", "Quant Researcher (Intern)"))
    assert a == fingerprint(p("b", "2", "quant  researcher - intern", "JUMP trading"))
    assert a == fingerprint(p("c", "3", "QUANT RESEARCHER: INTERN!", "jump-trading"))
    assert a != fingerprint(p("a", "1", "Quant Developer"))


def test_empty_company_includes_source():
    assert fingerprint(p("a", "1", "Dev", "")) != fingerprint(p("b", "1", "Dev", ""))


def test_mark_duplicates():
    conn = db.connect(":memory:")
    db.migrate(conn)
    first = p("greenhouse", "1", "Quant Intern")
    second = p("hn", "9", "quant intern!")
    other = p("hn", "10", "Quant Researcher")
    db.upsert_posting(conn, first, fingerprint(first), NOW)
    db.upsert_posting(conn, second, fingerprint(second), NOW + timedelta(hours=1))
    db.upsert_posting(conn, other, fingerprint(other), NOW + timedelta(hours=1))
    assert mark_duplicates(conn) == 1
    assert db.get_posting(conn, first.id)["status"] == "new"
    row = db.get_posting(conn, second.id)
    assert (row["status"], row["filter_reason"]) == ("duplicate", "duplicate")
    assert db.get_posting(conn, other.id)["status"] == "new"
    assert mark_duplicates(conn) == 0
