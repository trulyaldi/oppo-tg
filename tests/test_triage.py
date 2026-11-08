import inspect
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from anthropic.resources.messages import AsyncMessages

from src import db
from src.clock import FixedClock
from src.config import ScoringConfig, TriageConfig
from src.models import Kind, Posting
from src.pipeline.dedupe import fingerprint
from src.pipeline.run import score_pending
from src.scoring.triage import (
    Triager,
    build_triage_message,
    contact_key,
    triage_prompt,
    triage_prompt_version,
)

NOW = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures"
YES = json.dumps(
    {"relevant": True, "reason": "quant intern", "type": "internship", "language": "en"}
)
NO = json.dumps({"relevant": False, "reason": "nursing", "type": "job", "language": "en"})
SCORE = json.dumps({"score": 8.0, "reason": "ok", "deadline": None, "red_flags": [], "tags": []})


class FakeClient:
    """Routes by the system prompt: triage calls get `triage` replies, scoring gets a score."""

    def __init__(self, triage=None, default=YES):
        self.triage = list(triage or [])
        self.default = default
        self.calls = []
        self.messages = self

    async def create(self, **kw):
        inspect.signature(AsyncMessages.create).bind(None, **kw)
        self.calls.append(kw)
        if "relevance filter" in kw["system"][0]["text"]:
            r = self.triage.pop(0) if self.triage else self.default
        else:
            r = SCORE
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=r)])

    @property
    def triage_calls(self):
        return [c for c in self.calls if "relevance filter" in c["system"][0]["text"]]

    @property
    def score_calls(self):
        return [c for c in self.calls if "relevance filter" not in c["system"][0]["text"]]


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    return c


def add(conn, ext, *, source="hn", baseline=False, title=None, desc="", now=NOW):
    p = Posting(
        source=source,
        external_id=ext,
        title=title or f"Quant intern {ext}",
        company="Acme",
        url=f"https://example.com/{ext}",
        description=desc,
        kind=Kind.INTERNSHIP,
    )
    out = db.upsert_posting(conn, p, fingerprint(p), now, baseline=baseline)
    return p, out


def status(conn, p):
    r = db.get_posting(conn, p.id)
    return r["status"], r["filter_reason"]


def triager(conn, client, **cfg):
    return Triager(client, conn, FixedClock(NOW), TriageConfig(**cfg))


async def test_yes_is_scored_and_no_is_filtered_without_scorer_calls(conn):
    a, _ = add(conn, "a")
    b, _ = add(conn, "b")
    client = FakeClient([YES, NO])
    stats = await score_pending(
        conn, client, "m", FixedClock(NOW), 10, triage_cfg=TriageConfig(), scoring_cfg=None
    )
    assert len(client.triage_calls) == 2
    states = {status(conn, a)[0], status(conn, b)[0]}
    assert states == {"scored", "filtered"}
    no = a if status(conn, a)[0] == "filtered" else b
    assert status(conn, no) == ("filtered", "triage_no")
    assert len(client.score_calls) == 1 and stats["scored"] == 1
    row = conn.execute("SELECT reason, relevant FROM triage_results WHERE posting_id=?", (no.id,))
    assert dict(row.fetchone()) == {"reason": "nursing", "relevant": 0}


async def test_disabled_triage_scores_new_postings_directly(conn):
    p, _ = add(conn, "a")
    client = FakeClient()
    await score_pending(
        conn, client, "m", FixedClock(NOW), 10, triage_cfg=TriageConfig(enabled=False)
    )
    assert client.triage_calls == [] and status(conn, p)[0] == "scored"


