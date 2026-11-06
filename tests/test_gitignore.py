"""Sensitive runtime files must never be committable."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

IGNORED = [
    "data/oppo.db",
    "data/oppo.db-wal",
    "data/oppo.db-shm",
    "data/run.log",
    "data/telethon.session",
    "data/telethon.session-journal",
    "other.session",
    "other.session-journal",
    "x.db-wal",
    "evals/last_run.json",
    "evals/last_triage_run.json",
    ".env",
]


@pytest.mark.skipif(shutil.which("git") is None, reason="git unavailable")
@pytest.mark.parametrize("path", IGNORED)
def test_path_is_ignored(path):
    r = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
    assert r.returncode == 0, f"{path} is not ignored"


@pytest.mark.skipif(shutil.which("git") is None, reason="git unavailable")
def test_only_gitkeep_tracked_under_data():
    out = subprocess.run(
        ["git", "ls-files", "data"], cwd=ROOT, capture_output=True, text=True
    ).stdout.split()
    assert set(out) <= {"data/.gitkeep"}
