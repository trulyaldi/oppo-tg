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
from src.outreach.lexicon import (
    ENTITY_STOP,
    INFLATION_STEMS,
    NUMBER_STEMS_RU,
    NUMBER_WORDS,
    SKILL_TERMS,
)

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


_NUM_TOKEN = re.compile(r"[^\W\d_]+")


def number_words(text_norm: str) -> set[str]:
    """Written-out numbers and multipliers ("five", "thousands", "tenfold", "тысяч"), FIX-31."""
    out: set[str] = set()
    for w in _NUM_TOKEN.findall(text_norm):
        if w in NUMBER_WORDS:
            out.add(NUMBER_WORDS[w])
        elif len(w) > 4 and w.endswith("fold"):
            out.add(w)
        else:
            out.update(v for stem, v in NUMBER_STEMS_RU if w.startswith(stem))
    return out


def numbers(text: str) -> set[str]:
    n = norm(text)
    return {m.replace(",", ".") for m in _NUM.findall(n)} | number_words(n)


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


# ---- entity grounding (FIX-15, D-140) ----

_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")
_CYR = re.compile(r"[Ѐ-ӿ]")


def _base(token: str) -> str:
    return re.split(r"['’]", token)[0]


def _word_re(word: str) -> re.Pattern[str]:
    w = norm(word)
    if _CYR.search(w) and len(w) >= 5:  # Russian inflection: "Яндексе" vs "Яндекс"
        # bounded tail: "маркет" must not be grounded by "маркетинг" (D-152)
        return re.compile(r"(?<!\w)" + re.escape(w[: max(4, len(w) - 2)]) + r"\w{0,3}(?!\w)")
    return re.compile(r"(?<!\w)" + re.escape(w) + r"(?!\w)")


def has_word(word: str, text_norm: str) -> bool:
    """Whole-word match (Cyrillic words by stem) of a name token in normalized text."""
    return bool(norm(word)) and bool(_word_re(word).search(text_norm))


def _is_cap(token: str) -> bool:
    b = _base(token)
    return len(b) >= 2 and b[0].isupper()


# Sentence-initial words that are ordinary words, not names, even in a first-person sentence.
_STARTER_WORDS = (
    "previously currently recently lately now today last next this that these those over under "
    "throughout between among through about regarding given based having being coming working "
    "looking building during after before while once still even just only also both either "
    "perhaps maybe finally first second third one two many most some every each another such "
    "what why how where who which whenever whether right early late overall together meanwhile "
    "honestly hopefully ideally personally generally mostly lastly currently often always never "
    "sometimes usually already soon later whatever whoever somehow somewhere hence thus therefore "
    "otherwise instead indeed clearly basically actually especially particularly specifically "
    "ever more less much few several all any none nothing everything something anything "
    "yesterday tomorrow tonight could would can will should may might must shall do does did "
    "is are was were am have has had let lets shall im ive id ill may whose whom whilst unless "
    "until upon within without across along beyond despite per via like unlike thanks "
    "interested excited curious happy glad eager keen impressed inspired"
)
_STARTERS = frozenset(_STARTER_WORDS.split())
# Sentence-initial verbs of tailoring advice and of CV-style claims ("Studied Mathematics at
# X"): never names, even when the next token is capitalized (FIX-34, D-159).
_VERB_STARTER_WORDS = (
    "highlight mention emphasize emphasise stress move put list place add drop cut shorten "
    "remove keep lead show note say feature foreground bring start open group combine merge "
    "trim reword rename reorder swap include use expand quantify tailor link pair connect "
    "describe explain frame position promote bold condense consolidate front reference "
    "name cite call point tie spotlight showcase "
    "studied built worked cleaned joined interned developed implemented wrote designed "
    "created analyzed analysed researched completed graduated trained volunteered "
    "participated contributed helped taught served"
)
_VERB_STARTERS = frozenset(_VERB_STARTER_WORDS.split())
_FIRST_PERSON = re.compile(
    r"(?<!\w)(?:i|i['’](?:m|ve|d|ll)|me|my|mine|myself|я|мне|меня|мой|моя|моё|мои|моей)(?!\w)",
    re.IGNORECASE,
)