async def test_second_run_uses_the_cache_and_a_change_costs_one_call(conn):
    p, _ = add(conn, "a", desc="v1")
    client = FakeClient([YES])
    t = triager(conn, client)
    assert (await t.triage_pending())["yes"] == 1
    assert len(client.triage_calls) == 1
    assert (await t.triage_pending()) == dict.fromkeys(
        ("yes", "no", "cached", "exempt", "errors", "capped"), 0
    )
    # forced back to new with the same hash: the cached verdict answers, 0 calls
    db.set_status(conn, p.id, "new")
    assert (await t.triage_pending())["cached"] == 1
    assert len(client.triage_calls) == 1
    # the content changes: the posting is reset to new and exactly one new call is made
    p2 = replace(p, description="v2 different")
    out = db.upsert_posting(conn, p2, fingerprint(p2), NOW + timedelta(hours=1))
    assert out is db.Upsert.CHANGED and status(conn, p)[0] == "new"
    client.triage.append(NO)
    await t.triage_pending()
    assert len(client.triage_calls) == 2
    assert status(conn, p) == ("filtered", "triage_no")


async def test_resighted_unchanged_scored_posting_makes_no_calls(conn):
    p, _ = add(conn, "a")
    client = FakeClient([YES])
    await score_pending(conn, client, "m", FixedClock(NOW), 10, triage_cfg=TriageConfig())
    assert len(client.calls) == 2 and status(conn, p)[0] == "scored"
    out = db.upsert_posting(conn, p, fingerprint(p), NOW + timedelta(hours=1))
    assert out is db.Upsert.UNCHANGED
    await score_pending(conn, client, "m", FixedClock(NOW), 10, triage_cfg=TriageConfig())
    assert len(client.calls) == 2


async def test_changed_scored_posting_is_not_reset(conn):
    p, _ = add(conn, "a", desc="v1")
    await score_pending(
        conn, FakeClient([YES]), "m", FixedClock(NOW), 10, triage_cfg=TriageConfig()
    )
    p2 = replace(p, description="v2")
    assert db.upsert_posting(conn, p2, fingerprint(p2), NOW) is db.Upsert.CHANGED
    assert status(conn, p)[0] == "scored"
    client = FakeClient()
    await score_pending(conn, client, "m", FixedClock(NOW), 10, triage_cfg=TriageConfig())
    assert client.calls == []


async def test_daily_cap_holds_and_warns_once(conn):
    for i in range(5):
        add(conn, str(i))
    bot = SimpleNamespace(sent=[])

    async def send_message(chat_id, text, **kw):
        bot.sent.append(text)

    bot.send_message = send_message
    cfg = TriageConfig(daily_call_cap=2)
    client = FakeClient()
    for _ in range(2):
        stats = await score_pending(
            conn, client, "m", FixedClock(NOW), 10, bot=bot, chat_id=1, triage_cfg=cfg
        )
    assert len(client.triage_calls) == 2
    assert stats["errors"] == 0  # capped is not a failure
    warnings = [t for t in bot.sent if "Triage cap reached" in t]
    assert len(warnings) == 1
    # the next Almaty day starts a new count
    later = Triager(client, conn, FixedClock(NOW + timedelta(days=1)), cfg)
    await later.triage_pending()
    assert len(client.triage_calls) == 4


async def test_baseline_share_used_up_still_triages_new_postings(conn):
    base = [add(conn, f"b{i}", baseline=True)[0] for i in range(3)]
    fresh, _ = add(conn, "fresh")
    cfg = TriageConfig(daily_call_cap=4, baseline_max_share=0.5)  # baseline sub-cap 2
    client = FakeClient()
    stats = await triager(conn, client, daily_call_cap=4, baseline_max_share=0.5).triage_pending()
    assert cfg.baseline_cap == 2
    assert stats["capped"] == 1 and stats["yes"] == 3
    assert status(conn, fresh)[0] == "triaged"
    assert sum(status(conn, b)[0] == "new" for b in base) == 1
    # later in the day a new non-baseline posting is still triaged
    later, _ = add(conn, "later")
    await triager(conn, client, daily_call_cap=4, baseline_max_share=0.5).triage_pending()
    assert status(conn, later)[0] == "triaged"


async def test_new_postings_are_triaged_before_baseline(conn):
    add(conn, "old", baseline=True)
    add(conn, "new")
    client = FakeClient()
    await triager(conn, client, daily_call_cap=1).triage_pending()
    assert "Quant intern new" in client.triage_calls[0]["messages"][0]["content"]


