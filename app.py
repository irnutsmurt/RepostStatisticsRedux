#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
from datetime import date, datetime
from pathlib import Path
import sys
import time

from repostbot.commands import CommandPoller
from repostbot.config import load_settings
from repostbot.database import RepostDatabase
from repostbot.imgur import ImgurClient
from repostbot.logrotate import rotate_and_prune
from repostbot.runlock import PollerLock, lock_path
from repostbot.service import RepostService
from repostbot.weekly import build_weekly_post, run_weekly_post


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Modernized Imgur repost detector")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Scan recent gallery pages")
    run.add_argument("--once", action="store_true", help="Run one scan and exit")
    run.add_argument("--job-id", type=int, help=argparse.SUPPRESS)

    backfill = sub.add_parser("backfill", help="Populate history without ever commenting")
    backfill.add_argument("--section", default="top", choices=["top", "hot", "user"])
    backfill.add_argument("--sort", default="viral")
    backfill.add_argument("--window", default="all", choices=["day", "week", "month", "year", "all"])
    backfill.add_argument("--pages", type=int, default=25)
    backfill.add_argument("--start-page", type=int, default=0)
    backfill.add_argument("--stop-after-stale", type=int, default=None,
                          help="Stop a section after this many consecutive pages with no new "
                               "posts (default: BACKFILL_STOP_AFTER_STALE_PAGES; 0 disables).")
    backfill.add_argument("--job-id", type=int, help=argparse.SUPPRESS)

    web = sub.add_parser("web", help="Run the LAN administration web app")
    web.add_argument("--host", help="Override WEB_HOST")
    web.add_argument("--port", type=int, help="Override WEB_PORT")

    sub.add_parser("stats", help="Show local database counts")
    sub.add_parser("rotate-logs", help="Compress the current logs and delete archives past the retention window")
    weekly = sub.add_parser("post-weekly", help="Publish (or preview) the weekly repost-statistics recap")
    weekly.add_argument("--dry-run", action="store_true",
                        help="Print the title, description, and chosen template without posting")
    sub.add_parser("debug-history", help="Diagnose Imgur command-history access without printing comment bodies")

    block = sub.add_parser("block", help="Block an Imgur account ID from bot comments")
    block.add_argument("account_id", type=int)
    unblock = sub.add_parser("unblock", help="Remove an account ID from the block list")
    unblock.add_argument("account_id", type=int)
    blockuser = sub.add_parser("blockuser", help="Block an Imgur username from bot comments")
    blockuser.add_argument("username")
    unblockuser = sub.add_parser("unblockuser", help="Remove an Imgur username from the block list")
    unblockuser.add_argument("username")
    return p


def weekly_post_due(settings, now) -> bool:
    """True when it is the configured weekday and at/after the configured time.
    Idempotency (one post per week) is enforced separately in the DB, so this
    can safely return True for the rest of the scheduled day."""
    if not settings.weekly_post_enabled:
        return False
    if now.strftime("%A").lower() != settings.weekly_post_day.lower():
        return False
    try:
        hour, minute = (int(x) for x in settings.weekly_post_time.split(":", 1))
    except ValueError:
        hour, minute = 13, 0
    return (now.hour, now.minute) >= (hour, minute)


def configure_logging(level: str, log_file: str | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    # Third-party libraries are extremely chatty at DEBUG (per-request connection
    # pool lines, PNG chunk decoding). Keep them at INFO so our own DEBUG output
    # (what was read / skipped / hashed) stays readable.
    for noisy in ("urllib3", "requests", "PIL", "PIL.PngImagePlugin", "PIL.Image"):
        logging.getLogger(noisy).setLevel(logging.INFO)


def make_runtime(settings):
    db = RepostDatabase(settings.database_path)
    client = ImgurClient(
        settings.client_id,
        settings.access_token,
        settings.client_secret,
        settings.refresh_token,
        telemetry_callback=db.record_api_event,
    )
    service = RepostService(settings, client, db)
    poller = CommandPoller(settings, client, db)
    return service, poller, db


def sleep_with_command_polling(seconds: int, poller: CommandPoller, interval: int, heartbeat=None) -> None:
    deadline = time.monotonic() + max(0, seconds)
    interval = max(1, interval)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(interval, remaining))
        poller.poll_once()
        if heartbeat:
            heartbeat()


def update_job(db: RepostDatabase, job_id: int | None, **fields) -> None:
    if job_id:
        db.update_web_job(job_id, **fields)


