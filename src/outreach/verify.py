"""Deterministic claims-vs-CV verifier (P1, D-107, D-109).

Pure functions, no I/O, no LLM. A failing item is removed and only counted; its text is
never echoed. The rules (8.1.11): V1 evidence is a verbatim CV quote, V2 numbers appear in
the evidence, V3 inflation words appear in the evidence, V4 skill terms appear in the CV,
K keywords appear in both CV and posting, G gap advice states no invented facts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.cv.extract import norm
from src.outreach.lexicon import INFLATION_STEMS, SKILL_TERMS

MAX_TEXT = 200
MIN_EVIDENCE = 8
CAPS = {"points": 3, "emphasize": 4, "cut": 4, "reorder": 4, "keywords": 10, "gaps": 4}
GAP_UNSUPPORTED_HOW = (
    "Not found in your CV. Only claim it if true; otherwise name a related project or "
    "plan to learn it."
)

_NUM = re.compile(r"\d+(?:[.,]\d+)?%?")


def _stem_re(stem: str) -> re.Pattern[str]:
    s = re.escape(stem)
    if len(stem) >= 5 or not stem.isascii():
        return re.compile(r"(?<!\w)" + s)
    return re.compile(r"(?<!\w)" + s + r"(?:s|ing|ers?)?(?!\w)")


_STEM_RES = [(s, _stem_re(s)) for s in INFLATION_STEMS]


def _term_re(term: str) -> re.Pattern[str]:
    t = norm(term)
    tail = r"(?![\w])" if t[-1] in "+#" else r"(?![\w+#])"
    return re.compile(r"(?<![\w+#.])" + re.escape(t) + tail)


_TERM_RES = [(t, _term_re(t)) for t in SKILL_TERMS]


def has_term(term: str, text_norm: str) -> bool:
    """Whole-word match of an already-meaningful term in normalized text."""
    return bool(_term_re(term).search(text_norm))


def numbers(text: str) -> set[str]:
    return {m.replace(",", ".") for m in _NUM.findall(norm(text))}


def inflation_stems(text_norm: str) -> set[str]:
    return {s for s, rx in _STEM_RES if rx.search(text_norm)}


def skill_terms(text_norm: str, extra: list[str] | None = None) -> set[str]:
    found = {t for t, rx in _TERM_RES if rx.search(text_norm)}
    for e in extra or []:
        if len(norm(e)) >= 2 and has_term(e, text_norm):
            found.add(norm(e))
    return found


def v1(evidence: Any, source_text: str) -> bool:
    if not isinstance(evidence, str):
        return False
    n = norm(evidence)
    return len(n) >= MIN_EVIDENCE and n in norm(source_text)


def claim_ok(text: Any, evidence: Any, cv_text: str, cv_skills: list[str]) -> bool:
    """V1-V4 for one statement about the candidate."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        return False
    if not v1(evidence, cv_text):
        return False
    t, ev, cv = norm(text), norm(evidence), norm(cv_text)
    if not numbers(t) <= numbers(ev):  # V2
        return False
    if not inflation_stems(t) <= inflation_stems(ev):  # V3
        return False
    return skill_terms(t, cv_skills) <= skill_terms(cv, cv_skills)  # V4


def _skills_in(text_norm: str, cv_skills: list[str]) -> set[str]:
    return skill_terms(text_norm, cv_skills)


def _items(value: Any, cap: int) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, dict)][: cap + 50]


def verify_tailoring(
    result: dict, cv_text: str, posting_text: str, cv_skills: list[str] | None = None
) -> tuple[dict, int]:
    """Return (verified result, removed count). Never raises on malformed input."""
    skills = list(cv_skills or [])
    cv_n, post_n = norm(cv_text), norm(posting_text)
    removed = 0
    result = result if isinstance(result, dict) else {}

    def keep_claims(raw: Any, cap: int) -> list[dict]:
        nonlocal removed
        out: list[dict] = []
        for it in _items(raw, cap):
            if len(out) < cap and claim_ok(it.get("text"), it.get("cv_evidence"), cv_text, skills):
                out.append({"text": it["text"], "cv_evidence": it["cv_evidence"]})
            else:
                removed += 1
        return out

    fit_raw = result.get("fit") if isinstance(result.get("fit"), dict) else {}
    level = fit_raw.get("level")
    verified: dict = {
        "fit": {
            "level": level if level in ("strong", "partial", "weak") else "weak",
            "points": keep_claims(fit_raw.get("points"), CAPS["points"]),
        },
        "emphasize": keep_claims(result.get("emphasize"), CAPS["emphasize"]),
        "cut": keep_claims(result.get("cut"), CAPS["cut"]),
        "reorder": keep_claims(result.get("reorder"), CAPS["reorder"]),
    }

    gaps: list[dict] = []
    seen_gap: set[str] = set()

    def add_gap(g: dict) -> None:
        key = norm(g["requirement"])
        if key not in seen_gap and len(gaps) < CAPS["gaps"]:
            seen_gap.add(key)
            gaps.append(g)

    keywords: list[dict] = []
    for it in _items(result.get("keywords"), CAPS["keywords"]):
        kw = it.get("keyword")
        if not isinstance(kw, str) or not norm(kw) or len(kw) > MAX_TEXT:
            removed += 1
            continue
        in_cv, in_post = has_term(kw, cv_n), has_term(kw, post_n)
        if (
            in_cv
            and in_post
            and v1(it.get("cv_evidence"), cv_text)
            and v1(it.get("posting_evidence"), posting_text)
            and len(keywords) < CAPS["keywords"]
        ):
            keywords.append(
                {
                    "keyword": kw,
                    "cv_evidence": it["cv_evidence"],
                    "posting_evidence": it["posting_evidence"],
                }
            )
            continue
        removed += 1
        if in_post and not in_cv:
            add_gap(
                {"requirement": kw, "posting_evidence": "", "how_to_address": GAP_UNSUPPORTED_HOW}
            )
    verified["keywords"] = keywords

    corpus = cv_n + " " + post_n
    for it in _items(result.get("gaps"), CAPS["gaps"]):
        req, pe, how = (
            it.get("requirement"),
            it.get("posting_evidence", ""),
            it.get("how_to_address"),
        )
        ok = (
            isinstance(req, str)
            and norm(req)
            and len(req) <= MAX_TEXT
            and isinstance(how, str)
            and how.strip()
            and len(how) <= MAX_TEXT
            and isinstance(pe, str)
            and (pe == "" or v1(pe, posting_text))
        )
        if ok:
            h = norm(how)
            ok = (
                numbers(h) <= numbers(corpus)  # G: V2 against CV + posting
                and inflation_stems(h) <= inflation_stems(corpus)
                and _skills_in(h, skills) <= _skills_in(corpus, skills)  # G: V4
            )
        if ok:
            add_gap({"requirement": req, "posting_evidence": pe, "how_to_address": how})
        else:
            removed += 1
    verified["gaps"] = gaps
    return verified, removed