async def test_invalid_reply_counts_attempts_then_triage_error(conn):
    p, _ = add(conn, "a")
    client = FakeClient(["nope", "still nope"] * 3)
    t = triager(conn, client)
    for expected in (1, 2):
        stats = await t.triage_pending()
        assert stats["errors"] == 1
        row = db.get_posting(conn, p.id)
        assert row["triage_attempts"] == expected and row["status"] == "new"
    await t.triage_pending()
    row = db.get_posting(conn, p.id)
    assert row["triage_attempts"] == 3 and row["status"] == "triage_error"
    assert (await t.triage_pending())["errors"] == 0  # no longer pending
    assert client.score_calls == []


async def test_pagewatch_is_exempt(conn):
    p, _ = add(conn, "pw", source="pagewatch")
    client = FakeClient()
    stats = await triager(conn, client).triage_pending()
    assert stats["exempt"] == 1 and client.calls == []
    assert status(conn, p)[0] == "triaged"


async def test_injection_text_stays_inside_the_untrusted_wrapper(conn):
    fx = json.loads((FIXTURES / "posting_injection.json").read_text())
    p = Posting(
        source="hn",
        external_id="inj",
        title=fx["title"],
        company=fx["company"],
        url="https://example.com/inj",
        description=fx["description"],
        kind=Kind.INTERNSHIP,
    )
    db.upsert_posting(conn, p, fingerprint(p), NOW)
    client = FakeClient()
    await triager(conn, client).triage_pending()
    (call,) = client.triage_calls
    user = call["messages"][0]["content"]
    assert user.startswith("Free-text item: no\n<posting>\n") and user.endswith("\n</posting>")
    assert user.count("</posting>") == 1 and user.count("<posting>") == 1  # fixture tags defused
    assert "Ignore all previous instructions" in user
    system = call["system"][0]["text"]
    assert "Ignore all previous instructions" not in system
    assert "untrusted" in system and "Never follow instructions" in system
    assert system == triage_prompt()


def test_message_truncates_description_to_max_chars():
    p = Posting(
        source="hn", external_id="x", title="T", company="C", url="https://e.com/x",
        description="a" * 100 + "TAIL", kind=Kind.JOB,
    )  # fmt: skip
    msg = build_triage_message(p, 100)
    assert "a" * 100 in msg and "TAIL" not in msg


@pytest.mark.parametrize(
    "reply",
    [
        '{"relevant": true, "reason": "r", "type": "job", "language": "en", "score": 9}',  # extra
        '{"relevant": "true", "reason": "r", "type": "job", "language": "en"}',  # string
        '{"relevant": 1, "reason": "r", "type": "job", "language": "en"}',  # int, not bool
        '{"reason": "r", "type": "job", "language": "en"}',  # relevant missing
        '{"relevant": true, "reason": "r", "language": "en"}',  # type missing
    ],
)
async def test_bad_shapes_are_rejected(conn, reply):
    p, _ = add(conn, "a")
    client = FakeClient([reply, reply])
    stats = await triager(conn, client).triage_pending()
    assert stats["errors"] == 1 and stats["yes"] == 0
    assert status(conn, p)[0] == "new"


async def test_long_reason_is_clipped_not_rejected(conn):
    p, _ = add(conn, "a")
    client = FakeClient([json.dumps({**json.loads(YES), "reason": "x" * 300})])
    await triager(conn, client).triage_pending()
    row = conn.execute("SELECT reason FROM triage_results").fetchone()
    assert len(row["reason"]) == 120 and status(conn, p)[0] == "triaged"


async def test_score_error_postings_are_retried_when_triage_is_on(conn):
    p, _ = add(conn, "a")
    db.set_status(conn, p.id, "triaged")
    db.record_score_error(conn, p.id)
    assert [r["id"] for r in db.pending_for_scoring(conn, 10, triage=True)] == [p.id]
    assert [r["id"] for r in db.pending_for_scoring(conn, 10, triage=False)] == [p.id]
    q, _ = add(conn, "b")  # still `new`: not pending for scoring with triage on
    assert q.id not in [r["id"] for r in db.pending_for_scoring(conn, 10, triage=True)]


