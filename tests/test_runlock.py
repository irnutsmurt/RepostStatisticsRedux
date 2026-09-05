import os
import subprocess
import sys
import textwrap

from repostbot.runlock import PollerLock, lock_path, running_pid


def test_second_acquire_fails_while_first_held(tmp_path):
    path = lock_path(tmp_path)
    first = PollerLock(path)
    assert first.acquire() is True
    # A second lock on the same file cannot be taken while the first holds it.
    second = PollerLock(path)
    assert second.acquire() is False
    # running_pid reports the holder's PID (this process).
    assert running_pid(path) == os.getpid()
    first.release()
    # Once released, the lock is free again.
    assert running_pid(path) is None
    assert second.acquire() is True
    second.release()


def test_running_pid_none_when_no_lockfile(tmp_path):
    assert running_pid(lock_path(tmp_path)) is None


def test_context_manager_releases(tmp_path):
    path = lock_path(tmp_path)
    with PollerLock(path) as held:
        assert held._fd is not None
        assert running_pid(path) == os.getpid()
    assert running_pid(path) is None


def test_lock_is_cross_process(tmp_path):
    """A different process cannot take the lock while we hold it, and the lock
    frees automatically when that process exits (kernel-released flock)."""
    path = lock_path(tmp_path)
    mine = PollerLock(path)
    assert mine.acquire() is True

    probe = textwrap.dedent(
        f"""
        from repostbot.runlock import PollerLock
        lock = PollerLock({str(path)!r})
        print("acquired" if lock.acquire() else "blocked")
        """
    )
    env = dict(os.environ, PYTHONPATH=os.getcwd())
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, env=env,
    )
    assert out.stdout.strip() == "blocked", out.stderr
    mine.release()
