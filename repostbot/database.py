from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from .hashing import ImageSignature, hamming_hex
from .models import GalleryPost, HistoricalPost


LOG = logging.getLogger(__name__)


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    gallery_id TEXT,
    image_index INTEGER NOT NULL DEFAULT 1,
    image_count INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL DEFAULT '',
    posted_at INTEGER NOT NULL,
    account_id INTEGER,
    account_url TEXT,
    image_url TEXT NOT NULL,
    animated INTEGER NOT NULL,
    width INTEGER,
    height INTEGER,
    exact_hash TEXT NOT NULL,
    ahash TEXT,
    dhash_h TEXT,
    dhash_v TEXT,
    bit_count INTEGER NOT NULL,
    discovered_at INTEGER NOT NULL,
    comment_id INTEGER,
    commented_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_posts_exact_hash ON posts(exact_hash);
CREATE INDEX IF NOT EXISTS idx_posts_bit_count ON posts(bit_count);
CREATE INDEX IF NOT EXISTS idx_posts_posted_at ON posts(posted_at);
CREATE INDEX IF NOT EXISTS idx_posts_gallery_id ON posts(gallery_id);

CREATE TABLE IF NOT EXISTS blocked_accounts (
    account_id INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS blocked_users (
    account_url TEXT PRIMARY KEY COLLATE NOCASE,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_notifications (
    notification_id TEXT PRIMARY KEY,
    processed_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS repost_events (
    current_post_id TEXT PRIMARY KEY,
    first_post_id TEXT NOT NULL,
    last_post_id TEXT NOT NULL,
    match_count INTEGER NOT NULL,
    exact_all INTEGER NOT NULL,
    ahash_distance INTEGER,
    dhash_h_distance INTEGER,
    dhash_v_distance INTEGER,
    title_similarity TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    detected_at INTEGER NOT NULL,
    FOREIGN KEY(current_post_id) REFERENCES posts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_repost_events_detected_at ON repost_events(detected_at DESC);

CREATE TABLE IF NOT EXISTS api_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    status INTEGER,
    client_limit INTEGER,
    client_remaining INTEGER,
    user_limit INTEGER,
    user_remaining INTEGER,
    user_reset INTEGER
);
CREATE INDEX IF NOT EXISTS idx_api_events_created_at ON api_events(created_at DESC);

CREATE TABLE IF NOT EXISTS web_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    pid INTEGER,
    args_json TEXT NOT NULL DEFAULT '{}',
    progress_json TEXT NOT NULL DEFAULT '{}',
    log_path TEXT,
    started_at INTEGER NOT NULL,
    heartbeat_at INTEGER,
    finished_at INTEGER,
    exit_code INTEGER
);
CREATE INDEX IF NOT EXISTS idx_web_jobs_started_at ON web_jobs(started_at DESC);

CREATE TABLE IF NOT EXISTS weekly_templates (
    filename TEXT PRIMARY KEY,
    last_used_at INTEGER NOT NULL,
    use_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS weekly_posts (
    week_start INTEGER PRIMARY KEY,
    posted_at INTEGER NOT NULL,
    gallery_id TEXT,
    template TEXT
);
"""


class RepostDatabase:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=5.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def has_post(self, post_id: str) -> bool:
        return self.connection.execute("SELECT 1 FROM posts WHERE id = ?", (post_id,)).fetchone() is not None

    def is_blocked(self, account_id: int | None, account_url: str | None = None) -> bool:
        if account_id is not None:
            row = self.connection.execute(
                "SELECT 1 FROM blocked_accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
            if row is not None:
                return True
        if account_url:
            return self.connection.execute(
                "SELECT 1 FROM blocked_users WHERE account_url = ? COLLATE NOCASE",
                (account_url.strip(),),
            ).fetchone() is not None
        return False

    def block_account(self, account_id: int) -> bool:
        cursor = self.connection.execute("INSERT OR IGNORE INTO blocked_accounts(account_id) VALUES (?)", (account_id,))
        self.connection.commit()
        return cursor.rowcount > 0

    def unblock_account(self, account_id: int) -> bool:
        cursor = self.connection.execute("DELETE FROM blocked_accounts WHERE account_id = ?", (account_id,))
        self.connection.commit()
        return cursor.rowcount > 0

    def block_username(self, username: str) -> bool:
        username = username.strip()
        if not username:
            raise ValueError("username cannot be empty")
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO blocked_users(account_url, created_at) VALUES (?, ?)",
            (username, int(time.time())),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def unblock_username(self, username: str) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM blocked_users WHERE account_url = ? COLLATE NOCASE",
            (username.strip(),),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def list_blocked_users(self, limit: int = 500) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT account_url, created_at FROM blocked_users ORDER BY created_at DESC, account_url LIMIT ?",
            (limit,),
        ).fetchall()

    def list_blocked_accounts(self, limit: int = 500) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT account_id FROM blocked_accounts ORDER BY account_id LIMIT ?", (limit,)
        ).fetchall()

    def notification_processed(self, notification_id: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM processed_notifications WHERE notification_id = ?", (str(notification_id),)
        ).fetchone() is not None

    def mark_notification_processed(self, notification_id: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO processed_notifications(notification_id, processed_at) VALUES (?, ?)",
            (str(notification_id), int(time.time())),
        )
        self.connection.commit()

    def insert_post(self, post: GalleryPost, signature: ImageSignature) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO posts (
                id, gallery_id, image_index, image_count, title, posted_at,
                account_id, account_url, image_url, animated, width, height,
                exact_hash, ahash, dhash_h, dhash_v, bit_count, discovered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                post.id, post.link_id, post.image_index, post.image_count, post.title,
                post.timestamp, post.account_id, post.account_url, post.image_url,
                int(signature.animated), post.width, post.height,
                signature.exact_hash, signature.ahash, signature.dhash_h, signature.dhash_v,
                signature.bit_count, int(time.time()),
            ),
        )
        self.connection.commit()

    def mark_commented(self, post_id: str, comment_id: int | None) -> None:
        self.connection.execute(
            "UPDATE posts SET comment_id = ?, commented_at = ? WHERE id = ?",
            (comment_id, int(time.time()), post_id),
        )
        self.connection.execute(
            "UPDATE repost_events SET decision = 'commented', reason = 'comment-posted' WHERE current_post_id = ?",
            (post_id,),
        )
        self.connection.commit()

    def was_commented(self, post_id: str) -> bool:
        row = self.connection.execute("SELECT commented_at FROM posts WHERE id = ?", (post_id,)).fetchone()
        return bool(row and row["commented_at"])

    def record_repost_event(
        self,
        post: GalleryPost,
        duplicates: list[HistoricalPost],
        *,
        title_similarity: str,
        decision: str,
        reason: str,
    ) -> None:
        if not duplicates:
            return
        # Persist the perceptual hamming distances of the match shown as "last
        # seen" so the UI can report real image-match confidence. These are NULL
        # for a byte-identical (exact) match, which the UI renders as "exact".
        last = duplicates[-1]
        self.connection.execute(
            """
            INSERT INTO repost_events (
                current_post_id, first_post_id, last_post_id, match_count,
                exact_all, ahash_distance, dhash_h_distance, dhash_v_distance,
                title_similarity, decision, reason, detected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(current_post_id) DO UPDATE SET
                first_post_id=excluded.first_post_id,
                last_post_id=excluded.last_post_id,
                match_count=excluded.match_count,
                exact_all=excluded.exact_all,
                ahash_distance=excluded.ahash_distance,
                dhash_h_distance=excluded.dhash_h_distance,
                dhash_v_distance=excluded.dhash_v_distance,
                title_similarity=excluded.title_similarity,
                decision=excluded.decision,
                reason=excluded.reason,
                detected_at=excluded.detected_at
            """,
            (
                post.id, duplicates[0].id, last.id, len(duplicates),
                int(all(item.exact_match for item in duplicates)),
                last.ahash_distance, last.dhash_h_distance, last.dhash_v_distance,
                title_similarity, decision, reason, int(time.time()),
            ),
        )
        self.connection.commit()

    def count_repost_events(self, reason: str | None = None) -> int:
        if reason:
            return int(self.connection.execute(
                "SELECT COUNT(*) FROM repost_events WHERE reason = ?", (reason,)
            ).fetchone()[0])
        return int(self.connection.execute("SELECT COUNT(*) FROM repost_events").fetchone()[0])

    def list_repost_events(self, limit: int = 100, offset: int = 0,
                           reason: str | None = None) -> list[sqlite3.Row]:
        where = "WHERE e.reason = ?" if reason else ""
        params: tuple = (reason, limit, offset) if reason else (limit, offset)
        return self.connection.execute(
            f"""
            SELECT e.*, p.title, p.account_url, p.posted_at, p.image_url, p.commented_at,
                   p.gallery_id AS current_gallery_id, p.image_index AS current_index,
                   p.image_count AS current_count,
                   pf.gallery_id AS first_gallery_id, pf.posted_at AS first_posted_at,
                   pf.account_url AS first_account_url,
                   pl.gallery_id AS last_gallery_id
            FROM repost_events e
            JOIN posts p ON p.id = e.current_post_id
            LEFT JOIN posts pf ON pf.id = e.first_post_id
            LEFT JOIN posts pl ON pl.id = e.last_post_id
            {where}
            ORDER BY e.detected_at DESC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()

    def list_posts(self, limit: int = 100, offset: int = 0) -> list[sqlite3.Row]:
        # `copies` counts every archived post sharing this exact payload, so
        # `copies - 1` is how many times the image has been reposted. The
        # subquery is cheap thanks to idx_posts_exact_hash and only runs for the
        # page's rows.
        return self.connection.execute(
            """
            SELECT p.id, p.gallery_id, p.image_index, p.image_count, p.title, p.posted_at,
                   p.account_url, p.account_id, p.animated, p.width, p.height, p.image_url,
                   p.exact_hash, p.ahash, p.dhash_h, p.dhash_v, p.bit_count,
                   p.discovered_at, p.commented_at,
                   (SELECT COUNT(*) FROM posts d WHERE d.exact_hash = p.exact_hash) AS copies
            FROM posts p
            ORDER BY p.discovered_at DESC, p.id
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()

    def recent_posts(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.list_posts(limit, 0)

    # -- Weekly recap post support -----------------------------------------
    # Backfill records repost_events too, always with reason 'backfill-no-comments'.
    # The weekly recap only counts what the *live* bot found, so every query here
    # filters those out.
    _NOT_BACKFILL = "reason != 'backfill-no-comments'"

    def weekly_stats(self, window_start: int, window_end: int) -> dict[str, int]:
        found = self.connection.execute(
            f"SELECT COUNT(*) FROM repost_events "
            f"WHERE {self._NOT_BACKFILL} AND detected_at >= ? AND detected_at < ?",
            (window_start, window_end),
        ).fetchone()[0]
        commented = self.connection.execute(
            "SELECT COUNT(*) FROM repost_events "
            "WHERE reason = 'comment-posted' AND detected_at >= ? AND detected_at < ?",
            (window_start, window_end),
        ).fetchone()[0]
        total = self.connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        earliest = self.connection.execute("SELECT MIN(posted_at) FROM posts").fetchone()[0]
        return {
            "found": int(found),
            "commented": int(commented),
            "archive_total": int(total),
            "earliest_posted_at": int(earliest) if earliest is not None else 0,
        }

    def weekly_commented_originals(self, window_start: int, window_end: int) -> list[sqlite3.Row]:
        """Distinct original galleries whose reposts we commented on this week,
        oldest original first. Albums expand per image, so dedupe by gallery."""
        return self.connection.execute(
            """
            SELECT fp.gallery_id AS gallery_id, MIN(fp.posted_at) AS posted_at, fp.title AS title
            FROM repost_events e
            JOIN posts fp ON fp.id = e.first_post_id
            WHERE e.reason = 'comment-posted' AND e.detected_at >= ? AND e.detected_at < ?
            GROUP BY fp.gallery_id
            ORDER BY posted_at ASC
            """,
            (window_start, window_end),
        ).fetchall()

    def pick_weekly_template(self, available: list[str]) -> str | None:
        """Least-recently-used pick: templates never used come first, then the
        ones used longest ago. Filenames absent from the folder are ignored, and
        newly added filenames get picked before any that have been used."""
        if not available:
            return None
        used = {
            row["filename"]: int(row["last_used_at"])
            for row in self.connection.execute(
                "SELECT filename, last_used_at FROM weekly_templates"
            )
        }
        return min(available, key=lambda name: (used.get(name, -1), name))

    def mark_weekly_template_used(self, filename: str, used_at: int) -> None:
        self.connection.execute(
            """
            INSERT INTO weekly_templates (filename, last_used_at, use_count)
            VALUES (?, ?, 1)
            ON CONFLICT(filename) DO UPDATE SET
                last_used_at = excluded.last_used_at,
                use_count = use_count + 1
            """,
            (filename, used_at),
        )
        self.connection.commit()

    def weekly_post_exists(self, week_start: int) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM weekly_posts WHERE week_start = ?", (week_start,)
        ).fetchone()
        return row is not None

    def record_weekly_post(self, week_start: int, posted_at: int,
                           gallery_id: str | None, template: str | None) -> None:
        self.connection.execute(
            """
            INSERT INTO weekly_posts (week_start, posted_at, gallery_id, template)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(week_start) DO UPDATE SET
                posted_at = excluded.posted_at,
                gallery_id = excluded.gallery_id,
                template = excluded.template
            """,
            (week_start, posted_at, gallery_id, template),
        )
        self.connection.commit()

    def find_duplicates(
        self,
        post: GalleryPost,
        signature: ImageSignature,
        *,
        ahash_max_distance: int,
        dhash_h_max_distance: int,
        dhash_v_max_distance: int,
        bitcount_padding: int,
    ) -> list[HistoricalPost]:
        exact_rows = self.connection.execute(
            """SELECT * FROM posts
               WHERE exact_hash = ? AND id != ? AND posted_at < ?
               ORDER BY posted_at ASC""",
            (signature.exact_hash, post.id, post.timestamp),
        ).fetchall()
        exact_ids = {row["id"] for row in exact_rows}
        results = [self._row_to_history(row, exact=True) for row in exact_rows]
        if exact_rows:
            LOG.debug("find_duplicates(%s): %d exact-hash match(es)", post.id, len(exact_rows))

        if signature.animated or not signature.ahash:
            LOG.debug(
                "find_duplicates(%s): animated/exact-only, %d result(s)", post.id, len(results),
            )
            return results

        low = max(0, signature.bit_count - bitcount_padding)
        high = signature.bit_count + bitcount_padding
        candidates = self.connection.execute(
            """SELECT * FROM posts
               WHERE animated = 0 AND id != ? AND posted_at < ?
                 AND bit_count BETWEEN ? AND ?
               ORDER BY posted_at ASC""",
            (post.id, post.timestamp, low, high),
        ).fetchall()
        LOG.debug(
            "find_duplicates(%s): bit_count=%d, scanning %d candidate(s) in [%d,%d] "
            "(limits ah<=%d hh<=%d vh<=%d)",
            post.id, signature.bit_count, len(candidates), low, high,
            ahash_max_distance, dhash_h_max_distance, dhash_v_max_distance,
        )

        matched = 0
        for row in candidates:
            if row["id"] in exact_ids or not row["ahash"] or not row["dhash_h"] or not row["dhash_v"]:
                continue
            ad = hamming_hex(signature.ahash, row["ahash"])
            hd = hamming_hex(signature.dhash_h, row["dhash_h"])
            vd = hamming_hex(signature.dhash_v, row["dhash_v"])
            if ad <= ahash_max_distance and hd <= dhash_h_max_distance and vd <= dhash_v_max_distance:
                matched += 1
                LOG.debug(
                    "  near-match %s: ah=%d/%d hh=%d/%d vh=%d/%d",
                    row["id"], ad, ahash_max_distance, hd, dhash_h_max_distance, vd, dhash_v_max_distance,
                )
                results.append(self._row_to_history(row, exact=False, ad=ad, hd=hd, vd=vd))

        if candidates:
            LOG.debug(
                "find_duplicates(%s): %d/%d candidate(s) within thresholds",
                post.id, matched, len(candidates),
            )
        results.sort(key=lambda item: item.timestamp)
        return results

    @staticmethod
    def _row_to_history(row: sqlite3.Row, *, exact: bool, ad: int | None = None,
                        hd: int | None = None, vd: int | None = None) -> HistoricalPost:
        keys = row.keys()
        gallery_id = row["gallery_id"] if "gallery_id" in keys else None
        image_index = row["image_index"] if "image_index" in keys else 1
        image_count = row["image_count"] if "image_count" in keys else 1
        return HistoricalPost(
            id=row["id"], title=row["title"], timestamp=row["posted_at"],
            account_id=row["account_id"], account_url=row["account_url"], image_url=row["image_url"],
            exact_match=exact, ahash_distance=ad, dhash_h_distance=hd, dhash_v_distance=vd,
            gallery_id=gallery_id or row["id"], image_index=image_index or 1, image_count=image_count or 1,
        )

    def count_posts(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0])

    def count_commented(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM posts WHERE commented_at IS NOT NULL").fetchone()[0])

    def count_blocked_users(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM blocked_users").fetchone()[0])

    def count_blocked_accounts(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM blocked_accounts").fetchone()[0])

    def dashboard_stats(self) -> dict[str, int]:
        row = self.connection.execute(
            """
            SELECT COUNT(*) posts,
                   SUM(CASE WHEN animated = 1 THEN 1 ELSE 0 END) animated,
                   SUM(CASE WHEN commented_at IS NOT NULL THEN 1 ELSE 0 END) commented,
                   COUNT(DISTINCT exact_hash) unique_exact
            FROM posts
            """
        ).fetchone()
        reposts = int(self.connection.execute("SELECT COUNT(*) FROM repost_events").fetchone()[0])
        exact_reposts = int(self.connection.execute(
            "SELECT COUNT(*) FROM repost_events WHERE exact_all = 1"
        ).fetchone()[0])
        return {
            "posts": int(row["posts"] or 0),
            "animated": int(row["animated"] or 0),
            "commented": int(row["commented"] or 0),
            "unique_exact": int(row["unique_exact"] or 0),
            "reposts": reposts,
            "exact_reposts": exact_reposts,
            "blocked_users": self.count_blocked_users(),
            "blocked_accounts": self.count_blocked_accounts(),
        }

    # Runtime settings are deliberately limited by the web layer. This table
    # contains only non-secret operational overrides.
    def get_runtime_settings(self) -> dict[str, str]:
        return {row["key"]: row["value"] for row in self.connection.execute(
            "SELECT key, value FROM runtime_settings"
        ).fetchall()}

    def set_runtime_setting(self, key: str, value: str) -> None:
        self.connection.execute(
            """INSERT INTO runtime_settings(key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, str(value), int(time.time())),
        )
        self.connection.commit()

    def delete_runtime_setting(self, key: str) -> None:
        self.connection.execute("DELETE FROM runtime_settings WHERE key = ?", (key,))
        self.connection.commit()

    def record_api_event(self, event: dict[str, Any]) -> None:
        cur = self.connection.execute(
            """
            INSERT INTO api_events (
                created_at, method, path, status, client_limit, client_remaining,
                user_limit, user_remaining, user_reset
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(time.time()), event.get("method", "?"), event.get("path", "?"), event.get("status"),
                event.get("client_limit"), event.get("client_remaining"), event.get("user_limit"),
                event.get("user_remaining"), event.get("user_reset"),
            ),
        )
        # Telemetry is diagnostic, not permanent history. Keep roughly the
        # latest 50k responses so this table cannot grow forever.
        if cur.lastrowid and int(cur.lastrowid) % 500 == 0:
            self.connection.execute("DELETE FROM api_events WHERE id < ?", (int(cur.lastrowid) - 50_000,))
        self.connection.commit()

    def api_summary(self) -> dict[str, Any]:
        since = int(time.time()) - 86400
        counts = self.connection.execute(
            """SELECT COUNT(*) calls,
                      SUM(CASE WHEN status = 503 THEN 1 ELSE 0 END) errors_503,
                      SUM(CASE WHEN status >= 500 THEN 1 ELSE 0 END) errors_5xx,
                      SUM(CASE WHEN status = 429 THEN 1 ELSE 0 END) errors_429
               FROM api_events WHERE created_at >= ?""",
            (since,),
        ).fetchone()
        latest = self.connection.execute(
            """SELECT * FROM api_events
               WHERE client_remaining IS NOT NULL OR user_remaining IS NOT NULL
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        return {
            "calls_24h": int(counts["calls"] or 0),
            "errors_503_24h": int(counts["errors_503"] or 0),
            "errors_5xx_24h": int(counts["errors_5xx"] or 0),
            "errors_429_24h": int(counts["errors_429"] or 0),
            "latest": dict(latest) if latest else None,
        }

    def recent_api_events(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM api_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def create_web_job(self, job_type: str, args: dict[str, Any], log_path: str) -> int:
        cur = self.connection.execute(
            """INSERT INTO web_jobs(job_type, status, args_json, progress_json, log_path, started_at)
               VALUES (?, 'starting', ?, '{}', ?, ?)""",
            (job_type, json.dumps(args, sort_keys=True), log_path, int(time.time())),
        )
        self.connection.commit()
        return int(cur.lastrowid)

    def update_web_job(self, job_id: int, **fields: Any) -> None:
        allowed = {"status", "pid", "progress_json", "heartbeat_at", "finished_at", "exit_code"}
        items = [(k, v) for k, v in fields.items() if k in allowed]
        if not items:
            return
        assignments = ", ".join(f"{key} = ?" for key, _ in items)
        values = [json.dumps(v, sort_keys=True) if key == "progress_json" and not isinstance(v, str) else v
                  for key, v in items]
        values.append(job_id)
        self.connection.execute(f"UPDATE web_jobs SET {assignments} WHERE id = ?", values)
        self.connection.commit()

    def get_web_job(self, job_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM web_jobs WHERE id = ?", (job_id,)).fetchone()

    def list_web_jobs(self, limit: int = 30, job_type: str | None = None) -> list[sqlite3.Row]:
        if job_type:
            return self.connection.execute(
                "SELECT * FROM web_jobs WHERE job_type = ? ORDER BY id DESC LIMIT ?", (job_type, limit)
            ).fetchall()
        return self.connection.execute("SELECT * FROM web_jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def active_web_job(self, job_type: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT * FROM web_jobs
               WHERE job_type = ? AND status IN ('starting','running','stopping')
               ORDER BY id DESC LIMIT 1""",
            (job_type,),
        ).fetchone()
