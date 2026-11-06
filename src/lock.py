"""Single-instance PID lock for `run` (S3, D-86).

Two bots polling one token make Telegram answer TelegramConflictError, so the
second `run` must refuse to start. The lock is a file holding the owner's PID;
a lock whose PID is no longer alive is stale and gets taken over.
"""

from __future__ import annotations

import contextlib
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


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


class InstanceLock:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._held = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(3):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                pid = _read_pid(self.path)
                if pid is not None and pid_alive(pid):
                    raise AlreadyRunning(pid, self.path) from None
                with contextlib.suppress(FileNotFoundError):  # stale or garbage lock
                    self.path.unlink()
                continue
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            self._held = True
            return
        raise RuntimeError(f"could not acquire lock {self.path}")

    def release(self) -> None:
        if self._held and _read_pid(self.path) == os.getpid():
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
        self._held = False

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
