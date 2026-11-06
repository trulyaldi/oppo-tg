"""Wrap untrusted text in delimiter tags (D-99)."""

from __future__ import annotations

import re

RESERVED_TAGS = ("cv", "posting", "page", "profile", "contact", "examples")

_OPEN = re.compile(r"<(?=\s*/?\s*(?:" + "|".join(RESERVED_TAGS) + r")(?![A-Za-z0-9_]))", re.I)


def wrap_untrusted(tag: str, text: str) -> str:
    """Return `<tag>\\ntext\\n</tag>`; reserved tag openers/closers inside text become `‹`."""
    return f"<{tag}>\n{_OPEN.sub('‹', text)}\n</{tag}>"
