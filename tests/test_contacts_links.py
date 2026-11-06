from urllib.parse import parse_qs, urlsplit

from src.contacts.links import linkedin_search_urls, yc_company_url
from src.delivery.format import safe_url


def test_linkedin_urls():
    links = linkedin_search_urls("Jane Street", "Quant Researcher Intern")
    assert len(links) == 2
    for _, u in links:
        p = urlsplit(u)
        assert (p.netloc, p.path) == ("www.linkedin.com", "/search/results/people/")
        assert safe_url(u) == u
    assert "keywords=Jane%20Street%20recruiter" in links[0][1]
    assert links[1][1].endswith("keywords=Jane%20Street%20quant%20researcher")


def test_fallback_first_four_words_and_cyrillic():
    links = linkedin_search_urls("Казахтелеком", "Младший аналитик данных в отдел")
    q = parse_qs(urlsplit(links[1][1]).query)["keywords"][0]
    assert q == "Казахтелеком младший аналитик данных в"
    assert "%D0%9A" in links[0][1] and "К" not in links[0][1]
    assert safe_url(links[0][1])


def test_empty_and_yc():
    assert linkedin_search_urls("", "x") == []
    assert yc_company_url("") is None
    u = yc_company_url("Bild AI")
    assert u == "https://www.ycombinator.com/companies?query=Bild%20AI"
    assert safe_url(u)
