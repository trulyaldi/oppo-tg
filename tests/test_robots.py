from pathlib import Path

from src import robots

FIX = Path(__file__).parent / "fixtures"


def _ok(body: str, path: str) -> bool:
    return robots.allowed(robots.parse(body), "oppo-tg", path)


def test_enbek():
    body = (FIX / "robots_enbek.txt").read_text()
    assert not _ok(body, "/ru/search/vacancy")
    assert _ok(body, "/ru/vacancy/vospitatel~6225562")
    assert not _ok(body, "/x/y.pdf")
    assert _ok(body, "/x/y.pdf?z")


def test_remotive_indented():
    body = (FIX / "robots_remotive.txt").read_text()
    assert not _ok(body, "/api/remote-jobs")


def test_own_group_denies_over_star():
    body = "User-agent: oppo-tg\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    assert not _ok(body, "/anything")
    assert robots.allowed(robots.parse(body), "other", "/anything")


def test_agent_case_insensitive():
    assert not _ok("User-agent: OPPO-TG\nDisallow: /\n", "/a")


def test_longest_wins_and_allow_tie():
    body = "User-agent: *\nDisallow: /\nAllow: /p\n"
    assert _ok(body, "/page")
    assert not _ok(body, "/other")
    assert _ok("User-agent: *\nDisallow: /a\nAllow: /a\n", "/a")


def test_empty_disallow_and_empty_body():
    assert _ok("User-agent: *\nDisallow:\n", "/x")
    assert _ok("", "/x")


def test_percent_case_normalised():
    body = "User-agent: *\nDisallow: /%d0%b2\n"
    assert not _ok(body, "/%D0%B2/x")


def test_multi_agent_group_and_comments():
    body = "User-agent: a\nUser-agent: oppo-tg # us\nDisallow: /z # c\n"
    assert not _ok(body, "/z")