def main() -> int:
    args = parser().parse_args()
    settings = load_settings()
    configure_logging(settings.log_level, settings.log_file)

    if args.command == "rotate-logs":
        summary = rotate_and_prune(
            settings.log_file, settings.web_job_log_dir, settings.log_retention_days
        )
        print(
            f"Rotated {summary['rotated']} main log(s), compressed {summary['compressed']} job log(s), "
            f"pruned {summary['pruned']} archive(s), freed {summary['bytes_freed'] / 1024 / 1024:.1f} MB."
        )
        return 0

    if args.command == "web":
        from waitress import serve
        from repostbot.webapp import create_app
        host = args.host or settings.web_host
        port = args.port or settings.web_port
        logging.getLogger(__name__).info("Starting LAN admin console on http://%s:%d", host, port)
        serve(create_app(), host=host, port=port, threads=6)
        return 0

    db = RepostDatabase(settings.database_path)
    if args.command == "stats":
        stats = db.dashboard_stats()
        print(f"Known posts: {stats['posts']}")
        print(f"Repost detections: {stats['reposts']}")
        print(f"Posts commented on: {stats['commented']}")
        print(f"Username blocks: {stats['blocked_users']}")
        print(f"Account ID blocks: {stats['blocked_accounts']}")
        db.close()
        return 0
    if args.command == "post-weekly":
        if args.dry_run:
            post = build_weekly_post(settings, db)
            print(f"TITLE:\n{post.title}\n")
            print(f"DESCRIPTION:\n{post.description}")
            print(f"TEMPLATE: {post.template_path or '(none found)'}")
            print(f"(reposts found={post.found}, commented={post.commented}, "
                  f"week_start={post.week_start})")
            db.close()
            return 0
        if not settings.client_id or not settings.access_token:
            print("IMGUR_CLIENT_ID and IMGUR_ACCESS_TOKEN are required to post.", file=sys.stderr)
            db.close()
            return 2
        client = ImgurClient(
            settings.client_id, settings.access_token, settings.client_secret,
            settings.refresh_token, telemetry_callback=db.record_api_event,
        )
        try:
            result = run_weekly_post(settings, db, client)
        except Exception as exc:
            print(f"Weekly post failed: {exc}", file=sys.stderr)
            db.close()
            return 1
        print(result)
        db.close()
        return 0
    if args.command == "block":
        added = db.block_account(args.account_id)
        print(("Blocked" if added else "Already blocked") + f" account ID {args.account_id}")
        db.close()
        return 0
    if args.command == "unblock":
        removed = db.unblock_account(args.account_id)
        print(("Unblocked" if removed else "Was not blocked") + f" account ID {args.account_id}")
        db.close()
        return 0
    if args.command == "blockuser":
        added = db.block_username(args.username)
        print(("Blocked" if added else "Already blocked") + f" Imgur user {args.username}")
        db.close()
        return 0
    if args.command == "unblockuser":
        removed = db.unblock_username(args.username)
        print(("Unblocked" if removed else "Was not blocked") + f" Imgur user {args.username}")
        db.close()
        return 0
    if args.command == "debug-history":
        db.close()
        if not settings.client_id or not settings.access_token:
            print("IMGUR_CLIENT_ID and IMGUR_ACCESS_TOKEN are required.", file=sys.stderr)
            return 2
        client = ImgurClient(settings.client_id, settings.access_token, settings.client_secret, settings.refresh_token)
        try:
            me = client.authenticated_account()
            auth_name = me.get("url") or me.get("username") or "<unknown>"
            auth_id = me.get("id", "<unknown>")
            print(f"OAuth account: {auth_name} (id={auth_id})")
            print(f"BOT_USERNAME: {settings.bot_username}")
            print(f"Account match: {str(auth_name).lower() == settings.bot_username.lower()}")
            print()
            for result in client.account_history_diagnostics(settings.bot_username):
                state = "OK" if result["ok"] else "ERROR"
                line = f"{result['name']}: {state}"
                if result.get("shape") is not None:
                    line += f" shape={result['shape']} count={result.get('count')}"
                if result.get("types"):
                    line += " types=" + ",".join(result["types"])
                if result.get("error"):
                    line += f" error={result['error']}"
                print(line)
            return 0
        except Exception as exc:
            print(f"History diagnostic failed: {exc}", file=sys.stderr)
            return 1
    db.close()

    if not settings.client_id:
        print("IMGUR_CLIENT_ID is required. Copy .env.example to .env and fill it in.", file=sys.stderr)
        return 2

    service, poller, db = make_runtime(settings)
    job_id = getattr(args, "job_id", None)
    update_job(db, job_id, status="running", heartbeat_at=int(time.time()))
    exit_code = 0
    poller_lock: PollerLock | None = None
    try:
        if args.command == "backfill":
            def progress(section, page, end_page, stats):
                update_job(
                    db, job_id,
                    heartbeat_at=int(time.time()),
                    progress_json={
                        "section": section,
                        "current_page": page,
                        "end_page_exclusive": end_page,
                        "scanned": stats.scanned,
                        "inserted": stats.inserted,
                        "known": stats.known,
                        "skipped": stats.skipped,
                        "reposts": stats.reposts,
                        "errors": stats.errors,
                    },
                )

            stop_stale = (args.stop_after_stale if args.stop_after_stale is not None
                          else settings.backfill_stop_after_stale_pages)
            stats = service.crawl(
                sections=(args.section,), sort=args.sort, window=args.window,
                pages=max(0, args.pages), start_page=max(0, args.start_page),
                allow_comment=False, progress_callback=progress,
                stop_after_stale_pages=max(0, stop_stale),
            )
            print(stats)
            update_job(db, job_id, status="completed", heartbeat_at=int(time.time()),
                       finished_at=int(time.time()), exit_code=0)
            return 0

        # Singleton guard: only one live poller may run at a time. A second one
        # (e.g. the web-console "Start live bot" button clicked while the systemd
        # bot service is already polling) would independently comment on every
        # post, so refuse to start and exit instead of double-commenting. The
        # lock is released automatically when this process exits.
        lock_file = lock_path(Path(__file__).resolve().parent)
        poller_lock = PollerLock(lock_file)
        if not poller_lock.acquire():
            poller_lock = None
            msg = (f"Another live poller already holds {lock_file}; refusing to "
                   "start a second one to avoid duplicate comments.")
            logging.getLogger(__name__).error(msg)
            print(msg, file=sys.stderr)
            update_job(db, job_id, status="failed",
                       finished_at=int(time.time()), exit_code=3)
            return 3

        # Startup housekeeping: compress finished job logs and prune old
        # archives, but leave the shared main log alone so frequent restarts
        # don't spray tiny archives. The main log is rotated on the day rollover
        # below (or via the `rotate-logs` subcommand from cron).
        last_rotation_day = date.today()
        try:
            rotate_and_prune(
                settings.log_file, settings.web_job_log_dir,
                settings.log_retention_days, rotate_main=False,
            )
        except Exception:
            logging.getLogger(__name__).exception("Startup log housekeeping failed")

        while True:
            today = date.today()
            if today != last_rotation_day:
                try:
                    summary = rotate_and_prune(
                        settings.log_file, settings.web_job_log_dir, settings.log_retention_days,
                    )
                    logging.getLogger(__name__).info("Daily log rotation: %s", summary)
                except Exception:
                    logging.getLogger(__name__).exception("Daily log rotation failed")
                last_rotation_day = today

            if weekly_post_due(settings, datetime.now()):
                try:
                    outcome = run_weekly_post(settings, db, service.client)
                    if outcome.get("status") == "posted":
                        logging.getLogger(__name__).info(
                            "Weekly recap posted: %s", outcome.get("url"),
                        )
                except Exception:
                    logging.getLogger(__name__).exception("Weekly recap post failed")

            poller.poll_once()

            stats = service.crawl(
                sections=settings.gallery_sections,
                sort=settings.gallery_sort,
                window=settings.gallery_window,
                pages=settings.gallery_pages,
                allow_comment=True,
            )
            print(stats, flush=True)
            update_job(
                db, job_id, status="running", heartbeat_at=int(time.time()),
                progress_json={
                    "scanned": stats.scanned, "inserted": stats.inserted,
                    "known": stats.known, "skipped": stats.skipped,
                    "reposts": stats.reposts, "comments": stats.comments,
                    "errors": stats.errors,
                },
            )
            if args.once:
                update_job(db, job_id, status="completed", finished_at=int(time.time()), exit_code=0)
                return 0
            sleep_with_command_polling(
                settings.poll_interval_seconds, poller,
                settings.command_poll_interval_seconds,
                heartbeat=lambda: update_job(db, job_id, heartbeat_at=int(time.time())),
            )
    except KeyboardInterrupt:
        exit_code = 130
        update_job(db, job_id, status="stopped", finished_at=int(time.time()), exit_code=130)
        return 130
    except Exception:
        exit_code = 1
        logging.getLogger(__name__).exception("Worker terminated unexpectedly")
        update_job(db, job_id, status="failed", finished_at=int(time.time()), exit_code=1)
        raise
    finally:
        if poller_lock is not None:
            poller_lock.release()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
