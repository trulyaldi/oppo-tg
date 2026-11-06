import json
import re
from pathlib import Path

from src.outreach.render import FOOTER, chunk_lines, prepare_keyboard, render_prepare

FIX = Path(__file__).parent / "fixtures"
POSTING = {"id": "0123456789abcdef", "title": "Quant Research Intern", "company": "Demo Trading"}
CONTACTS = [
    {
        "id": 7,
        "contact_type": "recruiter",
        "type_confidence": 0.9,
        "name": "Ann Fake",
        "role": "Recruiter FAKE FIXTURE",
        "source_url": "https://example.com/team?a=1&b=2",
    },
    {
        "id": 8,
        "contact_type": "engineer",
        "type_confidence": 0.55,
        "name": "Bob Fake",
        "role": "",
        "source_url": "javascript:alert(1)",
    },
]


def honest() -> dict:
    return json.loads((FIX / "tailor_reply_honest.json").read_text())


def balanced(text: str) -> bool:
    stack = []
    for m in re.finditer(r"<(/?)(\w+)[^>]*>", text):
        if m.group(1):
            if not stack or stack.pop() != m.group(2):
                return False
        else:
            stack.append(m.group(2))
    return not stack


def test_exact_output():
    chunks = render_prepare(POSTING, honest(), CONTACTS, [], 0)
    assert len(chunks) == 1
    assert chunks[0] == (FIX / "prepare_expected.txt").read_text().rstrip("\n")


def test_escaping_and_removed():
    posting = {"title": "<script>&", "company": "A & B <i>"}
    out = "\n".join(render_prepare(posting, honest(), [], [], 3))
    assert "<b>Prepare: &lt;script&gt;&amp;</b> — A &amp; B &lt;i&gt;" in out
    assert "<script>" not in out
    assert "none found yet" in out
    assert "Removed 3 unsupported statements." in out
    assert out.endswith(FOOTER)


def test_removed_zero_omitted():
    assert "Removed" not in "\n".join(render_prepare(POSTING, honest(), [], [], 0))


def test_large_result_chunks():
    item = {"text": "x <b> & " * 25, "cv_evidence": "e"}
    result = {"fit": {"level": "strong", "points": [item] * 60}, "emphasize": [item] * 60}
    chunks = render_prepare(POSTING, result, CONTACTS * 30, [], 0)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= 4000
        assert balanced(c)
        assert not c.startswith("\n") and not c.endswith("\n")


def test_chunk_lines_never_splits_line():
    lines = ["a" * 30] * 10
    chunks = chunk_lines(lines, 100)
    assert all(len(c) <= 100 for c in chunks)
    assert "\n".join(chunks).split("\n") == lines


def test_keyboard():
    contacts = [{**CONTACTS[0], "id": i} for i in range(1, 9)]
    links = [
        ("LinkedIn recruiters", "https://www.linkedin.com/search/results/people/?keywords=x"),
        ("bad", "javascript:alert(1)"),
    ]
    kb = prepare_keyboard("0123456789abcdef", contacts, links)
    buttons = [b for row in kb.inline_keyboard for b in row]
    assert sum(1 for b in buttons if b.url) == 1
    drafts = [b for b in buttons if b.callback_data and b.callback_data.startswith("dr:")]
    assert len(drafts) == 5
    assert drafts[0].text == "Draft: recruiter #1"
    assert buttons[-1].callback_data == "ac:0123456789abcdef"
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons if b.callback_data)
