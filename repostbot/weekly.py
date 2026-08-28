"""Weekly repost-statistics recap post.

Picks one meme template (least-recently-used, so 52 templates rotate across a
year with no repeats), computes the week's repost stats, renders a title and
description, and publishes it to the Imgur gallery via the account's OAuth
token. Backfill-detected reposts are excluded; only what the live bot found in
the window counts.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from string import Template
from typing import Any

from .config import Settings
from .database import RepostDatabase

LOG = logging.getLogger(__name__)

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif")
_TITLE_MAX = 40

DEFAULT_TITLE = "Weekly Repost Statistics for $weekstart – $weekend"

DEFAULT_BODY = (
    "Beep boop. Here's this week's repost roundup.\n"
    "\n"
    "Reposts found: $found\n"
    "Commented on: $commented\n"
    "\n"
    "Not every repost gets a comment. We stay quiet when someone reposts\n"
    "their own content, and when a post scores too low to bother with.\n"
    "\n"
    "$called_out"
    "\n"
    "To stop this bot commenting on your posts,\n"
    "comment with: @$bot blockme\n"
)


@dataclass(frozen=True)
class WeeklyPost:
    title: str
    description: str
    template_name: str | None
    template_path: str | None
    week_start: int
    window_start: int
    window_end: int
    found: int
    commented: int


def _fmt_long(d: date) -> str:
    return f"{d.strftime('%B')} {d.day} {d.year}"


def _fmt_short(d: date) -> str:
    return f"{d.strftime('%b')} {d.day:02d} {d.year}"


def week_window(now: datetime) -> tuple[int, int, date, date]:
    """The reporting week is the 7 days ending on the run day, inclusive.

    A Friday run covers the previous Saturday through that Friday. Returns
    (window_start_epoch, window_end_epoch, display_start_date, display_end_date)
    using local time, matching the local scheduling of the post.
    """
    run_date = now.date()
    start_date = run_date - timedelta(days=6)
    window_start = int(datetime.combine(start_date, dtime.min).timestamp())
    window_end = int(datetime.combine(run_date + timedelta(days=1), dtime.min).timestamp())
    return window_start, window_end, start_date, run_date


def list_templates(template_dir: str) -> list[str]:
    """Image filenames in the template folder, sorted, example art excluded."""
    try:
        names = os.listdir(template_dir)
    except OSError:
        return []
    out = [
        n for n in names
        if n.lower().endswith(_IMAGE_EXTS) and "example" not in n.lower()
    ]
    return sorted(out)


def _truncate_title(title: str) -> str:
    clean = " ".join((title or "").split())
    if len(clean) > _TITLE_MAX:
        return clean[:_TITLE_MAX].rstrip() + "..."
    return clean


def _called_out_block(originals: list[Any]) -> str:
    if not originals:
        return "Nothing crossed the line into a comment this week.\n"
    lines = ["Originals we called out this week:"]
    for row in originals:
        d = _fmt_short(date.fromtimestamp(int(row["posted_at"])))
        title = _truncate_title(row["title"])
        gallery = row["gallery_id"]
        lines.append(f'  {d}   "{title}"   https://imgur.com/gallery/{gallery}')
    return "\n".join(lines) + "\n"


def render(
    settings: Settings,
    stats: dict[str, int],
    originals: list[Any],
    display_start: date,
    display_end: date,
) -> tuple[str, str]:
    tokens = {
        "weekstart": _fmt_long(display_start),
        "weekend": _fmt_long(display_end),
        "found": str(stats["found"]),
        "commented": str(stats["commented"]),
        "called_out": _called_out_block(originals),
        "archive_total": f"{stats['archive_total']:,}",
        "earliest": (
            _fmt_long(date.fromtimestamp(stats["earliest_posted_at"]))
            if stats.get("earliest_posted_at") else "the beginning"
        ),
        "date": _fmt_long(display_end),
        "bot": settings.bot_username,
        "newline": "\n",
    }
    title = Template(DEFAULT_TITLE).safe_substitute(tokens)
    description = Template(DEFAULT_BODY).safe_substitute(tokens)
    return title, description


def build_weekly_post(
    settings: Settings, db: RepostDatabase, now: datetime | None = None,
) -> WeeklyPost:
    """Assemble the post (stats + template pick + rendered text). Read-only:
    does not mark the template used, record the post, or upload anything."""
    now = now or datetime.now()
    window_start, window_end, disp_start, disp_end = week_window(now)
    stats = db.weekly_stats(window_start, window_end)
    originals = db.weekly_commented_originals(window_start, window_end)
    title, description = render(settings, stats, originals, disp_start, disp_end)

    available = list_templates(settings.weekly_template_dir)
    template_name = db.pick_weekly_template(available)
    template_path = (
        os.path.join(settings.weekly_template_dir, template_name)
        if template_name else None
    )
    return WeeklyPost(
        title=title, description=description,
        template_name=template_name, template_path=template_path,
        week_start=window_start, window_start=window_start, window_end=window_end,
        found=stats["found"], commented=stats["commented"],
    )


def run_weekly_post(
    settings: Settings, db: RepostDatabase, client, now: datetime | None = None,
) -> dict[str, Any]:
    """Publish the weekly post for real. Idempotent per week: if a post for this
    week already exists it is skipped. Only marks the template used and records
    the post after a successful upload."""
    now = now or datetime.now()
    week_start, _, _, _ = week_window(now)
    if db.weekly_post_exists(week_start):
        LOG.info("Weekly post for week starting %s already exists; skipping.", week_start)
        return {"status": "skipped", "reason": "already-posted", "week_start": week_start}

    post = build_weekly_post(settings, db, now=now)
    if not post.template_path:
        LOG.warning("Weekly post skipped: no templates found in %s", settings.weekly_template_dir)
        return {"status": "skipped", "reason": "no-templates", "week_start": post.week_start}

    tags = [t.strip() for t in settings.weekly_post_tags.split(",") if t.strip()]
    result = client.create_gallery_post(
        post.template_path, post.title, post.description, tags,
    )
    posted_at = int(now.timestamp())
    db.mark_weekly_template_used(post.template_name, posted_at)
    db.record_weekly_post(post.week_start, posted_at, result.get("gallery_id"), post.template_name)
    LOG.info("Weekly post published: %s (template %s)", result.get("url"), post.template_name)
    return {"status": "posted", "week_start": post.week_start, "template": post.template_name, **result}
