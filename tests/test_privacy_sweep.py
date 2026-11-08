"""Cross-cutting privacy and safety sweep (D-111): CV -> /prepare -> /addcontact -> /draft."""

import inspect
import json
import logging
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from aiogram import Bot
from aiogram.types import Chat, Message, Update, User

from src import db
from src.bot.app import build_dispatcher
from src.bot.outreach_handlers import (
    cmd_addcontact,
    cmd_draft,
    cmd_prepare,
    ingest_cv_text,
)
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.cv import store
from src.http import build_client
from src.models import Kind, Posting
from src.outreach import draft as dr
from src.outreach import prepare as prep

ROOT = Path(__file__).parent.parent
FX = Path(__file__).parent / "fixtures"
CFG = AppConfig.load(FX / "config_repo_snapshot.toml")
NOW = datetime(2026, 11, 6, 5, tzinfo=UTC)
CLOCK = FixedClock(NOW)
SETTINGS = Settings.from_env(
    {"TELEGRAM_BOT_TOKEN": "1:t", "TELEGRAM_CHAT_ID": "42", "ANTHROPIC_API_KEY": "k"}
)
CANARIES = ("CANARY-CV", "CANARY-CONTACT", "CANARY-DRAFT", "CANARY-POSTING")
LI = "https://www.linkedin.com/in/fake-person"
BAIT = json.loads((FX / "posting_fabrication_bait.json").read_text())
CV = (
    (FX / "cv_bait_fake.txt")
    .read_text()
    .replace("SKILLS", "CANARY-CV shipped Python tooling\n\nSKILLS")
)
PARSE = json.dumps(
    {
        "education": [{"institution": "Sample Institute", "degree": "BSc Mathematics"}],
        "experience": [
            {
                "organization": "Demo Analytics",
                "role": "Data Intern",
                "bullets": ["Cleaned and joined survey datasets with Python and SQL"],
            }
        ],
        "projects": [{"name": "Weather Notebook", "bullets": ["CANARY-CV plotting"]}],
        "skills": ["Python", "SQL"],
        "other": [],
    }
)
TAILOR = (FX / "tailor_reply_honest.json").read_text()
DRAFT = json.loads((FX / "draft_reply_ok.json").read_text())
DRAFT["body"] = DRAFT["body"].replace(
    "Could you tell", "CANARY-DRAFT matters to me. Could you tell"
)
DRAFT = json.dumps(DRAFT)
TEAM = (
    (FX / "contacts/team_page_cards.html")
    .read_text()
    .replace("<body>", "<body><p>CANARY-CONTACT team page note</p>", 1)
)
PROFILE_PASTE = f"{LI}\nAlex Placeholder - Senior Recruiter at Acme Fake"


class Seq:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.messages = self

    async def create(self, **kw):
        from anthropic.resources.messages import AsyncMessages

        inspect.signature(AsyncMessages.create).bind(None, **kw)
        self.calls.append(kw)
        r = self.replies[min(len(self.calls), len(self.replies)) - 1]
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=r)])


class Msg:
    def __init__(self, text=""):
        self.text = text
        self.sent = []

    async def answer(self, text, parse_mode=None, **kw):
        self.sent.append(text)

    async def answer_document(self, document, **kw):
        self.sent.append(document.filename)


@pytest.fixture(autouse=True)
def _hooks():
    store.on_new_cv_version.clear()
    store.on_forget_cv.clear()
    prep.register_hooks()
    dr.register_hooks()
    prep._locks.clear()
    yield
    store.on_new_cv_version.clear()
    store.on_forget_cv.clear()


