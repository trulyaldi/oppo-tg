import re
from pathlib import Path

import src.sources  # noqa: F401
from src.config import ENV_VARS
from src.sources.base import REGISTRY

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()
PLAN = (ROOT / "PLAN.md").read_text()


def _section(title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", README, re.S | re.M)
    assert m, title
    return m.group(1)


def _first_cells(text: str) -> list[str]:
    cells = []
    for line in text.splitlines():
        if line.startswith("|") and not line.startswith("|---") and "`" in line.split("|")[1]:
            cells.append(line.split("|")[1].strip().strip("`"))
    return cells


def test_env_table_matches_env_vars():
    assert sorted(_first_cells(_section("Environment variables"))) == sorted(
        v.name for v in ENV_VARS
    )


def test_readme_has_section_0_commands():
    sec0 = PLAN.split("## 0.")[1].split("\n## ")[0]
    cmds = re.findall(r"`(\.venv/bin/python[^`]*|python3 -m venv[^`]*)`", sec0)
    assert cmds
    for cmd in cmds:
        for part in cmd.split(" / "):
            part = part.strip()
            if part.startswith("..."):
                part = ".venv/bin/python -m src.main " + part.removeprefix("... ")
            assert part in README, part
    assert ".venv/bin/python -m src.main probe" in README


def test_registry_names_in_sources_table():
    rows = {
        line.split("|")[1].strip()
        for line in _section("Sources").splitlines()
        if line.startswith("|") and not line.startswith("|---")
    }
    assert set(REGISTRY) <= rows
    assert {"yc", "euraxess", "kaggle"} <= rows


def test_env_example_block_in_user_actions():
    sec = _section("First run / user actions")
    blocks = re.findall(r"```\n(.*?)```", sec, re.S)
    block = next(b for b in blocks if "TELEGRAM_BOT_TOKEN=" in b)
    for v in ENV_VARS:
        assert re.search(rf"^\s*{v.name}=", block, re.M), v.name


def test_honest_verification_section():
    assert "never run against the real Telegram API" in _section("Verification status")


def test_dod_status_lists_all_spec_items():
    spec = (ROOT / "SPEC.md").read_text()
    items = re.findall(r"^- (.+)$", spec.split("## Definition of done")[1].split("\n## ")[0], re.M)
    assert len(items) == 8
    assert "### Definition of done status" in README
    sec = README.split("### Definition of done status")[1].split("\n## ")[0]
    for item in items:
        assert item.rstrip(".") in sec, item
    assert "NOT MET until the user adds the block" in sec


def test_digest_min_items_gone():
    assert "digest_min_items" not in README
    assert "digest_min_items" not in (ROOT / "config.toml").read_text()


def test_dod_status_lists_outreach_section_items():
    spec = (ROOT / "SPEC.md").read_text()
    sec = spec.split("## Definition of done: outreach and weights")[1].split("\n## ")[0]
    items = re.findall(r"^- (.+)$", sec, re.M)
    assert items
    status = README.split("### Definition of done status")[1].split("\n## ")[0]
    for item in items:
        assert item.rstrip(".") in status, item


def test_outreach_readme_mentions():
    for needle in (
        "config/weights.yaml",
        "/cv",
        "/forgetcv",
        "/cancel",
        "/prepare",
        "/contacts",
        "/addcontact",
        "/draft",
        "never fetches LinkedIn",
        "Anthropic API",
        "digest_quotas",
        "[outreach]",
    ):
        assert needle in README, needle
    assert "digest_max_items" not in README


def test_readme_says_claims_check_is_heuristic():
    from pathlib import Path

    text = " ".join((Path(__file__).parent.parent / "README.md").read_text().split())
    assert "deterministic heuristic" in text
    assert "D-161" in text
    assert "Read every draft and tailoring tip before you use it" in text


def test_readme_has_hh_section():
    sec = _section("hh.kz")
    for needle in ("dev.hh.ru", "client_credentials", "HH_ACCESS_TOKEN", "HTTP_CONTACT", "1175"):
        assert needle in sec


def test_coverage_and_freshness_readme():
    for needle in (
        "/stats",
        "/boards",
        "/channels",
        "src.evals.run --triage",
        "src.main discover",
        "data/discovered_boards.toml",
        "[scheduler]",
        "[politeness]",
        "[discovery]",
        "[triage]",
        "[scoring]",
        "hard_blocker_keywords",
        "COLLECT_CRON_HOURS` is ignored",
        "hand-written",
        "join the channels yourself",
    ):
        assert needle in " ".join(README.split()) or needle in README, needle
    for ref in ("D-174", "D-175", "D-176", "D-177", "D-180", "D-179", "D-202"):
        assert ref in README, ref
    assert "User actions (coverage and freshness)" in PLAN


def test_fix47_corrected_sentences():
    text = " ".join(README.split())
    assert "and the Workable accounts you list in `config.toml`" in text
    assert "discovered boards, Remote OK," in text
    assert "discovered Workable boards are polled without any account" in text
    assert "Greenhouse, Lever and Ashby are only active when their config list" in text
    assert '`[triage] daily_call_cap = 0` does not mean "no triage"' in text
    assert "makes every posting `capped` all day" in text
    assert 'labeled "errors (all time)"' in text
