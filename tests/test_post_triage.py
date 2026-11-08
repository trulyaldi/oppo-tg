"""Post-triage step (KF-09): unpaid by type, scam filter, repost dedupe (D-246, D-262)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from src import db
from src.clock import FixedClock
from src.config import TriageConfig
from src.cues import load_cues
from src.models import Kind, Posting
from src.pipeline.dedupe import fingerprint
from src.pipeline.post_triage import apply_post_triage, repost_key
from src.pipeline.run import score_pending

NOW = datetime(2026, 11, 8, 6, 0, tzinfo=UTC)
CLOCK = FixedClock(NOW)
CUES = load_cues()
UNPAID = "Unpaid summer school in machine learning"


def mk():
    conn = db.connect(":memory:")
    db.migrate(conn)
    return conn


def add(conn, title, *, kind=Kind.JOB, source="t", target="a", company="", desc="",
        opp=None, status="triaged", age_days=0, **cols):  # fmt: skip
    p = Posting(
        source=source, external_id=f"{target}:{title}:{company}:{age_days}", title=title,
        company=company, url="https://example.com/x", description=desc, kind=kind,
    )  # fmt: skip
    db.upsert_posting(conn, p, fingerprint(p), NOW - timedelta(days=age_days), target=target)
    sets = {"status": status, **cols}
    if opp:
        sets["opportunity_type"] = opp
        sets["type_source"] = "triage"
    with conn:
        conn.execute(
            f"UPDATE postings SET {', '.join(k + '=?' for k in sets)} WHERE id=?",
            (*sets.values(), p.id),
        )
    return p.id


def state(conn, pid):
    r = db.get_posting(conn, pid)
    return r["status"], r["filter_reason"]


def test_unpaid_summer_school_not_filtered_but_internship_is():
    conn = mk()
    a = add(conn, "Summer school", kind=Kind.JOB, desc=UNPAID, opp="summer_school")
    b = add(conn, "Summer school", kind=Kind.JOB, desc=UNPAID, opp="internship", target="b")
    assert apply_post_triage(conn, CUES, CLOCK) == {"unpaid": 1}
    assert state(conn, a) == ("triaged", None)
    assert state(conn, b) == ("filtered", "unpaid")


def test_triage_off_uses_kind_type_for_new_rows():
    conn = mk()
    hack = add(conn, "Hack Night", kind=Kind.HACKATHON, desc="Unpaid volunteer judges",
               status="new")  # fmt: skip
    job = add(conn, "Data Intern", kind=Kind.JOB, desc="This is an unpaid role", status="new")
    apply_post_triage(conn, CUES, CLOCK, include_new=True)
    assert state(conn, hack) == ("new", None)
    assert state(conn, job) == ("filtered", "unpaid")


def test_new_rows_untouched_while_triage_pending():
    conn = mk()
    job = add(conn, "Data Intern", desc="This is an unpaid role", status="new")
    assert apply_post_triage(conn, CUES, CLOCK) == {}
    assert state(conn, job) == ("new", None)


def test_scam_filtered_except_manual_and_personal_contact_ok():
    conn = mk()
    up = add(conn, "Data Intern", scam="upfront_payment")
    other = add(conn, "ML Role", scam="other", target="b")
    pc = add(conn, "AI Role", scam="personal_contact_only", target="c")
    manual = add(conn, "Quant Role", scam="upfront_payment", source="manual", target="manual")
    assert apply_post_triage(conn, CUES, CLOCK) == {"scam": 2}
    assert state(conn, up) == ("filtered", "scam")
    assert state(conn, other) == ("filtered", "scam")
    assert state(conn, pc) == ("triaged", None)
    assert state(conn, manual) == ("triaged", None)


def test_scam_item_never_reaches_scorer():
    conn = mk()
    pid = add(conn, "Data Intern", scam="upfront_payment", status="new")
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda *a, **k: 1 / 0))
    stats = asyncio.run(
        score_pending(conn, client, "m", CLOCK, 5, triage_cfg=TriageConfig(enabled=False))
    )
    assert state(conn, pid) == ("filtered", "scam")
    assert stats["scored"] == 0 and stats["errors"] == 0


def test_repost_across_channels_gives_one_duplicate():
    conn = mk()
    first = add(conn, "Data Analyst", company="Acme", target="chan1", contact_key="k1", age_days=1)
    second = add(conn, "Data Analyst", company="Acme", target="chan2", contact_key="k1")
    assert apply_post_triage(conn, CUES, CLOCK) == {"repost": 1}
    assert state(conn, first) == ("triaged", None)
    assert state(conn, second) == ("duplicate", "repost")
    assert db.get_posting(conn, first)["repost_key"] == db.get_posting(conn, second)["repost_key"]


def test_repost_uses_extracted_fields_and_manual_source():
    conn = mk()
    first = add(conn, "", company="", ex_title="Python Developer", ex_company="Kaspi",
                target="chan1", age_days=2)  # fmt: skip
    second = add(conn, "Python Developer", company="Kaspi", source="manual", target="manual")
    apply_post_triage(conn, CUES, CLOCK)
    assert state(conn, first)[0] == "triaged"
    assert state(conn, second) == ("duplicate", "repost")


def test_same_channel_old_and_no_key_never_merge():
    conn = mk()
    a = add(conn, "Data Analyst", company="Acme", target="chan1", age_days=1)
    b = add(conn, "Data Analyst", company="Acme", target="chan1")  # same target: not a repost
    old = add(conn, "ML Engineer", company="Acme", target="x", age_days=40)
    c = add(conn, "ML Engineer", company="Acme", target="y")  # original older than 30 days
    d = add(conn, "Data Scientist", target="p")  # no company, no contact
    e = add(conn, "Data Scientist", target="q")
    f = add(conn, "Data Scientist", target="r", contact_key="k")  # different keys
    apply_post_triage(conn, CUES, CLOCK)
    for pid in (a, b, old, c, d, e, f):
        assert state(conn, pid) == ("triaged", None), pid
    assert db.get_posting(conn, d)["repost_key"] is None


def test_repost_key_rules():
    assert repost_key("", "Data Scientist", None) is None
    assert repost_key("Acme", "", None) is None
    assert repost_key("ACME", "Data  Scientist!", "k") == repost_key("acme", "data scientist", "k")
    assert repost_key("Acme", "x", "k") != repost_key("Acme", "x", "j")


def test_duplicate_original_is_not_a_match():
    conn = mk()
    dup = add(conn, "Data Analyst", company="Acme", target="c1", age_days=3, status="duplicate",
              repost_key=repost_key("Acme", "Data Analyst", None))  # fmt: skip
    new = add(conn, "Data Analyst", company="Acme", target="c2")
    apply_post_triage(conn, CUES, CLOCK)
    assert state(conn, new) == ("triaged", None)
    assert state(conn, dup)[0] == "duplicate"
