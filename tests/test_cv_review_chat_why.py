"""FIX-55: the chat summary shows "what — why" per change."""

from src.bot.outreach_handlers import review_summary_chunks
from src.cv import review as rv


def _text(changes):
    summary = {"fit_summary": ["Fit."], "changes": changes, "gaps": []}
    res = rv.ReviewResult(1, summary, "# md", 0, 0, False)
    return "\n".join(review_summary_chunks(res))


def test_chat_summary_shows_why_for_each_change():
    changes = [{"what": f"Change {i}", "why": f"Reason {i}"} for i in range(1, 5)]
    changes.append({"what": "No reason change", "why": ""})
    text = _text(changes)
    assert "1. Change 1 — Reason 1" in text and "4. Change 4 — Reason 4" in text
    assert "5. No reason change\n" in text and "5. No reason change —" not in text


def test_long_change_line_is_clipped():
    line = next(
        x for x in _text([{"what": "W" * 100, "why": "Y" * 400}]).splitlines() if x.startswith("1.")
    )
    assert len(line) <= 303 and line.endswith("…")
