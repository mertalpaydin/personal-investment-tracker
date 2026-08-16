"""A cross-process advisory lock, so two updates never run at once.

The database's own ``busy_timeout`` handles a collision measured in
milliseconds: two writers reaching for the same table, one waiting a moment for
the other. It is the wrong tool for the daily update, which fetches from the
network and can take minutes. Two of those running together would not deadlock
-- they would both succeed, fetch the same days twice, and rebuild twice, for
nothing.

So the rule here is *refuse*, never queue. A second update that finds the lock
held records why it did nothing and exits. Waiting would only mean the machine
does the same work later instead of now.

Both backends -- ``msvcrt.locking`` on Windows, ``fcntl.flock`` elsewhere --
release the lock when the file descriptor closes, and the kernel closes it when
the process dies. That is why there is no stale-lock recovery here: a killed
update cannot leave the lock held, so code to break one would only ever fire
when the holder was alive and the answer was already "refuse".

The lock is advisory: it binds the processes that ask for it, and nothing else.
That is enough, because the only things that take it are the scheduled task,
the catch-up thread and the manual button -- all of them this code.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

#: The byte offset the lock is taken at. Windows locks a *range*, and a range
#: past end-of-file is legal and conflicts with nothing else -- so the file's
#: contents, if anyone ever gives it any, stay independent of the lock on it.
LOCK_OFFSET = 1 << 30


class LockBusy(RuntimeError):
    """Another process is already running an update."""


def _try_lock(fd: int) -> None:
    """Take an exclusive, non-blocking lock. Raises OSError if held."""
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - CI runs Linux; Windows is the user's platform
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    """Release. Closing would do it too; being explicit makes the pairing read."""
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        # The close in the `finally` below releases it regardless. An error
        # here would mask whatever the guarded block raised.
        pass


@contextmanager
def exclusive(path: Path | str) -> Iterator[None]:
    """Hold an exclusive lock on ``path`` for the duration of the block.

    Raises :class:`LockBusy` immediately -- never waits -- if another process
    holds it.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(target, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            _try_lock(fd)
        except OSError as exc:
            raise LockBusy("another update is already running") from exc
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
