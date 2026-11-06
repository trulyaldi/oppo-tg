import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from src import db
from src.clock import FixedClock
from src.models import Kind, Posting
from src.pipeline.dedupe import fingerprint
from src.scoring.prompt import build_system_blocks
from src.scoring.scorer import Scorer

NOW = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
GOOD = '{"score": 7, "reason": "ok", "deadline": null, "red_flags": [], "tags": []}'


class FakeClient:
    def __init__(self):
        self.calls: list[dict] = []
        self.messages = self

    async def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=GOOD)])


def add(conn, name):
    p = Posting(
        source="t",
        external_id=name,
        title=name,
        company="Acme",
        url="https://example.com/" + name,
        kind=Kind.INTERNSHIP,
    )
    db.upsert_posting(conn, p, fingerprint(p), NOW)
    return p.id


def test_injection_end_to_end():
    conn = db.connect(":memory:")
    db.migrate(conn)
    likes = []
    for i in range(25):
        pid = add(conn, f"Liked{i:02d}")
        db.set_reaction(conn, pid, "like", NOW + timedelta(minutes=i))
        likes.append(pid)
    for i in range(3):
        pid = add(conn, f"Disliked{i}")
        db.set_reaction(conn, pid, "dislike", NOW + timedelta(minutes=i))
    # toggle off the newest like
    db.set_reaction(conn, likes[-1], "like", NOW + timedelta(hours=1))
    add(conn, "Pending")

    client = FakeClient()
    asyncio.run(Scorer(client, "m", conn, FixedClock(NOW), 10).score_pending(1))
    assert len(client.calls) == 1
    system = client.calls[0]["system"]
    assert len(system) == 2
    text = system[1]["text"]
    liked, disliked = text.split("## Disliked by the user")
    assert "## Liked by the user" in liked
    # 20 newest active likes: indices 04..23 (24 toggled off)
    assert {f"Liked{i:02d}" for i in range(4, 24)} == {
        w.strip("- ").split(" (")[0] for w in liked.splitlines() if w.startswith("- ")
    }
    assert "Liked24" not in text and "Liked03" not in text
    assert sorted(w for w in disliked.splitlines() if w.startswith("- ")) == [
        f"- Disliked{i} (Acme)" for i in range(3)
    ]


def test_static_block_unchanged_by_examples():
    empty = build_system_blocks({"like": [], "dislike": []})
    full = build_system_blocks(
        {"like": [{"title": "X", "company": "Y", "tags": []}], "dislike": []}
    )
    assert len(empty) == 1 and len(full) == 2
    assert empty[0] == full[0]
    assert full[0]["cache_control"] == {"type": "ephemeral"}
