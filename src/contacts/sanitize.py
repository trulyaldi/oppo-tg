"""Redact emails and phone numbers, strip control characters, clip fields (N1, D-103)."""

from __future__ import annotations

import re

EMAIL_MARK = "[email removed]"
PHONE_MARK = "[phone removed]"
NAME_MAX, ROLE_MAX, NOTE_MAX = 100, 150, 200

_EMAIL = re.compile(r"[\w.+\-]+\s?(?:@|\[at\]|\(at\))\s?[\w\-]+(?:\.[\w\-]+)+", re.IGNORECASE)
_PHONE = re.compile(r"(?<![\w/])\+?\(?\d[\d\s().\-]{5,}\d(?![\w])")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏ -‮⁠﻿]")


def _phone_sub(m: re.Match[str]) -> str:
    s = m.group(0)
    if _ISO_DATE.fullmatch(s.strip()):
        return s
    return PHONE_MARK if sum(c.isdigit() for c in s) >= 7 else s


def redact(text: str) -> str:
    text = _CTRL.sub(" ", text or "")
    text = _EMAIL.sub(EMAIL_MARK, text)
    text = _PHONE.sub(_phone_sub, text)
    # A long unbroken digit run (7+) never survives, whatever the separators.
    text = re.sub(r"\d{7,}", PHONE_MARK, text)
    return " ".join(text.split())


def had_contact_address(text: str) -> bool:
    return bool(_EMAIL.search(text or ""))


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def clean_field(text: str, limit: int) -> str:
    return clip(redact(text), limit)
