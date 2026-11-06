import os
import subprocess
import sys

import pytest

import src.main as main_mod
from src.lock import AlreadyRunning, InstanceLock

ENV = {"TELEGRAM_BOT_TOKEN": "1:t", "TELEGRAM_CHAT_ID": "42", "ANTHROPIC_API_KEY": "k"}


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_acquire_writes_pid_and_release_removes(tmp_path):
    lock = InstanceLock(tmp_path / "data" / "x.lock")
    lock.acquire()
    assert (tmp_path / "data" / "x.lock").read_text() == str(os.getpid())
    lock.release()
    assert not (tmp_path / "data" / "x.lock").exists()


def test_second_instance_refused(tmp_path):
    path = tmp_path / "x.lock"
    with InstanceLock(path):
        with pytest.raises(AlreadyRunning) as e:
            InstanceLock(path).acquire()
        assert str(os.getpid()) in str(e.value) and "already running" in str(e.value)
    InstanceLock(path).acquire()  # free again after release


def test_stale_pid_is_recovered(tmp_path):
    path = tmp_path / "x.lock"
    path.write_text(str(dead_pid()))
    lock = InstanceLock(path)
    lock.acquire()
    assert path.read_text() == str(os.getpid())


def test_garbage_lock_is_recovered(tmp_path):
    path = tmp_path / "x.lock"
    path.write_text("not a pid")
    InstanceLock(path).acquire()
    assert path.read_text() == str(os.getpid())


def test_release_does_not_delete_someone_elses_lock(tmp_path):
    path = tmp_path / "x.lock"
    lock = InstanceLock(path)
    lock.acquire()
    path.write_text("99999999")
    lock.release()
    assert path.exists()


def test_run_refuses_second_instance(tmp_path, monkeypatch, capsys):
    db = tmp_path / "oppo.db"
    called = []

    async def fake_serve(settings):
        called.append(1)

    monkeypatch.setattr(main_mod, "_serve", fake_serve)
    env = {**ENV, "DB_PATH": str(db)}
    with InstanceLock(tmp_path / "oppo.lock"):  # a live "other instance"
        assert main_mod.main(["run"], env) == 3
        assert called == []
        assert "already running" in capsys.readouterr().err
        assert (tmp_path / "oppo.lock").exists()  # the refused start must not steal the lock


def test_run_starts_and_releases_lock(tmp_path, monkeypatch):
    seen = []

    async def fake_serve(settings):
        seen.append((tmp_path / "oppo.lock").exists())

    monkeypatch.setattr(main_mod, "_serve", fake_serve)
    assert main_mod.main(["run"], {**ENV, "DB_PATH": str(tmp_path / "oppo.db")}) == 0
    assert seen == [True] and not (tmp_path / "oppo.lock").exists()


def test_live_unrelated_pid_without_flock_is_acquired(tmp_path):
    path = tmp_path / "x.lock"
    path.write_text(str(os.getppid()))  # alive, but holds no flock (PID reuse)
    InstanceLock(path).acquire()
    assert path.read_text() == str(os.getpid())


def test_release_then_reacquire(tmp_path):
    path = tmp_path / "x.lock"
    lock = InstanceLock(path)
    lock.acquire()
    lock.release()
    lock.acquire()
    lock.release()
