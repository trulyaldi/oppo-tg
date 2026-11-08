"""CV review service: one LLM call, deterministic verification, cache per CV version (D-259).

Every suggested claim is checked against the CV text with src/outreach/verify.py. An unsupported
rewrite becomes a question ("Did you ...? If yes, add it.") that names only the unsupported
tokens, never the whole claim. CV and review text are never logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.clock import Clock
from src.config import AppConfig, CvReviewConfig, OutreachConfig
from src.cues import load_cues
from src.cv import store as cv_store
from src.cv.extract import norm
from src.cv.market import MarketSummary, market_summary
from src.cv.store import current_cv
from src.llm.budget import OUTREACH_PURPOSES, LlmBudget
from src.llm.json_call import call_json
from src.llm.untrusted import wrap_untrusted
from src.outreach import verify as vf

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
PROMPT_PATH = HERE / "prompts" / "review.md"
PROFILE_PATH = HERE.parent / "scoring" / "profile.md"
PURPOSE = "cv_review"
MAX_TOKENS = 3000
EXCERPT_CHARS = 12000
CAPS = {"fit": 3, "changes": 5, "gaps": 3, "bullets": 6, "keywords": 10}
NOTE_FIELDS = ("structure", "length", "ordering", "kz_notes", "intl_notes", "formatting")
MAX_FIELD = 400
# Language and country names are CV-advice vocabulary (D-300), not claims about the candidate.
ADVICE_VOCAB = "russian kazakh english kazakhstan"


def prompt_version() -> str:
    h = hashlib.sha256(PROMPT_PATH.read_bytes() + PROFILE_PATH.read_bytes())
    return h.hexdigest()[:12]


class ReviewOut(BaseModel):
    """Loose on purpose: verification validates shape and content."""

    fit_summary: Any = Field(default_factory=list)
    changes: Any = Field(default_factory=list)
    gaps: Any = Field(default_factory=list)
    report: Any = Field(default_factory=dict)


@dataclass
class ReviewResult:
    cv_version: int
    summary: dict
    report_md: str
    removed: int
    questions: int
    cached: bool


@dataclass
class ReviewError:
    kind: str  # no_cv | budget | api | invalid


def _on_new_version(conn: sqlite3.Connection, new_version: int) -> int:
    return conn.execute("DELETE FROM cv_reviews WHERE cv_version < ?", (new_version,)).rowcount


def _on_forget(conn: sqlite3.Connection) -> int:
    return conn.execute("DELETE FROM cv_reviews").rowcount


def register_hooks() -> None:
    if _on_new_version not in cv_store.on_new_cv_version:
        cv_store.on_new_cv_version.append(_on_new_version)
    if _on_forget not in cv_store.on_forget_cv:
        cv_store.on_forget_cv.append(_on_forget)


register_hooks()

_locks: dict[int, asyncio.Lock] = {}


# ---- verification ----


def _s(value: Any, limit: int = MAX_FIELD) -> str:
    return value.strip() if isinstance(value, str) and len(value) <= limit else ""


def _unsupported(text: str, evidence_n: str, cv_n: str, skills: list[str]) -> list[str]:
    """Tokens of `text` that the evidence (normalized) does not support, deterministic order."""
    t = norm(text)
    out: list[str] = sorted(vf.numbers(t) - vf.numbers(evidence_n))
    out += sorted(vf.skill_terms(t, skills) - vf.skill_terms(evidence_n, skills))
    out += sorted(vf.inflation_stems(t) - vf.inflation_stems(evidence_n))
    out += vf.ungrounded_entities(text, cv_n, skills)
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))]  # type: ignore[func-returns-value]


def _text_ok(text: str, corpus_n: str, skills: list[str], *, numbers: bool = True) -> bool:
    """Free-text statement check against the CV (plus market labels for advice fields)."""
    if not text or vf.has_contact_info(text, corpus_n):
        return False
    t = norm(text)
    if numbers and not vf.numbers(t) <= vf.numbers(corpus_n):
        return False
    return (
        vf.inflation_stems(t) <= vf.inflation_stems(corpus_n)
        and vf.skill_terms(t, skills) <= vf.skill_terms(corpus_n, skills)
        and not vf.ungrounded_entities(text, corpus_n, skills)
    )


def _items(value: Any, cap: int) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, dict)][: cap + 20]


def _quote(tokens: list[str]) -> str:
    return ", ".join(f'"{t}"' for t in tokens[:6])


def verify_review(
    out: dict, cv_text: str, market_text: str, skills: list[str]
) -> tuple[dict, int, int]:
    """Return (verified object, removed count, questions count). Never raises."""
    cv_n = norm(cv_text)
    advice_n = f"{cv_n} {norm(market_text)} {ADVICE_VOCAB}"
    removed = questions = 0
    verified: dict = {"fit_summary": [], "changes": [], "gaps": []}

    raw_fit = out.get("fit_summary")
    for line in (raw_fit if isinstance(raw_fit, list) else [])[: CAPS["fit"] + 5]:
        text = _s(line)
        if len(verified["fit_summary"]) < CAPS["fit"] and _text_ok(text, cv_n, skills):
            verified["fit_summary"].append(text)
        else:
            removed += 1

    for it in _items(out.get("changes"), CAPS["changes"]):
        what, why = _s(it.get("what")), _s(it.get("why"))
        if len(verified["changes"]) < CAPS["changes"] and _text_ok(what, advice_n, skills):
            if why and not _text_ok(why, advice_n, skills, numbers=False):
                why = ""
            verified["changes"].append({"what": what, "why": why})
        else:
            removed += 1

    for it in _items(out.get("gaps"), CAPS["gaps"]):
        gap, note = _s(it.get("gap"), 120), _s(it.get("note"))
        mark = it.get("mark")
        if not gap or mark not in ("add_if_true", "worth_learning"):
            removed += 1
            continue
        if mark == "add_if_true" and vf.has_term(gap, cv_n):
            removed += 1  # already in the CV, nothing to add
            continue
        if len(verified["gaps"]) >= CAPS["gaps"]:
            removed += 1
            continue
        if note and not _text_ok(note, advice_n, skills, numbers=False):
            note = ""
        bucket = it.get("bucket") if it.get("bucket") in ("kz", "intl", "both") else "both"
        entry = {"gap": gap, "bucket": bucket, "mark": mark, "note": note}
        if mark == "add_if_true":
            entry["question"] = f"Do you have experience with {gap}? If yes, add it."
            questions += 1
        verified["gaps"].append(entry)

    report_raw = out.get("report") if isinstance(out.get("report"), dict) else {}
    report: dict = {}
    for name in NOTE_FIELDS:
        text = _s(report_raw.get(name))
        if text and not _text_ok(text, advice_n, skills, numbers=False):
            removed += 1
            text = ""
        report[name] = text

    bullets: list[dict] = []
    for it in _items(report_raw.get("bullets"), CAPS["bullets"]):
        orig, sugg = _s(it.get("original")), _s(it.get("suggestion"))
        if len(bullets) >= CAPS["bullets"] or not sugg:
            removed += 1
            continue
        if vf.claim_ok(sugg, orig, cv_text, skills):
            bullets.append({"original": orig, "suggestion": sugg})
            continue
        grounded = vf.v1(orig, cv_text)
        tokens = _unsupported(sugg, norm(orig) if grounded else cv_n, cv_n, skills)
        if tokens:
            bullets.append(
                {
                    "original": orig if grounded else "",
                    "question": f"Did you do something involving {_quote(tokens)}? "
                    "If yes, add it in your own words.",
                }
            )
            questions += 1
        else:
            removed += 1
    report["bullets"] = bullets

    keywords: list[str] = []
    raw_kw = report_raw.get("keywords")
    for kw in (raw_kw if isinstance(raw_kw, list) else [])[: CAPS["keywords"] + 10]:
        if (
            isinstance(kw, str)
            and norm(kw)
            and len(kw) <= 60
            and vf.has_term(kw, cv_n)
            and kw.strip() not in keywords
            and len(keywords) < CAPS["keywords"]
        ):
            keywords.append(kw.strip())
        else:
            removed += 1
    report["keywords"] = keywords
    verified["report"] = report
    return verified, removed, questions


# ---- rendering ----


def _market_lines(m: MarketSummary) -> str:
    lines = []
    for label, b in (("KZ employers", m.kz), ("International employers", m.intl)):
        if not b.enough:
            lines.append(f"{label}: not enough data ({b.n_postings} postings)")
            continue
        groups = "; ".join(f"{g} {c} ({round(s * 100)}%)" for g, c, s in b.groups)
        lines.append(f"{label} ({b.n_postings} postings): {groups}")
    return "\n".join(lines)


def render_report(v: dict, removed: int, questions: int) -> str:
    rep = v["report"]
    out: list[str] = ["# CV review", "", "## Fit"]
    out += [f"- {x}" for x in v["fit_summary"]] or ["- (nothing verified)"]
    out += ["", "## Changes"]
    for i, c in enumerate(v["changes"], 1):
        out.append(f"{i}. {c['what']}" + (f" ({c['why']})" if c["why"] else ""))
    if not v["changes"]:
        out.append("- (nothing verified)")
    out += ["", "## Gaps"]
    for g in v["gaps"]:
        tag = "[add if true]" if g["mark"] == "add_if_true" else "[worth learning]"
        line = f"- {tag} {g['question'] if g.get('question') else g['gap']}"
        out.append(line + (f" {g['note']}" if g["note"] else ""))
    if not v["gaps"]:
        out.append("- (none)")
    for title, key in (("Structure", "structure"), ("Length", "length"), ("Ordering", "ordering")):
        out += ["", f"## {title}", rep[key] or "(no note)"]
    out += ["", "## Bullets"]
    for b in rep["bullets"]:
        if b.get("question"):
            ref = f' (bullet: "{b["original"]}")' if b["original"] else ""
            out.append(f"- {b['question']}{ref}")
        else:
            out.append(f'- "{b["original"]}" -> "{b["suggestion"]}"')
    if not rep["bullets"]:
        out.append("- (none)")
    out += ["", "## Keywords", ", ".join(rep["keywords"]) or "(none found in your CV)"]
    out += [
        "",
        "## Kazakhstan applications",
        rep["kz_notes"] or "(no note)",
        "",
        "## International applications",
        rep["intl_notes"] or "(no note)",
        "",
        "## Formatting",
        rep["formatting"] or "(no note)",
        "",
        "## Verification",
        f"{removed} removed, {questions} turned into questions.",
    ]
    return "\n".join(out) + "\n"


# ---- service ----


def _cached(conn: sqlite3.Connection, version: int, pv: str) -> ReviewResult | None:
    row = conn.execute(
        "SELECT * FROM cv_reviews WHERE cv_version=? AND prompt_version=?", (version, pv)
    ).fetchone()
    if row is None:
        return None
    return ReviewResult(
        version,
        json.loads(row["summary_json"]),
        row["report_md"],
        row["removed"],
        row["questions"],
        True,
    )


async def review_cv(
    conn: sqlite3.Connection,
    client: Any,
    cfg: AppConfig,
    clock: Clock,
    *,
    refresh: bool = False,
) -> ReviewResult | ReviewError:
    cv = current_cv(conn)
    if cv is None:
        return ReviewError("no_cv")
    version, pv = cv["version"], prompt_version()
    lock = _locks.setdefault(version, asyncio.Lock())
    async with lock:
        if not refresh:
            hit = _cached(conn, version, pv)
            if hit is not None:
                return hit
        oc = OutreachConfig.from_cfg(cfg)
        budget = LlmBudget(conn, clock, oc.daily_llm_call_cap, OUTREACH_PURPOSES)
        market = market_summary(conn, load_cues(), CvReviewConfig.from_cfg(cfg), clock)
        market_text = _market_lines(market)
        cv_block = wrap_untrusted("cv", f"{cv['sections']}\n\n{cv['text'][:EXCERPT_CHARS]}")
        user_text = f"Today is {clock.now().date().isoformat()}.\n{cv_block}\n" + wrap_untrusted(
            "market", market_text
        )
        system = [
            {
                "type": "text",
                "text": PROMPT_PATH.read_text() + "\n\n" + PROFILE_PATH.read_text(),
                "cache_control": {"type": "ephemeral"},
            }
        ]
        res = await call_json(
            client,
            model=oc.tailor_model,
            system_blocks=system,
            user_text=user_text,
            schema=ReviewOut,
            max_tokens=MAX_TOKENS,
            budget=budget,
            purpose=PURPOSE,
        )
        if res.value is None:
            return ReviewError(res.error_kind or "api")
        try:
            skills = json.loads(cv["sections"]).get("skills", [])
        except (ValueError, AttributeError):
            skills = []
        skills = [x for x in skills if isinstance(x, str)]
        verified, removed, questions = verify_review(
            res.value.model_dump(), cv["text"], market_text, skills
        )
        report_md = render_report(verified, removed, questions)
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO cv_reviews (cv_version, prompt_version, model,"
                " market_json, summary_json, report_md, removed, questions, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    version,
                    pv,
                    oc.tailor_model,
                    json.dumps(asdict(market)),
                    json.dumps(verified),
                    report_md,
                    removed,
                    questions,
                    clock.now().isoformat(),
                ),
            )
        log.info(
            "cv review done cv_version=%s removed=%s questions=%s", version, removed, questions
        )
        return ReviewResult(version, verified, report_md, removed, questions, False)