def entities(sentence: str, extra_skills: list[str] | None = None) -> list[str]:
    """Name-like tokens of one sentence: capitalized words, acronyms, CamelCase.

    Skipped: stop words, skill terms, and a lone sentence-initial capital (an ordinary
    word), unless the sentence is first-person and the word is not a common starter, -ing/-ly
    word (D-152): "Citadel hired me last summer." then names Citadel.
    """
    first_person = bool(_FIRST_PERSON.search(sentence))
    toks = _WORD.findall(sentence)
    out: list[str] = []
    for i, tok in enumerate(toks):
        b = _base(tok)
        if not _is_cap(tok) or b.casefold() in ENTITY_STOP:
            continue
        if skill_terms(norm(b), extra_skills):
            continue
        shouty = b.isupper() or any(c.isupper() for c in b[1:])  # MIT, OpenAI
        if i == 0 and not shouty and (b.casefold() in _VERB_STARTERS | _STARTERS):
            continue
        if i == 0 and not shouty and not (len(toks) > 1 and _is_cap(toks[1])):
            low = b.casefold()
            if not first_person or low in _STARTERS or low.endswith(("ly", "ing")):
                continue
        out.append(b)
    return out


# Lowercase degree/title phrases that the capitalized-token rule cannot see (D-151).
_CRED = re.compile(
    r"(?<!\w)(?:(master)(?:['’]?s\b|\s+(?:degree|of)\b)|(bachelor)(?:['’]?s\b|\s+(?:degree|of)\b)"
    r"|(vice\s+president)|(магистр)\w*|(бакалавр)\w*|(doctorate)|(doctoral)"
    r"|(chief\s+\w+\s+officer)|(доктор\w*\s+наук)|(кандидат\w*\s+наук))"
)


def credentials(text_n: str) -> set[str]:
    return {next(g for g in m.groups() if g) for m in _CRED.finditer(text_n)}


def ungrounded_entities(
    text: str, corpus_n: str, extra_skills: list[str] | None = None
) -> list[str]:
    bad: list[str] = sorted(credentials(norm(text)) - credentials(corpus_n))
    for s in split_sentences(text) or [text]:
        for e in entities(s, extra_skills):
            if not has_word(e, corpus_n) and e not in bad:
                bad.append(e)
    return bad


_PAST = (
    r"interned|worked|studied|served|volunteered|graduated|trained|completed|spent|did|"
    r"joined|researched|taught|participated|built|developed|helped|contributed|was|were"
)
_PAST_RU = r"стажировал\w*|работал\w*|учил\w*|окончил\w*|служил\w*|занимал\w*"
_PREP = r"(?:at|with|for|from|in|under|@)"
_NOUN = (
    r"internship|intern|degree|msc|bsc|masters?|bachelors?|phd|job|role|position|studies|"
    r"coursework|experience|employment|tenure|thesis|education|summer|winter|semester|"
    r"year|years|time|stint|term|months|days"
)
_ROLE = (
    r"intern|internship|engineer|analyst|developer|researcher|employee|student|member|"
    r"fellow|trainee|apprentice|associate|assistant|contractor|consultant|volunteer|"
    r"resident|scientist|programmer|designer|alumnus|alumna"
)
_INTENT = re.compile(
    r"\b(?:to|excited|eager|thrilled|glad|happy|keen|hoping|would|could|impressed|inspired|struck|"
    r"fascinated|intrigued|drawn|interested|curious|following|follow(?:ed)?|admire[d]?|motivated)\b"
)


def _ent_pat(e: str) -> str:
    w = norm(e)
    if _CYR.search(w) and len(w) >= 5:
        return re.escape(w[: max(4, len(w) - 2)]) + r"\w{0,3}(?!\w)"
    return re.escape(w) + r"(?!\w)"


