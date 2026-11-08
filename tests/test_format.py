import re
from datetime import date
from pathlib import Path

from src.delivery.format import (
    MAX_ITEM_CHARS,
    Item,
    feedback_keyboard,
    render_digest_header,
    render_item,
    safe_url,
)

FIXTURES = Path(__file__).parent / "fixtures"


def make(**kw) -> Item:
    base = dict(
        posting_id="0123456789abcdef",
        title="Quant Research Intern",
        company="Jump Trading",
        url="https://example.com/jobs/123?a=1&b=2",
        score=8.5,
        reason="Strong match: ML research internship, paid, remote-friendly.",
        deadline=date(2026, 12, 1),
        red_flags=("unclear visa", "long commute"),
    )
    base.update(kw)
    return Item(**base)


def balanced(text: str) -> bool:
    stack = []
    for m in re.finditer(r"<(/?)(\w+)[^>]*>", text):
        if m.group(1):
            if not stack or stack.pop() != m.group(2):
                return False
        else:
            stack.append(m.group(2))
    return not stack


def test_render_matches_fixture():
    expected = (FIXTURES / "render_item_expected.txt").read_text(encoding="utf-8")
    assert render_item(make()) == expected


def test_no_deadline_line_or_red_flags_paragraph():
    out = render_item(make(deadline=None, red_flags=()))
    assert "Deadline" not in out and "Red flags" not in out and "until" not in out
    out = render_item(make())
    assert "Deadline:" not in out and "Red flags:" not in out
    assert out.splitlines()[0].count("⚠") == 1  # one marker only


def test_relevance_number_formatting():
    assert (
        render_item(make(base_score=7.5, score=9.0))
        .splitlines()[1]
        .startswith("Relevance 7.5/10: ")
    )
    assert render_item(make(base_score=8.0)).splitlines()[1].startswith("Relevance 8/10: ")


def test_marker_priority_and_clip():
    assert "⚠ hard blocker" in render_item(make(hard_blocker="x", marker="visa barrier"))
    assert "⚠ visa barrier" in render_item(make(marker="visa barrier"))
    long_flag = render_item(make(red_flags=("a very long red flag text indeed",)))
    line1 = long_flag.splitlines()[0]
    assert line1.endswith("⚠ a very long red flag te…")
    assert len(line1.split("⚠ ")[1]) == 24


def test_title_company_fallback_from_row():
    import sqlite3

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    row = c.execute(
        "SELECT 'id1' id, '' title, '' company, 'https://e.com/x' url, 7.0 adjusted_score,"
        " 'Python in Almaty' reason, NULL deadline, NULL llm_deadline, '[]' red_flags,"
        " 7.0 base_score, 'unknown' employer_category, '' adjustment_detail,"
        " 'Data Intern' ex_title, 'Kaspi' ex_company, '' location, 'Almaty' ex_location,"
        " '2026-12-05' ex_deadline, 'upfront_payment' scam, 1 visa_barrier, 2 contract"
    ).fetchone()
    item = Item.from_row(row)
    assert (item.title, item.company, item.city) == ("Data Intern", "Kaspi", "Almaty")
    assert item.deadline == date(2026, 12, 5) and item.marker == "possible scam"
    assert item.contract == 2


def test_escaping():
    out = render_item(make(title="A <b>&", company="X>"))
    assert "A &lt;b&gt;&amp;" in out
    assert "X&gt;" in out
    assert out.count("<") == 2  # only our own <b> and </b>
    assert balanced(out)


def test_unsafe_url_omitted():
    out = render_item(make(url="javascript:alert(1)"))
    assert len(out.splitlines()) == 2
    assert "javascript" not in out


def test_safe_url():
    assert safe_url("https://a.com/x") == "https://a.com/x"
    assert safe_url("http://a.com") == "http://a.com"
    for bad in ("javascript:alert(1)", "ftp://a.com", "https://", "", "https://a.com/ x"):
        assert safe_url(bad) is None


def test_long_item_capped_and_valid():
    out = render_item(make(reason="<i>&" * 20000, red_flags=("<u>" * 500,) * 10))
    assert len(out) <= MAX_ITEM_CHARS
    assert balanced(out)
    assert "<i>" not in out
    assert not re.search(r"&(?!amp;|lt;|gt;)", out)
    assert out.endswith("https://example.com/jobs/123?a=1&amp;b=2")


def test_long_item_alert_capped():
    out = render_item(make(reason="x" * 20000, title="T" * 5000), alert=True)
    assert len(out) <= MAX_ITEM_CHARS
    assert out.startswith("<b>ALERT</b> · ")


def test_alert_prefix():
    assert render_item(make(), alert=True).startswith("<b>ALERT</b> · <b>Quant")


def test_header():
    assert render_digest_header(date(2026, 11, 6), 7) == "<b>Daily digest 2026-11-06</b> · 7 items"


