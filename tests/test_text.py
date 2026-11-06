import pytest

from src.text import html_to_text, truncate


def test_html_to_text():
    assert html_to_text("<p>a &amp; b</p><br>c") == "a & b\nc"


def test_html_strips_scripts_and_empty():
    assert html_to_text("") == ""
    assert html_to_text("<script>x()</script><div>hi</div>") == "hi"


@pytest.mark.parametrize("limit", [0, 1, 2, 5, 10, 100])
def test_truncate_never_exceeds(limit):
    assert len(truncate("hello world, this is long", limit)) <= limit


def test_truncate_short_unchanged():
    assert truncate("abc", 10) == "abc"
    assert truncate("abcdefghij", 5).endswith("…")
