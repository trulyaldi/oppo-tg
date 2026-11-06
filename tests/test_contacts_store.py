import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src import db
from src.contacts.sanitize import redact
from src.contacts.store import (
    RawContact,
    add_contact,
    contacts_for,
    get_contact,
    prune_contacts,
)

TZ = timezone(timedelta(hours=5))
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=TZ)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    c.execute(
        "INSERT INTO postings (id, source, external_id, title, url, fingerprint, status,"
        " first_seen_at, last_seen_at) VALUES ('p1','t','1','T','https://x.example','f','scored',?,?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    c.commit()
    return c


def _add(conn, name="Jane Roe", **kw):
    kw.setdefault("source_kind", "posting_text")
    kw.setdefault("now", NOW)
    return add_contact(conn, "p1", RawContact(name, kw.pop("role", "Recruiter")), **kw)


def test_redacts_email_and_phone(conn):
    cid = add_contact(
        conn,
        "p1",
        RawContact(
            "Jane Roe, jane@x.io, +7 701 123 4567", "Recruiter 87011234567", "mail a.b@c.kz"
        ),
        source_kind="posting_text",
        now=NOW,
    )
    row = get_contact(conn, cid)
    for col in ("name", "role", "note", "company"):
        assert "@" not in row[col]
        assert len(re.sub(r"[^\d]", "", row[col])) < 7
    assert "[email removed]" in row["name"] and "[phone removed]" in row["name"]


def test_redact_keeps_dates_and_short_numbers():
    assert redact("Deadline 2026-11-06, team of 12") == "Deadline 2026-11-06, team of 12"
    assert redact("call (701) 123-45-67 now") == "call [phone removed] now"
    assert redact("a\x00b‮c") == "a b c"


def test_clips_fields(conn):
    cid = add_contact(
        conn,
        "p1",
        RawContact("N" * 300, "R" * 300, "x" * 500),
        source_kind="pasted",
        now=NOW,
    )
    r = get_contact(conn, cid)
    assert len(r["name"]) <= 100 and len(r["role"]) <= 150 and len(r["note"]) <= 200


def test_url_only_http(conn):
    a = _add(conn, "A", source_url="javascript:alert(1)")
    b = _add(conn, "B", source_url="https://x.example/team")
    assert get_contact(conn, a)["source_url"] == ""
    assert get_contact(conn, b)["source_url"] == "https://x.example/team"


def test_type_computed_by_code(conn):
    cid = _add(conn, role="Technical Recruiter")
    r = get_contact(conn, cid)
    assert (r["contact_type"], r["type_confidence"]) == ("recruiter", 0.9)


def test_expiry_no_deadline_60_days(conn):
    r = get_contact(conn, _add(conn))
    assert datetime.fromisoformat(r["expires_at"]) == NOW + timedelta(days=60)


def test_expiry_deadline_plus_one_when_sooner(conn):
    conn.execute("UPDATE postings SET deadline='2026-11-20' WHERE id='p1'")
    r = get_contact(conn, _add(conn))
    assert datetime.fromisoformat(r["expires_at"]) == datetime(2026, 11, 21, tzinfo=TZ)


def test_expiry_uses_llm_deadline_and_ignores_far_one(conn):
    conn.execute(
        "INSERT INTO scores (posting_id, base_score, adjusted_score, reason, deadline,"
        " model, prompt_version, scored_at) VALUES ('p1',5,5,'r','2027-06-01','m','v',?)",
        (NOW.isoformat(),),
    )
    r = get_contact(conn, _add(conn))
    assert datetime.fromisoformat(r["expires_at"]) == NOW + timedelta(days=60)


def test_cap_and_duplicates(conn):
    assert _add(conn, "Dup", source_url="https://x.example/t") is not None
    assert _add(conn, "Dup", source_url="https://x.example/t") is None
    for i in range(9):
        assert _add(conn, f"P{i}") is not None
    assert _add(conn, "Eleventh") is None
    assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 10
    assert _add(conn, "Capped", max_per_posting=3) is None


def test_empty_name_ignored(conn):
    assert _add(conn, "  \x00 ") is None


def test_contacts_for_unexpired_only_and_prune(conn):
    keep = _add(conn, "Keep")
    gone = _add(conn, "Gone", ttl_days=1)
    later = NOW + timedelta(days=2)
    assert {r["id"] for r in contacts_for(conn, "p1", NOW)} == {keep, gone}
    assert {r["id"] for r in contacts_for(conn, "p1", later)} == {keep}
    assert prune_contacts(conn, later) == 1
    assert get_contact(conn, gone) is None and get_contact(conn, keep) is not None


def test_db_prune_prunes_contacts(conn):
    _add(conn, ttl_days=1)
    res = db.prune(conn, NOW + timedelta(days=3))
    assert res["contacts"] == 1


def test_posting_delete_cascades(conn):
    _add(conn)
    conn.execute("DELETE FROM postings WHERE id='p1'")
    assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 0


def test_no_names_in_logs(conn, caplog):
    caplog.set_level(logging.DEBUG)
    _add(conn, "Zebediah Quux", ttl_days=1)
    prune_contacts(conn, NOW + timedelta(days=3))
    assert "Zebediah" not in caplog.text and "Quux" not in caplog.text


def test_migration_v5_to_v6_keeps_rows(tmp_path):
    path = tmp_path / "v5.db"
    c = db.connect(path)
    c.executescript(db.SCHEMA_1)
    c.commit()
    c.close()
    raw = sqlite3.connect(str(path))
    raw.execute("PRAGMA user_version=1")
    raw.commit()
    raw.close()
    c = db.connect(path)
    db.migrate(c)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 6
    c.execute(
        "INSERT INTO postings (id, source, external_id, title, url, fingerprint, status,"
        " first_seen_at, last_seen_at) VALUES ('p9','t','9','T','https://x.example','f9','new',?,?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    c.commit()
    c.close()
    c = db.connect(path)
    c.execute("DROP TABLE contacts")
    c.execute("PRAGMA user_version=5")
    c.commit()
    db.migrate(c)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 6
    assert c.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 0
