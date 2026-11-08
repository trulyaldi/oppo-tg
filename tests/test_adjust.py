import shutil
from pathlib import Path

from src import db
from src.ranking.adjust import adjust, recompute_adjusted
from src.ranking.weights import load_weights

SNAP = Path(__file__).parent / "fixtures" / "weights_snapshot"
W = load_weights(SNAP / "weights.yaml")
TS = "2026-11-01T00:00:00+06:00"


def test_adjust_cases():
    assert adjust(9.5, "intl_top_tier", None, None, W)[0] == 10.0
    assert adjust(0.3, "intl_other", True, False, W)[0] == 0.0
    assert adjust(7.0, "intl_top_tier", True, False, W) == (7.0, 0.0, "")
    assert adjust(7.0, "intl_top_tier", True, True, W) == (7.5, 0.5, "+0.5 top-tier")
    assert adjust(7.0, "kz_startup", True, False, W) == (8.5, 1.5, "+1.5 KZ startup")
    assert adjust(7.0, "kz_employer", None, None, W) == (8.0, 1.0, "+1.0 KZ employer")
    assert adjust(7.0, "kz_program", None, None, W) == (8.0, 1.0, "+1.0 KZ program")
    assert adjust(7.0, "unknown", True, False, W) == (7.0, 0.0, "")
    assert adjust(7.0, "intl_other", None, None, W) == (7.0, 0.0, "")
    assert adjust(7.0, "intl_other", True, False, W) == (6.5, -0.5, "-0.5 visa")


def _seed(conn):
    for pid, company in (("kz", "Arbuz.kz"), ("intl", "Citadel")):
        conn.execute(
            "INSERT INTO postings (id, source, external_id, title, company, url, fingerprint,"
            " status, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pid, "t", pid, "T", company, "https://x.example", "fp" + pid, "scored", TS, TS),
        )
        conn.execute(
            "INSERT INTO scores (posting_id, base_score, adjusted_score, reason, model,"
            " prompt_version, scored_at) VALUES (?,?,?,?,?,?,?)",
            (pid, 6.0, 6.0, "r", "m", "v", TS),
        )
    conn.commit()


def test_recompute_only_changed_and_yaml_edit(tmp_path):
    cfg = tmp_path / "cfg"
    shutil.copytree(SNAP, cfg)
    conn = db.connect(":memory:")
    db.migrate(conn)
    _seed(conn)
    w1 = load_weights(cfg / "weights.yaml")
    assert recompute_adjusted(conn, w1) == 2
    assert recompute_adjusted(conn, w1) == 0
    row = conn.execute("SELECT * FROM scores WHERE posting_id='kz'").fetchone()
    assert row["employer_category"] == "kz_startup" and row["adjusted_score"] == 7.5
    assert row["weights_version"] == w1.version + "+ll"
    text = (cfg / "weights.yaml").read_text()
    assert "kz_startup: 1.5" in text
    (cfg / "weights.yaml").write_text(text.replace("kz_startup: 1.5", "kz_startup: 3.0"))
    w2 = load_weights(cfg / "weights.yaml")
    assert w2.version != w1.version
    assert recompute_adjusted(conn, w2) == 2
    row2 = conn.execute("SELECT * FROM scores WHERE posting_id='kz'").fetchone()
    assert row2["adjusted_score"] == 9.0 and row2["weights_version"] == w2.version + "+ll"
    intl = conn.execute("SELECT * FROM scores WHERE posting_id='intl'").fetchone()
    assert intl["adjusted_score"] == 6.5 and intl["employer_category"] == "intl_top_tier"


def test_recompute_lang_loc_rule():
    conn = db.connect(":memory:")
    db.migrate(conn)
    rows = (
        ("ru_alm", "Разработчик Python", "Алматы", None),
        ("en_alm", "Python developer", "Almaty", None),
        ("ru_msk", "Разработчик Python", "Москва", None),
        ("ru_ex", "Разработчик Python", "", "Астана"),
    )
    for pid, title, loc, ex in rows:
        conn.execute(
            "INSERT INTO postings (id, source, external_id, title, company, location, url,"
            " fingerprint, status, first_seen_at, last_seen_at, ex_location)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, "t", pid, title, "", loc, "https://x.example", "fp" + pid, "scored", TS, TS, ex),
        )
        conn.execute(
            "INSERT INTO scores (posting_id, base_score, adjusted_score, reason, model,"
            " prompt_version, scored_at) VALUES (?,?,?,?,?,?,?)",
            (pid, 6.0, 6.0, "r", "m", "v", TS),
        )
    conn.commit()
    assert recompute_adjusted(conn, W) == 4
    cats = {r["posting_id"]: r["employer_category"] for r in conn.execute("SELECT * FROM scores")}
    assert cats == {
        "ru_alm": "kz_employer",
        "en_alm": "unknown",
        "ru_msk": "unknown",
        "ru_ex": "kz_employer",
    }