def is_past_claim(sentence: str, ents: list[str]) -> bool:
    """First-person completed-experience claim naming one of ents (not intent, not a question)."""
    sn = norm(sentence)
    if sn.endswith("?"):
        return False
    for e in ents:
        ep = _ent_pat(e)
        m = re.search(
            r"(?<!\w)i(?:'ve| have)?(?:\s+\w+){0,3}?\s+(?:" + _PAST + r")\b(?P<mid>[^.?!]{0,60}?)"
            r"(?<!\w)" + _PREP + r"\s+(?:the\s+)?(?:\w+\s+){0,2}?" + ep,
            sn,
        )
        if m and not _INTENT.search(m.group("mid")):
            return True
        if re.search(
            r"(?<!\w)я\s+(?:\w+\s+){0,2}?(?:" + _PAST_RU + r")[^.?!]{0,40}?"
            r"(?<!\w)(?:в|во|на|у|при)\s+" + ep,
            sn,
        ):
            return True
        if re.search(
            r"(?<!\w)(?:my|i\s+(?:hold|have|earned|received|got))\s+(?:\w+\s+){0,3}?"
            r"(?:" + _NOUN + r")\s+" + _PREP + r"\s+(?:the\s+)?" + ep,
            sn,
        ):
            return True
        if _present_or_copula_claim(sn, ep) or _fronted_or_hired_claim(sn, ep):
            return True
    return False


_HIRED = r"hired|employed|recruited|took\s+on|brought\s+on|onboarded"


def _fronted_or_hired_claim(sn: str, ep: str) -> bool:
    """ "At X, I built ...", "В X я работал ...", "X hired me as ..." (FIX-33, D-158)."""
    m = re.search(
        r"(?<!\w)(?:at|with|for|in)\s+(?:the\s+)?(?:\w+\s+){0,2}?"
        + ep
        + r"(?:\s+\w+){0,2}?[,]?\s+i(?:'ve|\s+have)?"
        r"(?:\s+\w+){0,2}?\s+(?:" + _PAST + r")\b(?P<rest>[^.?!]{0,40})",
        sn,
    )
    if m and not _INTENT.search(m.group("rest")):
        return True
    if re.search(
        r"(?<!\w)(?:в|во|на|у|при)\s+(?:\w+\s+){0,2}?"
        + ep
        + r"(?:\s+\w+){0,2}?[,]?\s+я\s+(?:\w+\s+){0,2}?"
        r"(?:" + _PAST_RU + r")",
        sn,
    ):
        return True
    return bool(re.search(r"(?<!\w)" + ep + r"(?:\s+\w+){0,2}?\s+(?:" + _HIRED + r")\s+me\b", sn))


def _present_or_copula_claim(sn: str, ep: str) -> bool:
    """ "I am (currently) a ... at X", "former ... X", "X was my employer" (D-152)."""
    m = re.search(
        r"(?<!\w)i(?:'m|\s+am)(?P<mid>(?:\s+\w+){0,4}?)\s+(?:at|with|for)\s+(?:the\s+)?"
        r"(?:\w+\s+){0,2}?" + ep,
        sn,
    )
    if (
        m
        and re.search(r"\b(?:" + _ROLE + r")\b", m.group("mid"))
        and not _INTENT.search(m.group("mid"))
    ):
        return True
    m = re.search(r"(?<!\w)former\b(?P<span>[^.?!]{0,40}?)(?<!\w)" + ep, sn)
    if m and not _INTENT.search(m.group("span")):
        return True
    return bool(
        re.search(
            r"(?<!\w)" + ep + r"(?:\s+\w+){0,2}?\s+(?:was|is)\s+"
            r"(?:where\s+i\b|(?:\w+\s+){0,2}?my\s+(?:\w+\s+)?"
            r"(?:employer|school|university|company|workplace|team|internship|job|college)\b)",
            sn,
        )
    )


_NOUN_2P = _NOUN + r"|work|projects?"  # "your work at X" (FIX-33)
_NOT_EXPERIENCE = re.compile(r"\b(?:candidate|applicant|fit|match|choice|prospect)\b")