def porcelain():
    return subprocess.run(
        ["git", "status", "--porcelain", "--ignored", "--untracked-files=all"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout  # fmt: skip


def git_ls(*specs):
    return subprocess.run(
        ["git", "ls-files", *specs], cwd=ROOT, capture_output=True, text=True
    ).stdout.split()


async def run_flow(tmp_path):
    dbdir = tmp_path / "dbdir"
    dbdir.mkdir()
    conn = db.connect(dbdir / "oppo.db")
    db.migrate(conn)
    p = Posting(
        source="t", external_id="1", title=BAIT["title"], company="Acme Fake",
        url="https://acme-fake.test/jobs/1", kind=Kind.JOB,
        description=BAIT["description"] + " CANARY-POSTING",
    )  # fmt: skip
    db.upsert_posting(conn, p, "fp" + p.id, NOW)
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if req.url.path == "/team":
            return httpx.Response(200, text=TEAM)
        return httpx.Response(404, text="")

    http = build_client(conn, CLOCK, transport=httpx.MockTransport(handler), min_interval_s=0)
    client = Seq(PARSE, TAILOR, DRAFT)
    m = Msg()
    await ingest_cv_text(m, conn, CLOCK, CFG, client, CV)
    await cmd_prepare(m, conn, CLOCK, CFG, client, p.id, http)
    await cmd_addcontact(m, conn, CLOCK, CFG, client, f"{p.id} {PROFILE_PASTE}")
    cid = conn.execute("SELECT id FROM contacts WHERE source_url=?", (LI,)).fetchone()["id"]
    await cmd_draft(m, conn, CLOCK, CFG, client, str(cid))
    await http.aclose()
    return conn, client, m, seen, cid


async def test_full_flow_leaks_nothing(tmp_path, monkeypatch, caplog):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    before = porcelain()
    caplog.set_level(logging.DEBUG)
    conn, client, m, seen, cid = await run_flow(tmp_path)

    # the flow really ran end to end
    assert len(client.calls) == 4
    assert any(s.startswith("Draft for") for s in m.sent), m.sent
    assert conn.execute("SELECT COUNT(*) FROM outreach_drafts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] >= 2

    # no canary or stored contact/draft/CV text in logs
    for c in CANARIES:
        assert c not in caplog.text
    secrets = [r["name"] for r in conn.execute("SELECT name FROM contacts")]
    secrets += [r["body"] for r in conn.execute("SELECT body FROM outreach_drafts")]
    secrets += ["Sam Sample", "Demo Analytics", "Sample Institute", "Alex Placeholder"]
    for s in secrets:
        assert s not in caplog.text

    # canaries did reach the model as data (the sweep is not vacuous)
    blob = json.dumps(client.calls)
    assert "CANARY-CV" in blob and "CANARY-POSTING" in blob

    # no tools or tool_choice on any Anthropic call
    for kw in client.calls:
        assert "tools" not in kw and "tool_choice" not in kw

    # LinkedIn / YC hosts never requested; only the employer host was
    hosts = {r.url.host for r in seen}
    assert hosts == {"acme-fake.test"}
    assert not any("linkedin" in h or "ycombinator" in h for h in hosts)

    # nothing written outside the tmp DB and its WAL/SHM
    names = {p.name for p in (tmp_path / "dbdir").iterdir()}
    assert names <= {"oppo.db", "oppo.db-wal", "oppo.db-shm"}
    assert list(cwd.iterdir()) == []
    assert {p.name for p in tmp_path.iterdir()} == {"cwd", "dbdir"}
    assert porcelain() == before
    conn.close()


async def test_every_bot_send_targets_configured_chat(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    conn, client, _m, _seen, cid = await run_flow(tmp_path)
    calls = []

    async def fake_call(self, method, request_timeout=None):
        calls.append(method)
        return True

    monkeypatch.setattr(Bot, "__call__", fake_call)
    dp = build_dispatcher(conn, SETTINGS, CFG, CLOCK, client)
    bot = Bot("1:t")
    user = User(id=42, is_bot=False, first_name="x")
    chat = Chat(id=42, type="private")
    text = f"/draft {cid}"
    ents = [{"type": "bot_command", "offset": 0, "length": 6}]
    msg = Message(message_id=1, date=NOW, chat=chat, from_user=user, text=text, entities=ents)
    await dp.feed_update(bot, Update(update_id=1, message=msg))
    await bot.session.close()
    sends = [c for c in calls if type(c).__name__ == "SendMessage"]
    assert sends and all(str(s.chat_id) == "42" for s in sends)
    for c in CANARIES:
        assert c not in caplog.text
    conn.close()


# Canned model replies (structured fields, no free CV document) need no marker (D-138, D-301).
CANNED_MODEL_REPLIES = {
    "tests/fixtures/cv_parse_reply.json",
    "tests/fixtures/cv_review_reply_ok.json",
    "tests/fixtures/cv_review_reply_fabricated.json",
}


def test_no_data_or_cv_files_tracked_and_fixtures_marked():
    assert [f for f in git_ls("data") if f != "data/.gitkeep"] == []
    assert git_ls("*.db", "*.db-wal", "*.db-shm", "*.session") == []
    marked = [
        f for f in git_ls("tests/fixtures")
        if re.match(r"tests/fixtures/(cv_[^/]*|contacts/.*)$", f)
    ]  # fmt: skip
    assert marked  # the glob matched something
    # cv_parse_reply.json is a canned model reply (structured fields, no free CV document),
    # so the marker applies to CV documents and contact fixtures (D-138).
    missing = [
        f for f in marked
        if f not in CANNED_MODEL_REPLIES
        and b"FAKE FIXTURE" not in (ROOT / f).read_bytes()
    ]  # fmt: skip
    assert missing == []


def test_gitignore_covers_runtime_data():
    text = (ROOT / ".gitignore").read_text()
    assert "data/*" in text.splitlines() and "!data/.gitkeep" in text.splitlines()
    for p in ["data/oppo.db", "data/cv.txt", "data/cv.pdf", "data/anything/x"]:
        r = subprocess.run(["git", "check-ignore", "-q", p], cwd=ROOT)
        assert r.returncode == 0, p


def test_outreach_code_has_no_send_path():
    files = [
        *(ROOT / "src/outreach").rglob("*.py"),
        *(ROOT / "src/contacts").rglob("*.py"),
        *(ROOT / "src/cv").rglob("*.py"),
        ROOT / "src/bot/outreach_handlers.py",
    ]
    bad = re.compile(
        r"smtplib|send_mail|aiosmtplib|telethon|sendmail|\.send_message\(|"
        r"\.send_document\(|\.forward_message\(|email\.mime|sendgrid|mailgun",
        re.I,
    )
    for f in files:
        assert not bad.search(f.read_text()), f