def test_config_validation_and_defaults():
    c = TriageConfig()
    assert c.enabled and c.model == "claude-haiku-4-5" and c.max_desc_chars == 500
    assert c.daily_call_cap == 600 and c.baseline_cap == 300 and c.exempt_sources == ("pagewatch",)
    with pytest.raises(ValueError):
        TriageConfig.from_mapping({"nope": 1})
    with pytest.raises(ValueError):
        TriageConfig(enabled="yes")
    with pytest.raises(ValueError):
        TriageConfig(daily_call_cap=-1)
    assert ScoringConfig().daily_call_cap == 200  # unchanged


async def test_turning_triage_off_scores_already_triaged_rows(conn):
    a, _ = add(conn, "a")
    b, _ = add(conn, "b")
    client = FakeClient([YES, YES])
    await triager(conn, client).triage_pending()
    assert status(conn, a)[0] == "triaged" and status(conn, b)[0] == "triaged"
    assert len(client.triage_calls) == 2 and client.score_calls == []
    await score_pending(
        conn, client, "m", FixedClock(NOW), 10, triage_cfg=TriageConfig(enabled=False)
    )
    assert len(client.triage_calls) == 2
    assert len(client.score_calls) == 2
    assert status(conn, a)[0] == "scored" and status(conn, b)[0] == "scored"


def test_pending_for_scoring_selection_by_triage_flag(conn):
    new, _ = add(conn, "n")
    tri, _ = add(conn, "t")
    conn.execute("UPDATE postings SET status='triaged' WHERE id=?", (tri.id,))
    on = {r["id"] for r in db.pending_for_scoring(conn, 10, triage=True)}
    off = {r["id"] for r in db.pending_for_scoring(conn, 10, triage=False)}
    assert on == {tri.id}
    assert off == {new.id, tri.id}


async def test_baseline_subcap_applies_to_the_retry(conn):
    """KF-01: the correction retry may not exceed the baseline sub-cap."""
    p, _ = add(conn, "b", baseline=True)
    client = FakeClient(["nope", YES])
    t = triager(conn, client, daily_call_cap=2, baseline_max_share=0.5)  # sub-cap 1
    stats = await t.triage_pending()
    assert len(client.triage_calls) == 1
    assert stats["capped"] == 1
    assert db.get_posting(conn, p.id)["status"] == "new"


async def test_non_baseline_retry_still_allowed(conn):
    p, _ = add(conn, "n")
    client = FakeClient(["nope", YES])
    await triager(conn, client, daily_call_cap=2, baseline_max_share=0.0).triage_pending()
    assert len(client.triage_calls) == 2
    assert db.get_posting(conn, p.id)["status"] == "triaged"


def test_cap_zero_rejected_only_while_enabled():
    from src.config import AppConfig

    with pytest.raises(ValueError, match=r"\[triage\] daily_call_cap"):
        AppConfig.from_dict({"triage": {"enabled": True, "daily_call_cap": 0}})
    with pytest.raises(ValueError, match="enabled = false"):
        TriageConfig(daily_call_cap=0)
    assert TriageConfig(enabled=False, daily_call_cap=0).daily_call_cap == 0
    assert AppConfig.from_dict({"triage": {"enabled": False, "daily_call_cap": 0}})


REPLIES = json.loads((FIXTURES / "triage_replies.json").read_text(encoding="utf-8"))
RAW_CONTACT = REPLIES["ru_freetext"]["contact"]


def add_tg(conn, ext, desc="Fictional channel post"):
    p = Posting(
        source="telegram_channels",
        external_id=ext,
        title="",
        company="",
        url=f"https://example.com/{ext}",
        description=desc,
        kind=Kind.OTHER,
    )
    db.upsert_posting(conn, p, fingerprint(p), NOW)
    return p


