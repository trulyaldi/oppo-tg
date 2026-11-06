"""Parse pasted profile text or a pasted URL, as DATA only (N7, D-135).

A URL is stored as `source_url` and never fetched. Pasted text is untrusted: the model call
wraps it with `wrap_untrusted`, and name/role must be substrings of the pasted text.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from src.config import OutreachConfig
from src.contacts.sanitize import NAME_MAX, ROLE_MAX
from src.contacts.store import RawContact
from src.cv.extract import norm
from src.llm.budget import LlmBudget
from src.llm.json_call import call_json
from src.llm.untrusted import wrap_untrusted

PROMPT_PATH = Path(__file__).parent.parent / "outreach" / "prompts" / "extract_contact.md"
MAX_TOKENS = 400
MAX_LINES = 2
MAX_CHARS = 300
MAX_INPUT_CHARS = 6000

_LINKEDIN = re.compile(
    r"(?:https?://)?(?:[a-z0-9-]+\.)?linkedin\.com/in/[^\s<>\"'\])]+", re.IGNORECASE
)
_ANY_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_SPLIT = re.compile(r"\s+[-–—]\s+|\s*,\s+")


def extract_linkedin_url(text: str) -> str | None:
    m = _LINKEDIN.search(text or "")
    if not m:
        return None
    url = m.group(0).rstrip(".,;:!?")
    if not url.lower().startswith("http"):
        url = "https://" + url
    return url[:500]


def strip_urls(text: str) -> str:
    text = _LINKEDIN.sub(" ", text or "")
    text = _ANY_URL.sub(" ", text)
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def _plausible_name(name: str) -> bool:
    words = name.split()
    if not 2 <= len(words) <= 5 or len(name) > NAME_MAX:
        return False
    if "@" in name or any(ch.isdigit() for ch in name):
        return False
    return all(w[0].isalpha() and w[0].isupper() for w in words)


def _parse_short(line: str) -> RawContact | None:
    parts = _SPLIT.split(line.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    name, role = parts[0].strip(), parts[1].strip()
    if not _plausible_name(name) or not role or len(role) > ROLE_MAX or "@" in role:
        return None
    return RawContact(name=name, role=role)


def parse_pasted(text: str) -> tuple[str | None, RawContact | None, bool]:
    """Return (linkedin_url, contact, needs_llm). Never fetches anything."""
    url = extract_linkedin_url(text)
    body = strip_urls(text)
    if not body:
        return url, None, False
    lines = body.splitlines()
    if len(lines) > MAX_LINES or len(body) > MAX_CHARS:
        return url, None, True
    if len(lines) == 1:
        return url, _parse_short(lines[0]), False
    # Two lines: "Name" then "Role at Company" is accepted as a short form too.
    if _plausible_name(lines[0]) and "," not in lines[0]:
        return url, RawContact(name=lines[0], role=lines[1][:ROLE_MAX]), False
    return url, _parse_short(lines[0]) or _parse_short(lines[1]), False


class ContactOut(BaseModel):
    name: str = ""
    role: str = ""
    company: str = ""
    note: str = ""


def _grounded(value: str, haystack: str) -> bool:
    n = norm(value)
    return bool(n) and n in haystack


async def extract_with_llm(
    client: Any, text: str, cfg: OutreachConfig, budget: LlmBudget
) -> tuple[RawContact | None, str, str | None]:
    """Cheap-model extraction. Returns (contact, company, error_kind).

    error_kind is budget | api | invalid | rejected (name or role not in the pasted text).
    """
    body = strip_urls(text)[:MAX_INPUT_CHARS]
    system = [
        {
            "type": "text",
            "text": PROMPT_PATH.read_text(encoding="utf-8"),
            "cache_control": {"type": "ephemeral"},
        }
    ]
    res = await call_json(
        client,
        model=cfg.cheap_model,
        system_blocks=system,
        user_text=wrap_untrusted("profile", body),
        schema=ContactOut,
        max_tokens=MAX_TOKENS,
        budget=budget,
        purpose="extract_contact",
    )
    if res.error_kind:
        return None, "", res.error_kind
    out: ContactOut = res.value
    hay = norm(body)
    if not _grounded(out.name, hay) or not _plausible_name(out.name.strip()):
        return None, "", "rejected"
    if out.role.strip() and not _grounded(out.role, hay):
        return None, "", "rejected"
    company = out.company if _grounded(out.company, hay) else ""
    note = out.note if _grounded(out.note, hay) else ""
    return RawContact(name=out.name.strip(), role=out.role.strip(), note=note), company, None