def is_second_person_claim(text: str, ents: list[str]) -> bool:
    """Experience the user is told they have at a name that is not in the CV (D-152, D-158).

    "your internship at X", "your X internship", "you interned at X", "you were an intern at
    X", "X knows you from ...", "X hired you".
    """
    sn = norm(text)
    for e in ents:
        ep = _ent_pat(e)
        if re.search(
            r"(?<!\w)your\s+(?:\w+\s+){0,3}?(?:"
            + _NOUN_2P
            + r"|"
            + _ROLE
            + r")s?\s+"
            + _PREP
            + r"\s+(?:the\s+)?(?:\w+\s+){0,2}?"
            + ep,
            sn,
        ) or re.search(
            r"(?<!\w)your\s+(?:\w+\s+){0,2}?"
            + ep
            + r"(?:\s+\w+){0,2}?\s+(?:"
            + _NOUN_2P
            + r"|"
            + _ROLE
            + r")",
            sn,
        ):
            return True
        m = re.search(
            r"(?<!\w)you(?:'ve|\s+have)?(?:\s+\w+){0,3}?\s+(?:"
            + _PAST
            + r")\b(?P<mid>[^.?!]{0,60}?)"
            r"(?<!\w)" + _PREP + r"\s+(?:the\s+)?(?:\w+\s+){0,2}?" + ep,
            sn,
        )
        if m and not _INTENT.search(m.group("mid")):
            return True
        m = re.search(
            r"(?<!\w)you(?:'re|\s+are|\s+were)(?P<mid>(?:\s+\w+){0,4}?)\s+(?:at|with|for)\s+"
            r"(?:the\s+)?(?:\w+\s+){0,2}?" + ep,
            sn,
        )
        if (
            m
            and re.search(r"\b(?:" + _ROLE + r")\b", m.group("mid"))
            and not _INTENT.search(m.group("mid"))
            and not _NOT_EXPERIENCE.search(m.group("mid"))
        ):
            return True
        if re.search(
            r"(?<!\w)"
            + ep
            + r"(?:\s+\w+){0,3}?\s+(?:knows|knew|remembers|remembered|recognizes)\s+you\b",
            sn,
        ) or re.search(r"(?<!\w)" + ep + r"(?:\s+\w+){0,2}?\s+(?:" + _HIRED + r")\s+you\b", sn):
            return True
    return False


_YEAR = re.compile(r"(?:19|20)\d\d")  # a date ("Summer 2026"), not a quantity of experience
_SECOND = re.compile(r"(?<!\w)(?:you|you['’](?:ve|re|d|ll)|your|yours|yourself)(?!\w)")


def ungrounded_own_numbers(
    text_norm: str, cv_nums: set[str], exempt: set[str] = frozenset()
) -> set[str]:
    """Numbers in a sentence about the candidate that the CV does not contain (FIX-32).

    The caller has already established that the sentence speaks about the user. Years such
    as 2026 and an `exempt` set (the call length of the ask) are not quantities of experience.
    """
    return {
        n
        for n in numbers(text_norm)
        if n not in cv_nums and n not in exempt and not _YEAR.fullmatch(n)
    }


def gap_claims_posting_number(how: str, cv_nums: set[str]) -> bool:
    """A gap tip that tells the user "you have/your N ..." for an N that is not in the CV."""
    for clause in re.split(r"[;:.!?]", how):
        cn = norm(clause)
        if _SECOND.search(cn) and ungrounded_own_numbers(cn, cv_nums):
            return True
    return False


_CALL_LEN = re.compile(r"([^\W_]+)\s*-?\s*min(?:ute)?s?\b")


def call_length_numbers(ask: str) -> set[str]:
    """Numbers an ask may introduce: call length only ("15 minutes")."""
    out: set[str] = set()
    for tok in _CALL_LEN.findall(norm(ask)):
        out |= numbers(tok)  # "15 minutes", "fifteen-minute"
    return out


