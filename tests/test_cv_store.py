import os
import sqlite3
from datetime import UTC, datetime

import pytest

from src import db
from src.cv import store

NOW = datetime(2026, 11, 6, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "data" / "oppo.db")
    db.migrate(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _clean_hooks():
    store.on_new_cv_version.clear()
    store.on_forget_cv.clear()
    yield
    store.on_new_cv_version.clear()
    store.on_forget_cv.clear()


def _count(conn):
    return conn.execute("SELECT COUNT(*) FROM cv_versions").fetchone()[0]


def test_versioning(conn):
    assert store.save_cv(conn, "FAKE FIXTURE one", "{}", "text", "m", NOW) == (1, True)
    assert store.save_cv(conn, "FAKE FIXTURE two", "{}", "text", "m", NOW) == (2, True)
    assert _count(conn) == 1 and store.current_cv(conn)["version"] == 2
    assert store.save_cv(conn, "FAKE  FIXTURE\ntwo", "{}", "text", "m", NOW) == (2, False)
    assert _count(conn) == 1


def test_forget_then_monotonic(conn):
    store.save_cv(conn, "FAKE a", "{}", "text", "m", NOW)
    store.save_cv(conn, "FAKE b", "{}", "text", "m", NOW)
    assert store.forget_cv(conn) == {"cv_versions": 1, "derived": 0}
    assert store.current_cv(conn) is None
    assert store.save_cv(conn, "FAKE c", "{}", "pdf", "m", NOW) == (3, True)


def test_hooks_same_transaction(conn):
    conn.execute("CREATE TABLE dummy (v INTEGER)")
    conn.execute("INSERT INTO dummy VALUES (1)")
    conn.commit()
    seen = []

    def hook(c, v):
        seen.append(v)
        return c.execute("DELETE FROM dummy").rowcount

    store.on_new_cv_version.append(hook)
    store.save_cv(conn, "FAKE a", "{}", "text", "m", NOW)
    assert seen == [1]
    assert conn.execute("SELECT COUNT(*) FROM dummy").fetchone()[0] == 0

    conn.execute("INSERT INTO dummy VALUES (2)")
    conn.commit()
    store.on_forget_cv.append(lambda c: c.execute("DELETE FROM dummy").rowcount)
    assert store.forget_cv(conn)["derived"] == 1


def test_raising_hook_rolls_back(conn):
    store.save_cv(conn, "FAKE a", "{}", "text", "m", NOW)

    def boom(c, v):
        raise RuntimeError("x")

    store.on_new_cv_version.append(boom)
    with pytest.raises(RuntimeError):
        store.save_cv(conn, "FAKE b", "{}", "text", "m", NOW)
    assert store.current_cv(conn)["version"] == 1 and _count(conn) == 1
    store.on_forget_cv.append(lambda c: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        store.forget_cv(conn)
    assert _count(conn) == 1


def test_no_other_tables_touched(conn):
    before = {
        r[0]: conn.execute(f"SELECT COUNT(*) FROM {r[0]}").fetchone()[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if r[0] not in ("cv_versions", "sqlite_sequence")
    }
    store.save_cv(conn, "FAKE a", "{}", "text", "m", NOW)
    store.forget_cv(conn)
    for t, n in before.items():
        assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == n


def test_only_db_files_on_disk(conn, tmp_path):
    store.save_cv(conn, "FAKE a", "{}", "text", "m", NOW)
    store.save_cv(conn, "FAKE b", "{}", "text", "m", NOW)
    conn.commit()
    names = set(os.listdir(tmp_path / "data"))
    assert names <= {"oppo.db", "oppo.db-wal", "oppo.db-shm"}


def test_migration_v4_on_v3_db_with_rows(tmp_path):
    path = tmp_path / "v3.db"
    c = db.connect(path)
    c.executescript(db.SCHEMA_1)
    c.execute("PRAGMA user_version=1")
    for s in (db.SCHEMA_2, db.SCHEMA_3):
        for stmt in s.split(";"):
            if stmt.strip():
                c.execute(stmt)
    c.execute("PRAGMA user_version=3")
    c.execute("INSERT INTO state VALUES ('k','v')")
    c.commit()
    db.migrate(c)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 6
    assert c.execute("SELECT value FROM state WHERE key='k'").fetchone()[0] == "v"
    assert isinstance(c, sqlite3.Connection) and _count(c) == 0
