"""Reason helpers for scorer contract v2 (D-251): clipping and the generic-reason check."""

from __future__ import annotations

import re

REASON_MAX = 110
FLAG_MAX = 40
_WORD = re.compile(r"[^\W_]+", re.UNICODE)

# Words that carry no concrete information about the match. A reason with fewer than
# MIN_CONTENT_WORDS other words is generic (D-251).
_GENERIC_WORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "of",
        "for",
        "to",
        "in",
        "on",
        "at",
        "with",
        "by",
        "from",
        "as",
        "is",
        "are",
        "was",
        "be",
        "been",
        "being",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "your",
        "you",
        "yours",
        "my",
        "me",
        "i",
        "we",
        "our",
        "their",
        "they",
        "he",
        "she",
        "his",
        "her",
        "good",
        "great",
        "strong",
        "excellent",
        "solid",
        "decent",
        "nice",
        "perfect",
        "ideal",
        "interesting",
        "amazing",
        "awesome",
        "fine",
        "overall",
        "very",
        "quite",
        "really",
        "highly",
        "well",
        "fits",
        "fit",
        "match",
        "matches",
        "matching",
        "matched",
        "profile",
        "role",
        "roles",
        "opportunity",
        "opportunities",
        "candidate",
        "candidates",
        "skills",
        "skill",
        "background",
        "backgrounds",
        "relevant",
        "relevance",
        "suitable",
        "suited",
        "aligned",
        "alignment",
        "aligns",
        "position",
        "positions",
        "job",
        "jobs",
        "posting",
        "interests",
        "interest",
        "experience",
        "experienced",
        "high",
        "medium",
        "low",
        "some",
        "mostly",
        "generally",
        "seems",
        "seem",
        "looks",
        "look",
        "appears",
        "appear",
        "could",
        "would",
        "should",
        "may",
        "might",
        "can",
        "will",
        "worth",
        "site",
    ]
)
MIN_CONTENT_WORDS = 3


def one_line(text: str) -> str:
    return " ".join(str(text).split())


def clip_text(text: str, limit: int) -> str:
    """One line, at most `limit` characters, cut at a word boundary and ending with "…"."""
    s = one_line(text)
    if len(s) <= limit:
        return s
    room = limit - 1
    cut = s[:room]
    if not s[room].isspace():  # the cut landed inside a word: drop the partial word
        sp = cut.rfind(" ")
        if sp > 0:
            cut = cut[:sp]
    cut = cut.rstrip(" ,;:.-–—/(")
    return cut + "…"


def clip_reason(text: str) -> str:
    return clip_text(text, REASON_MAX)


def is_generic(reason: str) -> bool:
    """True when the reason has fewer than 3 content words after dropping generic ones."""
    words = [w.lower() for w in _WORD.findall(reason or "")]
    content = [w for w in words if w not in _GENERIC_WORDS]
    return len(content) < MIN_CONTENT_WORDS
