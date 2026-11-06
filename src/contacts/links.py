"""Human-assisted links (D-104): built for the user to open, never fetched by the bot."""

from __future__ import annotations

from urllib.parse import quote

from src.delivery.format import safe_url

LINKEDIN_PEOPLE = "https://www.linkedin.com/search/results/people/"
YC_COMPANIES = "https://www.ycombinator.com/companies"
TEAM_TERMS = (
    "quant researcher",
    "quant trader",
    "machine learning",
    "data scientist",
    "research scientist",
    "software engineer",
)
MAX_KEYWORD_CHARS = 120


def team_term(title: str) -> str:
    low = " ".join((title or "").lower().split())
    for term in TEAM_TERMS:
        if term in low:
            return term
    return " ".join(low.split()[:4])


def _enc(text: str) -> str:
    return quote(" ".join(text.split())[:MAX_KEYWORD_CHARS], safe="")


def linkedin_search_urls(company: str, title: str) -> list[tuple[str, str]]:
    company = " ".join((company or "").split())
    if not company:
        return []
    terms = [("LinkedIn: recruiters", "recruiter")]
    term = team_term(title)
    if term:
        terms.append((f"LinkedIn: {term}", term))
    out: list[tuple[str, str]] = []
    for label, kw in terms:
        url = safe_url(f"{LINKEDIN_PEOPLE}?keywords={_enc(f'{company} {kw}')}")
        if url:
            out.append((label, url))
    return out


def yc_company_url(company: str) -> str | None:
    company = " ".join((company or "").split())
    if not company:
        return None
    return safe_url(f"{YC_COMPANIES}?query={_enc(company)}")
