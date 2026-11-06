"""CV text normalization and hashing (C2, 8.1.1)."""

from __future__ import annotations

import hashlib
import re
import unicodedata

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏  ﻿]")
_SPACES = re.compile(r"[ \t ]+")
_BLANKS = re.compile(r"\n{3,}")
_QUOTES = {"‘": "'", "’": "'", "“": '"', "”": '"'}
_DASHES = "‐‑‒–—−"
_BULLETS = "•●▪◦‣⁃*-"


def normalize_cv_text(text: str, max_chars: int = 30000) -> str:
    t = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    t = _CTRL.sub("", t)
    t = _SPACES.sub(" ", t)
    t = "\n".join(line.strip() for line in t.split("\n"))
    t = _BLANKS.sub("\n\n", t).strip()
    return t[:max_chars].rstrip()


def cv_sha(text: str) -> str:
    """Stable across whitespace-only differences."""
    canon = " ".join(text.split())
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def norm(text: str) -> str:
    """Matching form: NFKC, casefold, unified quotes/dashes, no bullets, collapsed spaces."""
    t = unicodedata.normalize("NFKC", text).casefold()
    for k, v in _QUOTES.items():
        t = t.replace(k, v)
    for d in _DASHES:
        t = t.replace(d, "-")
    t = re.sub(r"(^|\s)[" + re.escape(_BULLETS) + r"]+(?=\s)", r"\1", t)
    return " ".join(t.split())
