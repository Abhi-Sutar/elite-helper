"""Single-instance lock via an OS file lock: released by the kernel if the process dies, so never stale."""

import os
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

_LOCK_OFFSET = 1024  # lock a byte past the PID text so other processes can still read the PID on Windows


def _try_lock(f):
    try:
        if os.name == "nt":
            f.seek(_LOCK_OFFSET)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(f):
    if os.name == "nt":
        f.seek(_LOCK_OFFSET)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class ListenerLock:
    def __init__(self, path):
        self.path = Path(path)
        self._f = None

    def acquire(self):
        """True if we now hold the lock (PID written); False if another process holds it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+b")
        if not _try_lock(f):
            f.close()
            return False
        f.seek(0)
        f.truncate(0)
        f.write(str(os.getpid()).encode())
        f.flush()
        self._f = f
        return True

    def release(self):
        if self._f is not None:
            try:
                _unlock(self._f)
            finally:
                self._f.close()
                self._f = None


def holder(path):
    """(running, pid): probe the lock without keeping it."""
    path = Path(path)
    if not path.exists():
        return False, None
    probe = ListenerLock(path)
    if probe.acquire():
        probe.release()
        return False, None
    try:
        return True, int(path.read_bytes().split(b"\0")[0].decode() or 0) or None
    except (OSError, ValueError):
        return True, None