# ---- drafts (8.1.12) ----

_ABBR = {"dr", "mr", "mrs", "ms", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e", "inc", "no"}
_BOUNDARY = re.compile(r"([.!?]+[\"')\]]*)(\s+)")


def split_sentences(text: str) -> list[str]:
    text = " ".join(text.split())
    out: list[str] = []
    start = 0
    for m in _BOUNDARY.finditer(text):
        before = text[start : m.start()].split()
        last = before[-1].lower().strip("\"'([") if before else ""
        nxt = text[m.end() : m.end() + 1]
        if m.group(1).startswith("."):
            if last in _ABBR or (len(last) == 1 and last.isalpha()):
                continue
            if nxt and nxt.islower():
                continue
        out.append(text[start : m.end(1)].strip())
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return [s for s in out if s]


@dataclass
class DraftVerdict:
    ok: bool
    body: str = ""
    removed: int = 0
    unsupported: list[str] = field(default_factory=list)  # terms only, never CV text
    reason: str = ""


def verify_draft(
    draft: dict,
    cv_text: str,
    posting_text: str,
    contact_text: str = "",
    cv_skills: list[str] | None = None,
) -> DraftVerdict:
    """Sentence-level check. ok=False means regenerate (or give up); never raises."""
    skills = list(cv_skills or [])
    if not isinstance(draft, dict):
        return DraftVerdict(False, reason="shape")
    body, ask, hook = draft.get("body"), draft.get("ask"), draft.get("hook")
    if not (isinstance(body, str) and isinstance(ask, str) and isinstance(hook, dict)):
        return DraftVerdict(False, reason="shape")
    source = {"posting": posting_text, "contact": contact_text}.get(hook.get("from"))
    quote = hook.get("quote")
    if source is None or not v1(quote, source):
        return DraftVerdict(False, reason="hook")
    sents = split_sentences(body)
    if not 3 <= len(sents) <= 5:
        return DraftVerdict(False, reason="length")
    if not ask.strip() or norm(ask) not in norm(body):
        return DraftVerdict(False, reason="ask")

    cv_n = norm(cv_text)
    facts = norm(" ".join([cv_text, posting_text, contact_text]))
    quote_n = norm(quote)
    allowed_nums = numbers(facts) | numbers(
        ask
    )  # the ask is a proposal ("15 minutes"), not a claim
    claims = draft.get("claims") if isinstance(draft.get("claims"), list) else []
    removed = sum(
        1
        for c in claims
        if not (
            isinstance(c, dict) and claim_ok(c.get("text"), c.get("cv_evidence"), cv_text, skills)
        )
    )

    kept: list[str] = []
    bad: set[str] = set()
    for s in sents:
        sn = norm(s)
        miss = {n for n in numbers(sn) if n not in allowed_nums}
        miss |= inflation_stems(sn) - inflation_stems(cv_n)
        miss |= {
            t
            for t in _skills_in(sn, skills)
            if t not in _skills_in(cv_n, skills) and not has_term(t, quote_n)
        }
        if miss:
            removed += 1
            bad |= miss
        else:
            kept.append(s)
    new_body = " ".join(kept)
    if len(kept) < 3:
        return DraftVerdict(False, removed=removed, unsupported=sorted(bad), reason="too few")
    if norm(ask) not in norm(new_body):
        return DraftVerdict(False, removed=removed, unsupported=sorted(bad), reason="ask dropped")
    return DraftVerdict(True, new_body, removed, sorted(bad))