async def test_russian_free_text_reply_sets_type_language_and_extraction(conn, caplog):
    caplog.set_level("DEBUG")
    p = add_tg(conn, "ru1", "Стажировка, пишите в личку")
    client = FakeClient([json.dumps(REPLIES["ru_freetext"])])
    await triager(conn, client).triage_pending()
    row = db.get_posting(conn, p.id)
    assert row["opportunity_type"] == "internship" and row["type_source"] == "triage"
    assert row["language"] == "ru" and row["ex_company"] == "Пример Капитал"
    assert row["ex_deadline"] == "2026-12-20" and row["scam"] == "none"
    assert row["ex_title"] == "Стажёр в quant-команду" and row["ex_location"] == "Алматы"
    assert len(row["contact_key"]) == 16 and int(row["contact_key"], 16) >= 0
    assert row["contact_key"] == contact_key("@Example_Recruiter")  # case and spaces ignored
    assert row["title"] == "" and row["description"] == "Стажировка, пишите в личку"
    tr = conn.execute("SELECT * FROM triage_results").fetchone()
    assert (tr["type"], tr["language"]) == ("internship", "ru")
    dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM postings")], default=str)
    dump += json.dumps(dict(tr), default=str)
    assert RAW_CONTACT not in dump and "example_recruiter" not in dump.casefold()
    assert RAW_CONTACT not in caplog.text and "example_recruiter" not in caplog.text.casefold()
    (call,) = client.triage_calls
    assert call["messages"][0]["content"].startswith("Free-text item: yes\n")
    assert call["max_tokens"] == 300


async def test_structured_item_ignores_extraction_keys(conn):
    p, _ = add(conn, "s")
    reply = json.dumps({**json.loads(YES), "company": "Evil", "contact": "x@y.z", "scam": "other"})
    await triager(conn, FakeClient([reply])).triage_pending()
    row = db.get_posting(conn, p.id)
    assert row["ex_company"] is None and row["contact_key"] is None and row["scam"] is None
    assert row["language"] == "en" and row["opportunity_type"] == "internship"


async def test_invalid_type_falls_back_without_retry(conn):
    p, _ = add(conn, "a")  # kind internship -> mapped internship
    client = FakeClient([json.dumps(REPLIES["bad_type"])])
    stats = await triager(conn, client).triage_pending()
    assert len(client.triage_calls) == 1 and stats["yes"] == 1
    row = db.get_posting(conn, p.id)
    assert row["opportunity_type"] == "internship" and row["type_source"] == "kind"
    assert row["language"] is None  # invalid language is dropped too


async def test_cached_verdict_reapplies_type_and_old_null_keeps_mapped(conn):
    p, _ = add(conn, "a")
    t = triager(conn, FakeClient([json.dumps({**json.loads(YES), "type": "hackathon"})]))
    await t.triage_pending()
    assert db.get_posting(conn, p.id)["opportunity_type"] == "hackathon"
    with conn:
        conn.execute("UPDATE postings SET status='new', opportunity_type='job', type_source='kind'")
    client = FakeClient()
    stats = await triager(conn, client).triage_pending()
    assert stats["cached"] == 1 and client.triage_calls == []
    row = db.get_posting(conn, p.id)
    assert (row["opportunity_type"], row["language"]) == ("hackathon", "en")
    # an old verdict (type NULL) leaves the mapped type alone
    with conn:
        conn.execute("UPDATE triage_results SET type=NULL, language=NULL")
        conn.execute("UPDATE postings SET status='new', opportunity_type='job', language=NULL")
    await triager(conn, FakeClient()).triage_pending()
    assert db.get_posting(conn, p.id)["opportunity_type"] == "job"


def test_extraction_strings_are_clipped_and_bad_values_degrade():
    from src.scoring.triage import TriageVerdict

    v = TriageVerdict.model_validate(
        {
            **json.loads(YES),
            "title": "t" * 300,
            "company": "c" * 300,
            "location": "l" * 300,
            "contact": "k" * 300,
            "deadline": "soon",
            "scam": "weird",
        }
    )
    assert [len(x) for x in (v.title, v.company, v.location, v.contact)] == [120, 120, 80, 80]
    assert v.deadline is None and v.scam == "other"


def test_prompt_version_tracks_the_v2_prompt():
    assert triage_prompt_version() != "afb9262e51c8"
    assert "Free-text item" in triage_prompt() and "kk" in triage_prompt()
