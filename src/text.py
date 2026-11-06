"""Text helpers."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

# Whole-word intern match: "Intern", "Internship", "Interns", never "Internal"/"International".
INTERN_RE = re.compile(r"\bintern(?:ship)?s?\b", re.IGNORECASE)

_BLOCK_TAGS = ("p", "div", "li", "ul", "ol", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section")


def html_to_text(html: str) -> str:
    """Convert HTML to plain text: one non-empty line per block/line break."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.append("\n")
    lines = (" ".join(line.split()) for line in soup.get_text().splitlines())
    return "\n".join(line for line in lines if line)


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    """Return text of at most `limit` characters, ending in `suffix` if cut."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if len(suffix) >= limit:
        return text[:limit]
    return text[: limit - len(suffix)].rstrip() + suffix
