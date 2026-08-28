import gzip
import os
import time

from repostbot.logrotate import rotate_and_prune


def _write(path, text, mtime=None):
    path.write_text(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_main_log_copytruncate_creates_archive(tmp_path):
    logs = tmp_path / "logs"
    jobs = logs / "jobs"
    jobs.mkdir(parents=True)
    main = logs / "repostbot.log"
    _write(main, "line one\nline two\n")

    summary = rotate_and_prune(main, jobs, retention_days=14)

    assert summary["rotated"] == 1
    # Live file is truncated in place (still exists, now empty).
    assert main.exists() and main.stat().st_size == 0
    archives = list(logs.glob("repostbot-*.log.gz"))
    assert len(archives) == 1
    with gzip.open(archives[0], "rt") as fh:
        assert fh.read() == "line one\nline two\n"


def test_empty_main_log_is_not_rotated(tmp_path):
    logs = tmp_path / "logs"
    jobs = logs / "jobs"
    jobs.mkdir(parents=True)
    main = logs / "repostbot.log"
    main.write_text("")

    summary = rotate_and_prune(main, jobs, retention_days=14)
    assert summary["rotated"] == 0
    assert list(logs.glob("*.gz")) == []


def test_startup_pass_leaves_main_log(tmp_path):
    logs = tmp_path / "logs"
    jobs = logs / "jobs"
    jobs.mkdir(parents=True)
    main = logs / "repostbot.log"
    _write(main, "keep me\n")

    summary = rotate_and_prune(main, jobs, retention_days=14, rotate_main=False)
    assert summary["rotated"] == 0
    assert main.read_text() == "keep me\n"


def test_old_job_log_is_compressed_recent_is_left(tmp_path):
    logs = tmp_path / "logs"
    jobs = logs / "jobs"
    jobs.mkdir(parents=True)
    main = logs / "repostbot.log"
    main.write_text("")

    now = time.time()
    old = jobs / "backfill-old.log"
    recent = jobs / "backfill-today.log"
    _write(old, "yesterday\n", mtime=now - 2 * 86400)
    _write(recent, "active\n", mtime=now)  # today -> left alone

    summary = rotate_and_prune(main, jobs, retention_days=14, now=now)

    assert summary["compressed"] == 1
    assert not old.exists()
    assert (jobs / "backfill-old.log.gz").exists()
    assert recent.exists()  # still active


def test_prune_deletes_archives_past_retention(tmp_path):
    logs = tmp_path / "logs"
    jobs = logs / "jobs"
    jobs.mkdir(parents=True)
    main = logs / "repostbot.log"
    main.write_text("")

    now = time.time()
    stale = logs / "repostbot-20200101-000000.log.gz"
    fresh = logs / "repostbot-recent.log.gz"
    _write(stale, "old-archive", mtime=now - 40 * 86400)
    _write(fresh, "new-archive", mtime=now - 1 * 86400)

    summary = rotate_and_prune(main, jobs, retention_days=14, rotate_main=False, now=now)

    assert summary["pruned"] == 1
    assert not stale.exists()
    assert fresh.exists()
