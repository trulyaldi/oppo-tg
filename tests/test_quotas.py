import itertools
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src import db
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.delivery.digest import select_digest, send_digest
from src.ranking.quotas import pick_with_quotas
from src.ranking.weights import Quotas, load_weights

SNAP = Path(__file__).parent / "fixtures" / "weights_snapshot"
W = load_weights(SNAP / "weights.yaml")
CFG = AppConfig.from_dict({})
TS = "2026-11-01T00:00:00+06:00"
NOW = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
SETTINGS = Settings.from_env(
    {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "42", "ANTHROPIC_API_KEY": "k"}
)
_COMPANY = {
    "kz_employer": ("Kaspi.kz", None),
    "kz_startup": ("Arbuz.kz", None),
    "intl_top_tier": ("Citadel", None),
    "intl_other": ("Acme Robotics", "intl"),
    "unknown": ("Mystery Corp", None),
}
_n = itertools.count()


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    return c


def add(conn, cat, base, visa=None, remote=None):
    company, region = _COMPANY[cat]
    pid = f"p{next(_n):04d}"
    conn.execute(
        "INSERT INTO postings (id, source, external_id, title, company, url, fingerprint,"
        " status, first_seen_at, last_seen_at, remote) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            pid,
            "t",
            pid,
            f"T {pid}",
            company,
            "https://x.example",
            "fp" + pid,
            "scored",
            TS,
            TS,
            None if remote is None else int(remote),
        ),
    )
    conn.execute(
        "INSERT INTO scores (posting_id, base_score, adjusted_score, reason, model,"
        " prompt_version, scored_at, employer_region, region_confidence, visa_barrier)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            pid,
            base,
            base,
            "r",
            "m",
            "v",
            TS,
            region or "unknown",
            0.9 if region else 0.0,
            None if visa is None else int(visa),
        ),
    )
    return pid


def cats(items):
    return [i.employer_category for i in items]


def test_pick_pure():
    q = Quotas(kz=1, intl=1, best_remaining=1)
    items = [("u", 9), ("k", 8), ("i", 7), ("k", 6), ("i", 5)]
    bucket_of = {"u": None, "k": "kz_employer", "i": "intl_other"}
    assert pick_with_quotas(items, q, lambda x: bucket_of[x[0]]) == [("u", 9), ("k", 8), ("i", 7)]


def test_quotas_four_four_two(conn):
    for b in (6.0, 6.5, 7.0, 7.5, 8.0, 6.2):
        add(conn, "kz_employer", b)
        add(conn, "intl_other", b)
    items = select_digest(conn, CFG, W)
    assert len(items) == 10
    assert [i.score for i in items] == sorted((i.score for i in items), reverse=True)
    assert sum(c == "kz_employer" for c in cats(items)) >= 4
    assert sum(c == "intl_other" for c in cats(items)) >= 4


def test_fall_through(conn):
    add(conn, "kz_employer", 7.0)
    for k in range(12):
        add(conn, "intl_other", 6.0 + k * 0.1)
    items = select_digest(conn, CFG, W)
    assert cats(items).count("kz_employer") == 1 and cats(items).count("intl_other") == 9


def test_unknown_only_via_free_slots(conn):
    for _ in range(3):
        add(conn, "unknown", 9.5)
    for _ in range(5):
        add(conn, "kz_employer", 6.0)
        add(conn, "intl_other", 6.0)
    items = select_digest(conn, CFG, W)
    # 4+4 forced, then 2 free slots go to the best remaining: the unknown 9.5s.
    assert cats(items).count("unknown") == 2
    assert cats(items).count("kz_employer") == 4 and cats(items).count("intl_other") == 4


def test_quality_floor_uses_base(conn):
    add(conn, "kz_startup", 4.5)
    ok = add(conn, "kz_startup", 5.0)
    items = select_digest(conn, CFG, W)
    assert [i.posting_id for i in items] == [ok]


def test_bad_kz_never_outranks_good_intl(conn):
    for kz_cat, kz_base in itertools.product(("kz_employer", "kz_startup"), (3.0, 4.0, 5.0, 5.5)):
        for intl_cat, visa, remote, base in itertools.product(
            ("intl_top_tier", "intl_other"), (None, True), (True, False, None), (7.5, 8.0, 9.0)
        ):
            c = db.connect(":memory:")
            db.migrate(c)
            kz_id = add(c, kz_cat, kz_base)
            intl_id = add(c, intl_cat, base, visa, remote)
            order = [i.posting_id for i in select_digest(c, CFG, W)]
            if kz_id in order and intl_id in order:
                assert order.index(intl_id) < order.index(kz_id)
            assert intl_id in order


def _tmp_weights(tmp_path, old, new):
    d = tmp_path / "w"
    shutil.copytree(SNAP, d)
    f = d / "weights.yaml"
    f.write_text(f.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    return load_weights(f)


def test_yaml_edit_quota_zero(conn, tmp_path):
    for _ in range(3):
        add(conn, "kz_employer", 5.0)
    for _ in range(6):
        add(conn, "intl_other", 8.0)
    w0 = _tmp_weights(
        tmp_path, "{kz: 4, intl: 4, best_remaining: 2}", "{kz: 0, intl: 4, best_remaining: 6}"
    )
    items = select_digest(conn, CFG, w0)
    # no forced KZ: KZ items only enter through best-remaining slots, after the 8.0 intl ones
    assert len(items) == 9
    assert cats(items)[:6] == ["intl_other"] * 6


def test_yaml_edit_startup_bonus_reorders(conn, tmp_path):
    kz = add(conn, "kz_startup", 5.5)
    intl = add(conn, "intl_other", 7.5)
    assert [i.posting_id for i in select_digest(conn, CFG, W)] == [intl, kz]
    w3 = _tmp_weights(tmp_path, "kz_startup: 1.0", "kz_startup: 3.0")
    assert [i.posting_id for i in select_digest(conn, CFG, w3)] == [kz, intl]


class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        return type("M", (), {"message_id": len(self.sent)})()


async def test_invalid_weights_single_notice(conn, tmp_path):
    bad = tmp_path / "weights.yaml"
    bad.write_text("adjustments: [", encoding="utf-8")
    cfg = AppConfig.from_dict({"ranking": {"weights_file": str(bad)}})
    add(conn, "unknown", 7.0)
    bot = Bot()
    assert await send_digest(bot, conn, SETTINGS, cfg, FixedClock(NOW)) == 1
    notices = [t for t in bot.sent if t.startswith("weights.yaml invalid, using defaults:")]
    assert len(notices) == 1
    add(conn, "unknown", 7.0)
    await send_digest(bot, conn, SETTINGS, cfg, FixedClock(NOW))
    assert len([t for t in bot.sent if t.startswith("weights.yaml invalid")]) == 1


async def test_invalid_weights_empty_still_sends(conn, tmp_path):
    cfg = AppConfig.from_dict({"ranking": {"weights_file": str(tmp_path / "missing.yaml")}})
    bot = Bot()
    await send_digest(bot, conn, SETTINGS, cfg, FixedClock(NOW))
    assert bot.sent[-1] == "No new opportunities today."
    assert bot.sent[0].startswith("weights.yaml invalid")
