"""Cross-process singleton lock for the live poller.

Only one ``app.py run`` poller may be active on a host at a time; a second one
would independently scan the gallery and comment, so every post ends up with two
identical replies. The guard is an advisory ``flock`` held for the lifetime of
the poller process, so the kernel releases it automatically on exit or crash
(no stale lockfiles to clean up). Because it lives on the filesystem it protects
against every launch path on the same machine: the systemd bot service, the
web-console "Start live bot" button, and a manual CLI invocation.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path

LOCK_FILENAME = "repostbot-poller.lock"


def lock_path(project_root: Path | str) -> Path:
    """The canonical lock location, next to app.py in the project root."""
    return Path(project_root) / LOCK_FILENAME


class PollerLock:
    """Non-blocking advisory lock. Use :meth:`acquire` and keep the instance
    alive for as long as the poller should own the lock."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Try to take the lock without blocking. Returns True on success,
        False if another live poller already holds it."""
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> "PollerLock":
        if not self.acquire():
            raise RuntimeError(f"live poller lock already held: {self.path}")
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def running_pid(path: Path | str) -> int | None:
    """Return the PID recorded in the lock if a poller currently holds it, or
    None if the lock is free. Non-destructive: it never keeps the lock, so it is
    safe to call from the web console to decide whether a poller already runs.
    Returns -1 when the lock is held but the PID cannot be read."""
    p = Path(path)
    if not p.exists():
        return None
    fd = os.open(p, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            data = os.pread(fd, 32, 0).decode(errors="ignore").strip()
            try:
                return int(data)
            except ValueError:
                return -1
        else:
            # We got the lock, so nobody was holding it: release and report free.
            fcntl.flock(fd, fcntl.LOCK_UN)
            return None
    finally:
        os.close(fd)