def claim_ok(
    text: Any, evidence: Any, cv_text: str, cv_skills: list[str], posting_text: str = ""
) -> bool:
    """V1-V5 for one statement about the candidate.

    With posting_text (fit points only, D-153) names found there are allowed, unless the
    text tells the user they have experience at that name ("your internship at X").
    """
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        return False
    if not v1(evidence, cv_text):
        return False
    t, ev, cv = norm(text), norm(evidence), norm(cv_text)
    if has_contact_info(text, cv):  # no clickable link/address in /prepare output (D-155)
        return False
    if not numbers(t) <= numbers(ev):  # V2
        return False
    if not inflation_stems(t) <= inflation_stems(ev):  # V3
        return False
    if not skill_terms(t, cv_skills) <= skill_terms(cv, cv_skills):  # V4
        return False
    bad = ungrounded_entities(text, cv, cv_skills)  # V5: names appear in the CV
    if bad and posting_text:
        post = norm(posting_text)
        if all(has_word(e, post) for e in bad):
            return not (is_second_person_claim(text, bad) or is_past_claim(text, bad))
    return not bad


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

    def keep_claims(raw: Any, cap: int, posting: str = "") -> list[dict]:
        nonlocal removed
        out: list[dict] = []
        for it in _items(raw, cap):
            if len(out) < cap and claim_ok(
                it.get("text"), it.get("cv_evidence"), cv_text, skills, posting
            ):
                out.append({"text": it["text"], "cv_evidence": it["cv_evidence"]})
            else:
                removed += 1
        return out

    fit_raw = result.get("fit") if isinstance(result.get("fit"), dict) else {}
    level = fit_raw.get("level")
    verified: dict = {
        "fit": {
            "level": level if level in ("strong", "partial", "weak") else "weak",
            "points": keep_claims(fit_raw.get("points"), CAPS["points"], posting_text),
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
            ok = not has_contact_info(how, cv_n) and (
                numbers(h) <= numbers(corpus)  # G: V2 against CV + posting
                and not gap_claims_posting_number(how, numbers(cv_n))  # FIX-32
                and inflation_stems(h) <= inflation_stems(corpus)
                and _skills_in(h, skills) <= _skills_in(corpus, skills)  # G: V4
                and not ungrounded_entities(how, corpus, skills)  # G: V5 against CV + posting
            )
            if ok:  # "your internship at <posting company>" is an invented CV fact
                only_post = [
                    e
                    for s_ in split_sentences(how) or [how]
                    for e in entities(s_, skills)
                    if not has_word(e, cv_n)
                ]
                ok = not (is_second_person_claim(how, only_post) or is_past_claim(how, only_post))
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


_URL = re.compile(  # scheme, www. or a path: always contact info
    r"(?i)(?:https?://|www\.|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}/\S)"
)
_BARE = re.compile(  # bare domain on a common TLD: skipped when the CV itself names it
    r"(?i)\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|io|co|ai|dev|app|me|kz|ru|"
    r"info|biz|xyz|site|online|link|ly|gl)\b"
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_HANDLE = re.compile(r"(?<![\w@])@\w{2,}")
_PHONE = re.compile(r"\+?\d[\d ()\-]{5,}\d")
_YEARS = re.compile(r"\d{4}\s*-\s*\d{4}")


def has_contact_info(sentence: str, cv_n: str = "") -> bool:
    """URL, email, @handle or a phone-like run of 7+ digits.

    A bare domain ("Socket.IO", "ASP.NET") is let through when the CV names it (D-153).
    """
    if _URL.search(sentence) or _EMAIL.search(sentence) or _HANDLE.search(sentence):
        return True
    for m in _BARE.finditer(sentence):
        if not (cv_n and has_term(m.group(), cv_n)):
            return True
    for m in _PHONE.finditer(sentence):
        if sum(c.isdigit() for c in m.group()) >= 7 and not _YEARS.fullmatch(m.group().strip()):
            return True
    return False


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
    # the ask is a proposal: only its call length ("15 minutes") is exempt, never "5 years"
    allowed_nums = numbers(facts) | call_length_numbers(ask)
    cv_nums, ask_nums = numbers(cv_n), call_length_numbers(ask)
    non_cv = norm(" ".join([posting_text, contact_text]))
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
        if has_contact_info(s, cv_n):
            removed += 1
            bad.add("contact-info")
            continue
        miss = {n for n in numbers(sn) if n not in allowed_nums}
        if _FIRST_PERSON.search(s):  # FIX-32: a number about "me" must come from the CV
            miss |= ungrounded_own_numbers(sn, cv_nums, ask_nums & call_length_numbers(s))
        miss |= inflation_stems(sn) - inflation_stems(cv_n)
        miss |= {
            t
            for t in _skills_in(sn, skills)
            if t not in _skills_in(cv_n, skills) and not has_term(t, quote_n)
        }
        ents = entities(s, skills)
        miss |= {e for e in ents if not has_word(e, facts)}  # invented names
        miss |= credentials(sn) - credentials(facts)  # "master's", "магистр"
        if not miss:
            only_ext = [e for e in ents if not has_word(e, cv_n) and has_word(e, non_cv)]
            if only_ext and is_past_claim(s, only_ext):  # "my internship at <posting company>"
                miss |= set(only_ext)
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
