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


def test_no_deadline_and_no_flags():
    out = render_item(make(deadline=None, red_flags=()))
    assert "Deadline: no deadline" in out
    assert "Red flags" not in out


def test_escaping():
    out = render_item(make(title="A <b>&", company="X>"))
    assert "A &lt;b&gt;&amp;" in out
    assert "X&gt;" in out
    assert out.count("<") == 2  # only our own <b> and </b>
    assert balanced(out)


def test_unsafe_url_omitted():
    out = render_item(make(url="javascript:alert(1)"))
    assert out.splitlines()[-1].startswith("Red flags")
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
    assert out.startswith("<b>ALERT</b>\n")


def test_alert_prefix():
    assert render_item(make(), alert=True).startswith("<b>ALERT</b>\n<b>Quant")


def test_header():
    assert render_digest_header(date(2026, 11, 6), 7) == "<b>Daily digest 2026-11-06</b> · 7 items"


def test_keyboard():
    kb = feedback_keyboard("0123456789abcdef", {"like": True})
    assert len(kb.inline_keyboard) == 2  # P4: Prepare row once like/save is active
    assert kb.inline_keyboard[1][0].callback_data == "pr:0123456789abcdef"
    assert len(feedback_keyboard("0123456789abcdef", {"applied": True}).inline_keyboard) == 1
    row = kb.inline_keyboard[0]
    assert [b.text for b in row] == ["✓ Like", "Dislike", "Save", "Applied"]
    assert [b.callback_data for b in row] == [
        f"fb:{a}:0123456789abcdef" for a in ("like", "dislike", "save", "applied")
    ]
    assert all(len(b.callback_data.encode()) <= 64 for b in row)
    assert [b.text for b in feedback_keyboard("x").inline_keyboard[0]][0] == "Like"


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
    assert out.startswith("<b>Backend") and "(base" not in out


def test_flagged_alert_and_length():
    out = render_item(flagged(reason="x" * 5000), alert=True)
    assert out.startswith("<b>ALERT</b>\n\U0001f1f0\U0001f1ff <b>")
    assert len(out) <= MAX_ITEM_CHARS
