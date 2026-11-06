import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from src import db as dbm
from src.clock import FixedClock
from src.config import AppConfig
from src.discovery import store
from src.discovery.boards import run_board_discovery
from src.http import build_client

FIX = Path(__file__).parent / "fixtures"
GH_META = json.dumps({"name": "Acme Corp", "content": "x"})
LEVER = (FIX / "lever_postings.json").read_text()
ASHBY = (FIX / "ashby_board.json").read_text()
T0 = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)


@asynccontextmanager
async def nolock():
    yield


def setup(tmp_path, responses, discovery=None, config_boards=None):
    cfg = AppConfig.from_dict(
        {
            "sources": {
                "greenhouse": {"enabled": True, "boards": config_boards or ["imc"]},
                "lever": {"enabled": True, "companies": ["levco"]},
                "ashby": {"enabled": True, "boards": ["ashco"]},
            },
            "discovery": {"boards_file": str(tmp_path / "boards.toml"), **(discovery or {})},
        }
    )
    conn = dbm.connect(":memory:")
    dbm.migrate(conn)
    clock = FixedClock(T0)
    seen = []

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        seen.append(req.url)
        for key, resp in responses.items():
            if key in str(req.url):
                return httpx.Response(resp[0], text=resp[1])
        return httpx.Response(404, text="{}")

    async def nosleep(_):
        pass

    http = build_client(conn, clock, transport=httpx.MockTransport(handler), sleep=nosleep)
    return conn, http, cfg, clock, seen


def add_posting(conn, n, url="https://x.example/job", desc=""):
    conn.execute(
        "INSERT INTO postings (id, source, external_id, title, url, description, fingerprint,"
        " status, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"p{n}", "hn", str(n), "T", url, desc, f"fp{n}", "new", "t", "t"),
    )
    conn.commit()


def write_corrupt(path):
    Path(path).write_text("not [valid toml")


async def run(conn, http, cfg, clock):
    return await run_board_discovery(conn, http, cfg, clock, lock_factory=nolock)


async def test_valid_candidate_added_with_company_and_origin(tmp_path):
    conn, http, cfg, clock, seen = setup(tmp_path, {"boards/newco": (200, GH_META)})
    add_posting(conn, 1, "https://boards.greenhouse.io/NewCo/jobs/1")
    stats = await run(conn, http, cfg, clock)
    assert stats.added == 1
    entries = store.load(cfg.discovery.boards_file)
    assert [(e.source, e.token, e.company) for e in entries] == [
        ("greenhouse", "newco", "Acme Corp")
    ]
    row = conn.execute("SELECT * FROM board_candidates").fetchone()
    assert row["status"] == "valid" and row["discovered_from"] == "posting:p1"
    assert [str(u) for u in seen] == ["https://boards-api.greenhouse.io/v1/boards/newco"]


async def test_lever_and_ashby_validators(tmp_path):
    conn, http, cfg, clock, seen = setup(
        tmp_path, {"postings/newlev": (200, LEVER), "job-board/newash": (200, ASHBY)}
    )
    add_posting(conn, 1, "https://jobs.lever.co/newlev", "see https://jobs.ashbyhq.com/newash")
    stats = await run(conn, http, cfg, clock)
    assert stats.added == 2
    assert any("mode=json" in str(u) and "limit=1" in str(u) for u in seen)


async def test_404_cached_invalid_and_not_retried_for_30_days(tmp_path):
    conn, http, cfg, clock, seen = setup(tmp_path, {})
    add_posting(conn, 1, "https://jobs.lever.co/ghost")
    await run(conn, http, cfg, clock)
    assert conn.execute("SELECT status FROM board_candidates").fetchone()[0] == "invalid"
    n = len(seen)
    dbm.set_state(conn, "discovery:last_rowid", "0")
    clock.set(T0 + timedelta(days=29))
    await run(conn, http, cfg, clock)
    assert len(seen) == n
    dbm.set_state(conn, "discovery:last_rowid", "0")
    clock.set(T0 + timedelta(days=31))
    await run(conn, http, cfg, clock)
    assert len(seen) == n + 1


async def test_transient_error_not_cached_as_invalid(tmp_path):
    conn, http, cfg, clock, seen = setup(tmp_path, {"newlev": (503, "x")})
    add_posting(conn, 1, "https://jobs.lever.co/newlev")
    stats = await run(conn, http, cfg, clock)
    assert stats.errors == 1 and stats.invalid == 0
    assert conn.execute("SELECT status FROM board_candidates").fetchone()[0] == "error"
    dbm.set_state(conn, "discovery:last_rowid", "0")
    clock.set(T0 + timedelta(days=1))
    await run(conn, http, cfg, clock)
    assert (
        conn.execute("SELECT COUNT(*) FROM board_candidates WHERE status='error'").fetchone()[0]
        == 1
    )


