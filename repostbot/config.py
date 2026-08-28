from __future__ import annotations

from dataclasses import dataclass
import os
import sqlite3
from dotenv import load_dotenv


def _read_runtime_overrides(database_path: str) -> dict[str, str]:
    if not database_path or database_path == ":memory:":
        return {}
    try:
        con = sqlite3.connect(database_path, timeout=1.0)
        try:
            rows = con.execute("SELECT key, value FROM runtime_settings").fetchall()
            return {str(k): str(v) for k, v in rows}
        finally:
            con.close()
    except sqlite3.Error:
        # First run: the table may not exist until RepostDatabase initializes it.
        return {}


def _raw(name: str, default: str, overrides: dict[str, str]) -> str:
    if name in overrides:
        return overrides[name]
    return os.getenv(name, default)


def _bool_value(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _int_value(raw: str | None, default: int) -> int:
    return default if raw in (None, "") else int(str(raw).strip())


def _command_max_age_seconds(overrides: dict[str, str], default_days: int = 7) -> int:
    """Resolve the maximum age of an actionable command mention, in seconds.

    The setting is configured in *days* (COMMAND_MAX_AGE_DAYS) but compared in
    seconds downstream. A legacy COMMAND_MAX_AGE_SECONDS value is still honored
    when the days key is absent, so older configs keep working.
    """
    days_raw = _raw("COMMAND_MAX_AGE_DAYS", "", overrides)
    if str(days_raw).strip():
        return max(1, round(float(str(days_raw).strip()) * 86400))
    legacy_raw = _raw("COMMAND_MAX_AGE_SECONDS", "", overrides)
    if str(legacy_raw).strip():
        return max(1, _int_value(legacy_raw, default_days * 86400))
    return default_days * 86400


@dataclass(frozen=True)
class Settings:
    client_id: str
    access_token: str | None
    client_secret: str | None
    refresh_token: str | None
    dry_run: bool
    database_path: str
    log_level: str
    poll_interval_seconds: int
    gallery_sections: tuple[str, ...]
    gallery_sort: str
    gallery_window: str
    gallery_pages: int
    min_net_score: int
    max_post_age_seconds: int
    comment_cooldown_seconds: int
    ahash_max_distance: int
    dhash_h_max_distance: int
    dhash_v_max_distance: int
    bitcount_padding: int
    ignore_same_account: bool
    bot_username: str
    commands_enabled: bool
    command_poll_interval_seconds: int
    command_max_age_seconds: int
    blockme_reply: str
    blockme_already_blocked_reply: str
    log_file: str = "logs/repostbot.log"
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    web_job_log_dir: str = "logs/jobs"
    log_retention_days: int = 14
    weekly_post_enabled: bool = False
    weekly_post_day: str = "friday"
    weekly_post_time: str = "13:00"
    weekly_template_dir: str = "template"
    weekly_post_tags: str = ""
    backfill_stop_after_stale_pages: int = 50


def load_settings() -> Settings:
    load_dotenv()
    database_path = os.getenv("DATABASE_PATH", "repoststats.sqlite3")
    overrides = _read_runtime_overrides(database_path)

    sections = tuple(
        part.strip()
        for part in _raw("GALLERY_SECTIONS", "user,hot", overrides).split(",")
        if part.strip()
    )

    return Settings(
        client_id=os.getenv("IMGUR_CLIENT_ID", "").strip(),
        access_token=os.getenv("IMGUR_ACCESS_TOKEN") or None,
        client_secret=os.getenv("IMGUR_CLIENT_SECRET") or None,
        refresh_token=os.getenv("IMGUR_REFRESH_TOKEN") or None,
        dry_run=_bool_value(_raw("DRY_RUN", "true", overrides), True),
        database_path=database_path,
        log_level=_raw("LOG_LEVEL", "INFO", overrides).upper(),
        poll_interval_seconds=_int_value(_raw("POLL_INTERVAL_SECONDS", "300", overrides), 300),
        gallery_sections=sections,
        gallery_sort=_raw("GALLERY_SORT", "viral", overrides),
        gallery_window=_raw("GALLERY_WINDOW", "day", overrides),
        gallery_pages=_int_value(_raw("GALLERY_PAGES", "4", overrides), 4),
        min_net_score=_int_value(_raw("MIN_NET_SCORE", "40", overrides), 40),
        max_post_age_seconds=_int_value(_raw("MAX_POST_AGE_SECONDS", "43200", overrides), 43200),
        comment_cooldown_seconds=_int_value(_raw("COMMENT_COOLDOWN_SECONDS", "40", overrides), 40),
        ahash_max_distance=_int_value(_raw("AHASH_MAX_DISTANCE", "3", overrides), 3),
        dhash_h_max_distance=_int_value(_raw("DHASH_H_MAX_DISTANCE", "10", overrides), 10),
        dhash_v_max_distance=_int_value(_raw("DHASH_V_MAX_DISTANCE", "10", overrides), 10),
        bitcount_padding=_int_value(_raw("BITCOUNT_PADDING", "22", overrides), 22),
        ignore_same_account=_bool_value(_raw("IGNORE_SAME_ACCOUNT", "true", overrides), True),
        bot_username=os.getenv("BOT_USERNAME", "RepostStatisticsRedux").strip(),
        commands_enabled=_bool_value(_raw("COMMANDS_ENABLED", "true", overrides), True),
        command_poll_interval_seconds=_int_value(_raw("COMMAND_POLL_INTERVAL_SECONDS", "60", overrides), 60),
        command_max_age_seconds=_command_max_age_seconds(overrides),
        blockme_reply=os.getenv(
            "BLOCKME_REPLY",
            "Done. I won't post repost-statistics comments on your posts anymore.",
        ),
        blockme_already_blocked_reply=os.getenv(
            "BLOCKME_ALREADY_BLOCKED_REPLY",
            "You're already blocked from my repost-statistics comments.",
        ),
        log_file=os.getenv("LOG_FILE", "logs/repostbot.log"),
        web_host=os.getenv("WEB_HOST", "0.0.0.0"),
        web_port=int(os.getenv("WEB_PORT", "8080")),
        web_job_log_dir=os.getenv("WEB_JOB_LOG_DIR", "logs/jobs"),
        log_retention_days=_int_value(_raw("LOG_RETENTION_DAYS", "14", overrides), 14),
        weekly_post_enabled=_bool_value(_raw("WEEKLY_POST_ENABLED", "false", overrides), False),
        weekly_post_day=_raw("WEEKLY_POST_DAY", "friday", overrides).strip().lower(),
        weekly_post_time=_raw("WEEKLY_POST_TIME", "13:00", overrides).strip(),
        weekly_template_dir=_raw("WEEKLY_TEMPLATE_DIR", "template", overrides),
        weekly_post_tags=_raw("WEEKLY_POST_TAGS", "", overrides),
        backfill_stop_after_stale_pages=_int_value(
            _raw("BACKFILL_STOP_AFTER_STALE_PAGES", "50", overrides), 50),
    )
