"""Single-instance PID lock for `run` (S3, D-86).

Two bots polling one token make Telegram answer TelegramConflictError, so the
second `run` must refuse to start. The lock is an fcntl.flock on a file that
also holds the owner's PID for the error message. The kernel drops the flock when
the process dies, so a stale file (or a reused PID) never blocks startup.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path


class AlreadyRunning(RuntimeError):
    def __init__(self, pid: int, path: Path) -> None:
        self.pid = pid
        self.path = path
        super().__init__(
            f"another oppo-tg instance is already running (PID {pid}, lock file {path}). "
            "Stop it first; two bots on one token cause Telegram conflict errors."
        )


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


class InstanceLock:
    """Holds an exclusive flock on the lock file for the process lifetime (D-148)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(5):
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                pid = _read_pid(self.path)
                os.close(fd)
                raise AlreadyRunning(pid or 0, self.path) from None
            try:  # the holder may have unlinked the file between our open and flock
                same = os.fstat(fd).st_ino == os.stat(self.path).st_ino
            except FileNotFoundError:
                same = False
            if not same:
                os.close(fd)
                continue
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
            self._fd = fd
            return
        raise RuntimeError(f"could not acquire lock {self.path}")

    def release(self) -> None:
        if self._fd is None:
            return
        if _read_pid(self.path) == os.getpid():
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
        os.close(self._fd)  # closing drops the flock
        self._fd = None

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
