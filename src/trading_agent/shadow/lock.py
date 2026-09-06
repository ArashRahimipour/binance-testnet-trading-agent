"""A non-blocking, crash-safe overlap lock preventing two `shadow-run`
processes (e.g. two overlapping cron/scheduler invocations) from ever
executing a shadow cycle at the same time.

Uses a plain `fcntl.flock` advisory lock on a dedicated lock file - never
blocks (a second process fails immediately with `ShadowLockError` rather
than queueing), and the OS releases the lock automatically if the holding
process crashes or is killed, so a crashed run can never leave the lock
permanently stuck. POSIX-only, consistent with this project's existing
implicit platform assumptions (it does not run on Windows).
"""

from __future__ import annotations

import fcntl
from pathlib import Path
from typing import IO, Self


class ShadowLockError(Exception):
    """Raised when the shadow overlap lock is already held by another process."""


class ShadowLock:
    def __init__(self, lock_path: str | Path) -> None:
        self._path = Path(lock_path)
        self._fh: IO[str] | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self._path, "w")  # noqa: SIM115 - lifetime is the lock's, not this method's
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise ShadowLockError(
                f"another shadow-run process already holds the lock at {self._path} - "
                "refusing to run two shadow cycles concurrently."
            ) from exc
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