def test_keyboard():
    kb = feedback_keyboard("0123456789abcdef", {"like": True})
    assert len(kb.inline_keyboard) == 1
    row = kb.inline_keyboard[0]
    assert [b.text for b in row] == ["✓ Like", "Dislike", "Save", "Applied", "Prepare"]
    assert [b.callback_data for b in row] == [
        f"fb:{a}:0123456789abcdef" for a in ("like", "dislike", "save", "applied")
    ] + ["pr:0123456789abcdef"]
    assert all(len(b.callback_data.encode()) <= 64 for b in row)
    assert [b.text for b in feedback_keyboard("x").inline_keyboard[0]][0] == "Like"
    assert len(feedback_keyboard("x").inline_keyboard[0]) == 5


def flagged(**kw) -> Item:
    base = dict(
        posting_id="0123456789abcdef",
        title="Backend Intern",
        company="Arbuz.kz",
        url="https://example.com/jobs/9",
        score=6.5,
        reason="Good fit: Python, Almaty.",
        base_score=5.5,
        employer_category="kz_startup",
        adjustment_detail="+1.0 KZ startup",
        flag="\U0001f1f0\U0001f1ff",
    )
    base.update(kw)
    return Item(**base)


def test_flagged_matches_fixture():
    expected = (FIXTURES / "render_item_flagged_expected.txt").read_text(encoding="utf-8")
    assert render_item(flagged()) == expected


def test_intl_flag_and_unknown_has_no_prefix():
    assert render_item(flagged(flag="\U0001f30d")).startswith("\U0001f30d <b>Backend")
    out = render_item(flagged(flag="", adjustment_detail="", base_score=6.5))
    assert out.startswith("<b>Backend") and "(base" not in out and "+1.0" not in out


def test_flagged_alert_and_length():
    out = render_item(flagged(reason="x" * 5000), alert=True)
    assert out.startswith("<b>ALERT</b> · \U0001f1f0\U0001f1ff <b>")
    assert len(out) <= MAX_ITEM_CHARS


def test_aggregator_attribution():
    from src.delivery.format import SOURCE_ATTRIBUTION

    assert SOURCE_ATTRIBUTION == {
        "remoteok": "Remote OK",
        "jobicy": "Jobicy",
        "arbeitnow": "Arbeitnow",
    }
    out = render_item(make(source="remoteok"))
    assert out.splitlines()[-1].endswith(" · via Remote OK") and "example.com" in out
    assert out.splitlines()[-1].startswith("https://")
    assert render_item(make(source="arbeitnow")).endswith(" · via Arbeitnow")
    assert "via Jobicy" in render_item(make(source="jobicy"))
    for src in ("hn", ""):
        assert "via " not in render_item(make(source=src))


def test_alert_fixture():
    expected = (FIXTURES / "render_item_alert_expected.txt").read_text(encoding="utf-8")
    assert render_item(flagged(city="Almaty", deadline=date(2026, 12, 1)), alert=True) == expected


def test_v1_fallback_fixture_and_clip():
    long_reason = "Strong match for a quant research internship with Python and C++ " * 5
    item = make(reason=long_reason, contract=1, red_flags=(), deadline=None)
    out = render_item(item)
    expected = (FIXTURES / "render_item_v1_fallback_expected.txt").read_text(encoding="utf-8")
    assert out == expected
    reason = out.splitlines()[1].removeprefix("Relevance 8.5/10: ")
    assert len(reason) <= 110 and reason.endswith("…")
    # a contract=2 row is never re-clipped here
    assert len(render_item(make(reason=long_reason, red_flags=())).splitlines()[1]) > 150


def hostile() -> Item:
    return make(
        title="<b>T</b> & </a>" + "x" * 2000,
        company="<b>Co</b> & </a> " + "y" * 200,
        city="<b>Town</b> & </a> " + "z" * 100,
        reason="<b>why</b> & </a> because " + "w" * 3000,
        contract=1,
        red_flags=("<b>flag</b> & </a>",),
    )


def test_hostile_fixture_and_limits():
    out = render_item(hostile())
    expected = (FIXTURES / "render_item_hostile_expected.txt").read_text(encoding="utf-8")
    assert out == expected
    line1 = out.splitlines()[0]
    assert len(line1) <= 220
    assert len(out) <= MAX_ITEM_CHARS
    assert balanced(out)
    assert out.count("<b>") == 1 and "</a>" not in out
    assert "<" not in out.replace("<b>", "").replace("</b>", "")


def test_from_row_contract_1_clips_reason():
    import sqlite3

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    row = c.execute(
        "SELECT 'id1' id, 'T' title, 'C' company, 'https://e.com/x' url, 7.0 adjusted_score,"
        f" '{'old reason words ' * 30}' reason, NULL deadline, NULL llm_deadline, '[]' red_flags,"
        " 7.0 base_score, 'unknown' employer_category, '' adjustment_detail, 1 contract"
    ).fetchone()
    line2 = render_item(Item.from_row(row)).splitlines()[1]
    assert len(line2.removeprefix("Relevance 7/10: ")) <= 110
