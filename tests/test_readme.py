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