async def test_caps_hold_across_runs_and_reset_next_day(tmp_path):
    conn, http, cfg, clock, seen = setup(
        tmp_path,
        {"postings/": (200, LEVER)},
        discovery={"max_validations_per_day": 3, "max_new_boards_per_day": 2},
    )
    for i in range(6):
        add_posting(conn, i, f"https://jobs.lever.co/co{i}")
    s1 = await run(conn, http, cfg, clock)
    assert s1.added == 2 and s1.validated == 2
    s2 = await run(conn, http, cfg, clock)
    assert s2.validated == 0
    assert len(store.load(cfg.discovery.boards_file)) == 2
    clock.set(T0 + timedelta(days=1))
    s3 = await run(conn, http, cfg, clock)
    assert s3.added == 2
    assert len(store.load(cfg.discovery.boards_file)) == 4


async def test_validation_cap_counts_invalid_too(tmp_path):
    conn, http, cfg, clock, seen = setup(tmp_path, {}, discovery={"max_validations_per_day": 2})
    for i in range(5):
        add_posting(conn, i, f"https://jobs.lever.co/co{i}")
    assert (await run(conn, http, cfg, clock)).validated == 2
    assert (await run(conn, http, cfg, clock)).validated == 0
    clock.set(T0 + timedelta(days=1))
    assert (await run(conn, http, cfg, clock)).validated == 2


async def test_config_and_tombstoned_never_validated(tmp_path):
    conn, http, cfg, clock, seen = setup(tmp_path, {"": (200, LEVER)})
    store.tombstone(cfg.discovery.boards_file, "lever", "dead")
    add_posting(
        conn,
        1,
        "https://jobs.lever.co/LevCo",
        "https://jobs.lever.co/dead https://boards.greenhouse.io/IMC",
    )
    stats = await run(conn, http, cfg, clock)
    assert stats.validated == 0 and seen == []


async def test_only_canonical_hosts_and_unsafe_urls_never_requested(tmp_path):
    conn, http, cfg, clock, seen = setup(tmp_path, {"": (200, LEVER)})
    add_posting(
        conn,
        1,
        "https://linkedin.com/jobs/1",
        "http://127.0.0.1/x https://jobs.lever.co.evil.com/x https://evil.com/jobs.lever.co/y"
        " https://jobs.lever.co/okboard",
    )
    await run(conn, http, cfg, clock)
    assert {u.host for u in seen} == {"api.lever.co"}
    assert [u.path for u in seen] == ["/v0/postings/okboard"]


async def test_unregistered_or_disabled_ats_skipped_not_cached(tmp_path):
    conn, http, cfg, clock, seen = setup(tmp_path, {})
    add_posting(conn, 1, "https://apply.workable.com/acme/j/1")
    stats = await run(conn, http, cfg, clock)
    assert seen == [] and stats.skipped == 1
    assert conn.execute("SELECT COUNT(*) FROM board_candidates").fetchone()[0] == 0


async def test_cursor_advances_and_only_new_postings_scanned(tmp_path):
    conn, http, cfg, clock, seen = setup(tmp_path, {"postings/": (200, LEVER)})
    add_posting(conn, 1, "https://jobs.lever.co/aa")
    await run(conn, http, cfg, clock)
    assert dbm.get_state(conn, "discovery:last_rowid") == "1"
    add_posting(conn, 2, "https://jobs.lever.co/bb")
    s = await run(conn, http, cfg, clock)
    assert s.scanned == 1 and s.added == 1


async def test_corrupt_boards_file_adds_nothing(tmp_path):
    conn, http, cfg, clock, seen = setup(tmp_path, {"postings/": (200, LEVER)})
    write_corrupt(cfg.discovery.boards_file)
    add_posting(conn, 1, "https://jobs.lever.co/aa")
    s = await run(conn, http, cfg, clock)
    assert s.added == 0 and s.errors == 1


async def test_disabled_does_nothing(tmp_path):
    conn, http, cfg, clock, seen = setup(tmp_path, {}, discovery={"enabled": False})
    add_posting(conn, 1, "https://jobs.lever.co/aa")
    assert (await run(conn, http, cfg, clock)).validated == 0 and seen == []
