"""Single-instance lock, so two scheduled runs never overlap.

This matters more than usual here: Apify bills per result, so two concurrent
sweeps do not just race on the database, they double the invoice. A slow run
that overruns its schedule must be skipped, not joined.

`flock(1)` in the crontab is the idiomatic Linux answer and is what
`deploy/crontab` uses. This module is the in-process backstop for everything
else (systemd, manual runs, Windows dev).
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

_IS_WINDOWS = sys.platform == "win32"


class AlreadyRunning(RuntimeError):
    """Another instance holds the lock."""


def _try_lock(fd: int) -> bool:
    """Take an exclusive non-blocking lock. False if someone else holds it."""
    try:
        if _IS_WINDOWS:
            import msvcrt

            # Locks one byte from the CURRENT position, so byte 0 is reserved
            # for the lock and never written to -- writing into or truncating
            # a locked range fails on Windows.
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    try:
        if _IS_WINDOWS:
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        # Closing the descriptor releases it regardless; never mask a real
        # error raised from inside the guarded block.
        pass


@contextmanager
def single_instance(lock_path: Path):
    """Hold an exclusive lock for the duration of the block.

    Raises AlreadyRunning rather than blocking: a scheduled job that is still
    going means we are behind, and queueing another only makes it worse.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)

    if not _try_lock(fd):
        os.close(fd)
        raise AlreadyRunning(f"another run holds {lock_path}")

    try:
        # Record the pid for whoever has to debug a stuck lock. Byte 0 is the
        # lock region on Windows, so the pid goes after it and the file is
        # never truncated.
        try:
            os.lseek(fd, 1, os.SEEK_SET)
            os.write(fd, f" pid={os.getpid()}\n".encode())
        except OSError:
            pass  # diagnostics only

        log.debug("lock.acquired", path=str(lock_path), pid=os.getpid())
        yield

    finally:
        _unlock(fd)
        os.close(fd)
