"""Log rotation, compression, and retention.

The bot writes to a single shared ``logs/repostbot.log`` from several processes
(bot, backfill, web console) plus one file per web-managed job under
``logs/jobs``. This module keeps that from growing without bound:

* The shared main log is rotated with a *copytruncate* strategy — its current
  contents are gzipped into a dated archive and the live file is truncated in
  place, so every process keeps writing through its existing (O_APPEND) file
  handle without needing to reopen.
* Finished per-job logs from previous days are gzipped where they sit.
* Any compressed archive (or stray raw job log) older than the retention
  window is deleted.

Everything here is safe to run repeatedly and from a process other than the one
holding the log open.
"""
from __future__ import annotations

import gzip
import logging
import os
import shutil
import time
from pathlib import Path

LOG = logging.getLogger(__name__)


def _gzip_file(src: Path, dest: Path) -> None:
    with src.open("rb") as fin, gzip.open(dest, "wb") as fout:
        shutil.copyfileobj(fin, fout, length=1024 * 1024)


def _local_midnight(now: float) -> float:
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, lt.tm_wday, lt.tm_yday, -1))


def rotate_and_prune(
    main_log: str | os.PathLike[str],
    jobs_dir: str | os.PathLike[str],
    retention_days: int,
    *,
    rotate_main: bool = True,
    now: float | None = None,
) -> dict[str, int]:
    """Rotate/compress logs and delete archives past ``retention_days``.

    ``rotate_main`` gates the copytruncate of the shared main log. Pass ``False``
    on process startup (so frequent restarts don't spray tiny archives) and
    ``True`` on a genuine day rollover or an explicit manual/cron invocation.

    Returns a summary dict: ``rotated``, ``compressed``, ``pruned``,
    ``bytes_freed``.
    """
    now = time.time() if now is None else now
    main_log = Path(main_log)
    jobs_dir = Path(jobs_dir)
    log_root = main_log.parent
    cutoff = now - max(1, retention_days) * 86400
    midnight = _local_midnight(now)
    summary = {"rotated": 0, "compressed": 0, "pruned": 0, "bytes_freed": 0}

    # 1. Copytruncate the shared main log into a dated archive.
    if rotate_main:
        try:
            if main_log.exists() and main_log.stat().st_size > 0:
                stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
                archive = log_root / f"{main_log.stem}-{stamp}.log.gz"
                _gzip_file(main_log, archive)
                os.truncate(main_log, 0)
                summary["rotated"] += 1
                LOG.info("Rotated %s -> %s", main_log.name, archive.name)
        except OSError as exc:
            LOG.warning("Could not rotate main log %s: %s", main_log, exc)

    # 2. Compress finished job logs from previous days (leave today's/active
    #    ones, and leave ones already past retention for the prune step).
    if jobs_dir.is_dir():
        for logf in sorted(jobs_dir.glob("*.log")):
            try:
                mtime = logf.stat().st_mtime
                if mtime >= midnight or mtime < cutoff:
                    continue
                gz = logf.with_suffix(logf.suffix + ".gz")
                if gz.exists():
                    continue
                _gzip_file(logf, gz)
                logf.unlink()
                summary["compressed"] += 1
            except OSError as exc:
                LOG.warning("Could not compress job log %s: %s", logf, exc)

    # 3. Prune archives (and any stale raw job logs) past the retention window.
    candidates = list(log_root.glob("*.gz"))
    if jobs_dir.is_dir():
        candidates += list(jobs_dir.glob("*.gz")) + list(jobs_dir.glob("*.log"))
    for f in candidates:
        try:
            if f.resolve() == main_log.resolve():
                continue
            if f.stat().st_mtime < cutoff:
                size = f.stat().st_size
                f.unlink()
                summary["pruned"] += 1
                summary["bytes_freed"] += size
        except OSError as exc:
            LOG.warning("Could not prune %s: %s", f, exc)

    return summary
