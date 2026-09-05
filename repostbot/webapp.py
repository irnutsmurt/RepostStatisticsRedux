from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import time
from typing import Any

from flask import Flask, Response, abort, flash, redirect, render_template, request, url_for

from .config import load_settings
from .database import RepostDatabase
from .runlock import lock_path, running_pid


LOG = logging.getLogger(__name__)
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,30}$")
ACTIVE_JOB_STATES = {"starting", "running", "stopping"}

SETTING_SPECS: list[dict[str, Any]] = [
    {"key": "DRY_RUN", "label": "Dry run", "type": "bool", "help": "When enabled, detect/store reposts but never publish repost comments."},
    {"key": "COMMANDS_ENABLED", "label": "Self-service commands", "type": "bool", "help": "Poll mentions for blockme/help commands."},
    {"key": "IGNORE_SAME_ACCOUNT", "label": "Ignore same-account reposts", "type": "bool", "help": "Do not comment when the previous matching post is from the same account."},
    {"key": "POLL_INTERVAL_SECONDS", "label": "Live scan interval (seconds)", "type": "int", "min": 10, "max": 86400},
    {"key": "COMMAND_POLL_INTERVAL_SECONDS", "label": "Command poll interval (seconds)", "type": "int", "min": 10, "max": 3600},
    {"key": "COMMAND_MAX_AGE_DAYS", "label": "Maximum command age (days)", "type": "int", "min": 1, "max": 365, "help": "Ignore blockme/help mentions older than this many days (guards against replaying ancient history after downtime)."},
    {"key": "GALLERY_SECTIONS", "label": "Live gallery sections", "type": "sections", "help": "Comma-separated: user, hot, top."},
    {"key": "GALLERY_SORT", "label": "Gallery sort", "type": "choice", "choices": ["viral", "top", "time", "rising"]},
    {"key": "GALLERY_WINDOW", "label": "Gallery window", "type": "choice", "choices": ["day", "week", "month", "year", "all"]},
    {"key": "GALLERY_PAGES", "label": "Pages per live scan", "type": "int", "min": 1, "max": 500},
    {"key": "MIN_NET_SCORE", "label": "Minimum net score", "type": "int", "min": -10000, "max": 1000000},
    {"key": "MAX_POST_AGE_SECONDS", "label": "Maximum post age (seconds)", "type": "int", "min": 60, "max": 2592000, "help": "Only comment on posts newer than this many seconds (43200 = 12 hours)."},
    {"key": "COMMENT_COOLDOWN_SECONDS", "label": "Comment cooldown (seconds)", "type": "int", "min": 0, "max": 3600},
    {"key": "AHASH_MAX_DISTANCE", "label": "aHash max distance", "type": "int", "min": 0, "max": 256},
    {"key": "DHASH_H_MAX_DISTANCE", "label": "Horizontal dHash max distance", "type": "int", "min": 0, "max": 256},
    {"key": "DHASH_V_MAX_DISTANCE", "label": "Vertical dHash max distance", "type": "int", "min": 0, "max": 256},
    {"key": "BITCOUNT_PADDING", "label": "Bit-count candidate padding", "type": "int", "min": 0, "max": 256},
    {"key": "LOG_LEVEL", "label": "Log level", "type": "choice", "choices": ["DEBUG", "INFO", "WARNING", "ERROR"]},
    {"key": "LOG_RETENTION_DAYS", "label": "Compressed log retention (days)", "type": "int", "min": 1, "max": 3650, "help": "Delete rotated/compressed log archives older than this many days."},
    {"key": "WEEKLY_POST_ENABLED", "label": "Weekly recap post", "type": "bool", "help": "Publish a weekly repost-statistics recap to the Imgur gallery on the day/time below."},
    {"key": "WEEKLY_POST_DAY", "label": "Weekly post day", "type": "choice", "choices": ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]},
    {"key": "WEEKLY_POST_TIME", "label": "Weekly post time (24h local)", "type": "text", "pattern": r"^([01]?\d|2[0-3]):[0-5]\d$", "pattern_help": "use 24-hour HH:MM, e.g. 13:00"},
    {"key": "WEEKLY_POST_TAGS", "label": "Weekly post tags", "type": "text", "help": "Optional comma-separated Imgur gallery tags."},
]


def _epoch(value: Any) -> str:
    if value in (None, "", 0, "0"):
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(value)))
    except (TypeError, ValueError, OSError):
        return str(value)


def _json(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def _pid_matches_web_job(pid: int | None, job_id: int, project_root: Path) -> bool:
    """Return True only when *pid* still looks like this web-managed worker.

    The web UI persists job PIDs in SQLite so long backfills survive a dashboard
    restart. Merely checking ``kill(pid, 0)`` is not sufficient because Linux can
    later reuse a dead worker's PID for an unrelated process. On Linux we verify
    the command line contains our app.py and the exact ``--job-id``. If /proc is
    unavailable, fall back to the old liveness check rather than breaking other
    platforms.
    """
    if not _pid_alive(pid):
        return False
    pid = int(pid)
    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    if not proc_cmdline.exists():
        return True
    try:
        raw = proc_cmdline.read_bytes()
        argv = [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]
    except OSError:
        return False
    if not argv:
        return False

    try:
        idx = argv.index("--job-id")
        if idx + 1 >= len(argv) or argv[idx + 1] != str(job_id):
            return False
    except ValueError:
        return False

    expected_app = (project_root / "app.py").resolve()
    for arg in argv:
        if not arg.endswith("app.py"):
            continue
        candidate = Path(arg)
        if not candidate.is_absolute():
            try:
                cwd = Path(os.readlink(f"/proc/{pid}/cwd"))
                candidate = cwd / candidate
            except OSError:
                candidate = project_root / candidate
        try:
            if candidate.resolve() == expected_app:
                return True
        except OSError:
            continue
    return False


class JobManager:
    def __init__(self, project_root: Path, database_path: str, log_dir: str):
        self.project_root = project_root
        self.database_path = database_path
        log_path = Path(log_dir)
        self.log_dir = log_path if log_path.is_absolute() else project_root / log_path
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.processes: dict[int, subprocess.Popen] = {}

    def _db(self) -> RepostDatabase:
        return RepostDatabase(self.database_path)

    def refresh(self) -> None:
        db = self._db()
        try:
            for row in db.list_web_jobs(limit=100):
                if row["status"] not in ACTIVE_JOB_STATES:
                    continue
                job_id = int(row["id"])
                proc = self.processes.get(job_id)
                if proc is not None:
                    code = proc.poll()
                    if code is not None:
                        status = "completed" if code == 0 else "failed"
                        db.update_web_job(job_id, status=status, finished_at=int(time.time()), exit_code=code)
                        self.processes.pop(job_id, None)
                elif not _pid_matches_web_job(row["pid"], job_id, self.project_root):
                    # The worker vanished, or its PID was reused by an unrelated
                    # process. We cannot recover an exit code after the web process
                    # itself restarted, so use a neutral terminal state.
                    db.update_web_job(job_id, status="exited", finished_at=int(time.time()))
                    LOG.info("Reconciled stale web job #%d type=%s pid=%s -> exited",
                             job_id, row["job_type"], row["pid"])
                else:
                    # This is a real worker started by a previous web-console
                    # process. Deliberately keep it alive/adopted: a dashboard
                    # restart should not destroy a long-running backfill.
                    LOG.info("Recovered live web job #%d type=%s pid=%s from previous web session",
                             job_id, row["job_type"], row["pid"])
        finally:
            db.close()

    def start(self, job_type: str, args: dict[str, Any], command: list[str]) -> int:
        self.refresh()
        db = self._db()
        try:
            active = db.active_web_job(job_type)
            if active is not None and _pid_matches_web_job(active["pid"], int(active["id"]), self.project_root):
                raise RuntimeError(f"A {job_type} job is already running (job #{active['id']}).")
            stamp = time.strftime("%Y%m%d-%H%M%S")
            log_path = self.log_dir / f"{job_type}-{stamp}.log"
            job_id = db.create_web_job(job_type, args, str(log_path))
            full_command = [sys.executable, str(self.project_root / "app.py"), *command, "--job-id", str(job_id)]
            with log_path.open("ab", buffering=0) as out:
                proc = subprocess.Popen(
                    full_command,
                    cwd=self.project_root,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            self.processes[job_id] = proc
            db.update_web_job(job_id, status="running", pid=proc.pid, heartbeat_at=int(time.time()))
            LOG.info("Started web job #%d type=%s pid=%d: %s", job_id, job_type, proc.pid, full_command)
            return job_id
        except Exception:
            # If the job row was created but spawning failed, refresh() will
            # clean it on the next request. Preserve the original exception.
            raise
        finally:
            db.close()

    def stop(self, job_id: int) -> None:
        db = self._db()
        try:
            row = db.get_web_job(job_id)
            if row is None:
                raise KeyError(job_id)
            if row["status"] not in ACTIVE_JOB_STATES:
                return
            pid = row["pid"]
            db.update_web_job(job_id, status="stopping", heartbeat_at=int(time.time()))
            if pid and _pid_matches_web_job(pid, job_id, self.project_root):
                try:
                    os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
                except (AttributeError, ProcessLookupError, PermissionError, OSError):
                    os.kill(int(pid), signal.SIGTERM)
            proc = self.processes.get(job_id)
            if proc is not None:
                try:
                    code = proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    code = None
                if code is not None:
                    self.processes.pop(job_id, None)
            db.update_web_job(job_id, status="stopped", finished_at=int(time.time()), exit_code=code if proc else None)
            LOG.info("Stopped web job #%d pid=%s", job_id, pid)
        finally:
            db.close()


def _setting_value(settings, key: str) -> str:
    mapping = {
        "DRY_RUN": str(settings.dry_run).lower(),
        "COMMANDS_ENABLED": str(settings.commands_enabled).lower(),
        "IGNORE_SAME_ACCOUNT": str(settings.ignore_same_account).lower(),
        "POLL_INTERVAL_SECONDS": str(settings.poll_interval_seconds),
        "COMMAND_POLL_INTERVAL_SECONDS": str(settings.command_poll_interval_seconds),
        "COMMAND_MAX_AGE_DAYS": str(max(1, round(settings.command_max_age_seconds / 86400))),
        "GALLERY_SECTIONS": ",".join(settings.gallery_sections),
        "GALLERY_SORT": settings.gallery_sort,
        "GALLERY_WINDOW": settings.gallery_window,
        "GALLERY_PAGES": str(settings.gallery_pages),
        "MIN_NET_SCORE": str(settings.min_net_score),
        "MAX_POST_AGE_SECONDS": str(settings.max_post_age_seconds),
        "COMMENT_COOLDOWN_SECONDS": str(settings.comment_cooldown_seconds),
        "AHASH_MAX_DISTANCE": str(settings.ahash_max_distance),
        "DHASH_H_MAX_DISTANCE": str(settings.dhash_h_max_distance),
        "DHASH_V_MAX_DISTANCE": str(settings.dhash_v_max_distance),
        "BITCOUNT_PADDING": str(settings.bitcount_padding),
        "LOG_LEVEL": settings.log_level,
        "LOG_RETENTION_DAYS": str(settings.log_retention_days),
        "WEEKLY_POST_ENABLED": str(settings.weekly_post_enabled).lower(),
        "WEEKLY_POST_DAY": settings.weekly_post_day,
        "WEEKLY_POST_TIME": settings.weekly_post_time,
        "WEEKLY_POST_TAGS": settings.weekly_post_tags,
    }
    return mapping[key]


def _validate_setting(spec: dict[str, Any], raw: str | None) -> str:
    kind = spec["type"]
    if kind == "bool":
        return "true" if raw in {"1", "true", "on", "yes"} else "false"
    value = (raw or "").strip()
    if kind == "int":
        parsed = int(value)
        if parsed < spec["min"] or parsed > spec["max"]:
            raise ValueError(f"must be between {spec['min']} and {spec['max']}")
        return str(parsed)
    if kind == "choice":
        if value not in spec["choices"]:
            raise ValueError("invalid choice")
        return value
    if kind == "sections":
        parts = [p.strip() for p in value.split(",") if p.strip()]
        if not parts or any(p not in {"top", "hot", "user"} for p in parts):
            raise ValueError("use a comma-separated subset of user, hot, top")
        return ",".join(dict.fromkeys(parts))
    if kind == "text":
        if spec.get("pattern") and not re.match(spec["pattern"], value):
            raise ValueError(spec.get("pattern_help", "invalid format"))
        return value
    raise ValueError("unsupported setting type")


PER_PAGE_CHOICES = [25, 50, 100, 250, 500, 1000]

# Repost-detection reasons, as friendly filter labels for the /reposts page.
# The empty key is the default "all" view. Any value not in here is ignored so
# the query param can't inject an arbitrary reason string.
REPOST_REASON_FILTERS = [
    ("", "All detections"),
    ("comment-posted", "Commented"),
    ("same-account", "Same account"),
    ("below-min-score", "Below min score"),
    ("dry-run", "Dry run"),
    ("backfill-no-comments", "Backfill"),
]
REPOST_REASON_KEYS = {key for key, _ in REPOST_REASON_FILTERS if key}


def _pagination(total: int, page_arg: Any, per_page_arg: Any,
                default_per: int = 100, max_per: int = 1000) -> dict[str, int]:
    try:
        per_page = int(per_page_arg)
    except (TypeError, ValueError):
        per_page = default_per
    per_page = max(1, min(max_per, per_page))
    try:
        page = int(page_arg)
    except (TypeError, ValueError):
        page = 1
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    return {
        "page": page, "per_page": per_page, "pages": pages,
        "offset": (page - 1) * per_page, "total": total,
        "start": (page - 1) * per_page + 1 if total else 0,
        "end": min(page * per_page, total),
    }


def _tail(path: str | Path, max_bytes: int = 120_000) -> str:
    p = Path(path)
    if not p.exists() or not p.is_file():
        # A rotated/compressed job log: fall back to its .gz archive so the
        # viewer keeps working after compression. gzip is not seekable, so we
        # decompress fully and keep the tail (job logs are viewed on demand).
        import gzip

        gz = Path(str(p) + ".gz")
        if gz.exists():
            try:
                with gzip.open(gz, "rb") as fh:
                    data = fh.read()
            except OSError:
                return ""
            return data[-max_bytes:].decode("utf-8", errors="replace")
        return ""
    with p.open("rb") as fh:
        try:
            fh.seek(-max_bytes, os.SEEK_END)
        except OSError:
            fh.seek(0)
        data = fh.read()
    return data.decode("utf-8", errors="replace")


def create_app() -> Flask:
    settings = load_settings()
    project_root = Path(__file__).resolve().parent.parent
    app = Flask(__name__)
    app.config["SECRET_KEY"] = secrets.token_hex(32)
    app.config["CSRF_TOKEN"] = secrets.token_urlsafe(32)
    manager = JobManager(project_root, settings.database_path, settings.web_job_log_dir)
    # Reconcile persisted worker state immediately. Long-running workers survive
    # a web-console restart by design; dead/reused PIDs are cleared before the
    # first page is served.
    manager.refresh()

    app.jinja_env.filters["epoch"] = _epoch
    app.jinja_env.filters["fromjson"] = _json

    @app.context_processor
    def globals_for_templates():
        from .service import image_similarity_stars

        current = load_settings()

        def img_stars(ad, hd, vd):
            return image_similarity_stars(
                ad, hd, vd,
                current.ahash_max_distance, current.dhash_h_max_distance, current.dhash_v_max_distance,
            )

        return {
            "csrf_token": app.config["CSRF_TOKEN"],
            "bot_username": current.bot_username,
            "img_stars": img_stars,
        }

    @app.before_request
    def protect_posts():
        if request.method == "POST":
            token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
            if not secrets.compare_digest(token or "", app.config["CSRF_TOKEN"]):
                abort(403)

    def open_db() -> RepostDatabase:
        return RepostDatabase(load_settings().database_path)

    @app.get("/")
    def dashboard():
        manager.refresh()
        current = load_settings()
        db = open_db()
        try:
            stats = db.dashboard_stats()
            api = db.api_summary()
            recent_reposts = db.list_repost_events(12)
            recent_jobs = db.list_web_jobs(8)
            bot_job = db.active_web_job("bot")
            backfill_job = db.active_web_job("backfill")
            # The durable poller usually runs as the systemd service, which is not
            # a console job. Detect it via the singleton lock so the dashboard
            # shows "running" instead of "stopped" when the service is polling.
            poller_pid = None if bot_job else running_pid(lock_path(project_root))
            db_size = 0
            if current.database_path != ":memory:" and Path(current.database_path).exists():
                db_size = Path(current.database_path).stat().st_size
            return render_template(
                "dashboard.html", settings=current, stats=stats, api=api,
                recent_reposts=recent_reposts, recent_jobs=recent_jobs,
                bot_job=bot_job, backfill_job=backfill_job, poller_pid=poller_pid,
                db_size=db_size,
            )
        finally:
            db.close()

    @app.post("/bot/start")
    def bot_start():
        # A poller may already be running outside the console's job table (most
        # commonly the systemd bot service). Starting another one here would
        # double-comment on every post, so refuse if the singleton lock is held.
        pid = running_pid(lock_path(project_root))
        if pid is not None:
            flash(
                f"A live poller is already running (pid {pid}); not starting a "
                "duplicate. Stop it first if you want to restart it.",
                "warn",
            )
            return redirect(url_for("dashboard"))
        try:
            job_id = manager.start("bot", {}, ["run"])
            flash(f"Started live bot as job #{job_id}.", "ok")
        except RuntimeError as exc:
            flash(str(exc), "warn")
        return redirect(url_for("dashboard"))

    @app.post("/jobs/<int:job_id>/stop")
    def job_stop(job_id: int):
        try:
            manager.stop(job_id)
            flash(f"Stopped job #{job_id}.", "ok")
        except KeyError:
            flash(f"Job #{job_id} does not exist.", "warn")
        return redirect(request.referrer or url_for("dashboard"))

    @app.route("/backfill", methods=["GET", "POST"])
    def backfill():
        manager.refresh()
        if request.method == "POST":
            try:
                section = request.form.get("section", "top")
                window = request.form.get("window", "all")
                sort = request.form.get("sort", "viral")
                pages = int(request.form.get("pages", "25"))
                start_page = int(request.form.get("start_page", "0"))
                if section not in {"top", "hot", "user"}:
                    raise ValueError("invalid section")
                if window not in {"day", "week", "month", "year", "all"}:
                    raise ValueError("invalid window")
                if not 1 <= pages <= 100000:
                    raise ValueError("pages must be between 1 and 100000")
                if start_page < 0:
                    raise ValueError("start page cannot be negative")
                args = {"section": section, "sort": sort, "window": window, "pages": pages, "start_page": start_page}
                command = [
                    "backfill", "--section", section, "--sort", sort, "--window", window,
                    "--pages", str(pages), "--start-page", str(start_page),
                ]
                job_id = manager.start("backfill", args, command)
                flash(f"Started backfill job #{job_id}.", "ok")
                return redirect(url_for("backfill"))
            except (ValueError, RuntimeError) as exc:
                flash(str(exc), "warn")

        db = open_db()
        try:
            jobs = db.list_web_jobs(30, "backfill")
            active = db.active_web_job("backfill")
            return render_template("backfill.html", jobs=jobs, active=active)
        finally:
            db.close()

    @app.route("/blocklist", methods=["GET", "POST"])
    def blocklist():
        db = open_db()
        try:
            if request.method == "POST":
                kind = request.form.get("kind")
                value = (request.form.get("value") or "").strip()
                try:
                    if kind == "username":
                        if not USERNAME_RE.fullmatch(value):
                            raise ValueError("Username must be 1-30 letters, numbers, underscore, or dash.")
                        added = db.block_username(value)
                        flash(("Blocked " if added else "Already blocked: ") + value, "ok")
                    elif kind == "account_id":
                        account_id = int(value)
                        if account_id <= 0:
                            raise ValueError("Account ID must be positive.")
                        added = db.block_account(account_id)
                        flash(("Blocked account ID " if added else "Already blocked account ID ") + str(account_id), "ok")
                    else:
                        raise ValueError("Unknown block type.")
                except ValueError as exc:
                    flash(str(exc), "warn")
                return redirect(url_for("blocklist"))
            return render_template(
                "blocklist.html",
                users=db.list_blocked_users(), accounts=db.list_blocked_accounts(),
            )
        finally:
            db.close()

    @app.post("/blocklist/user/<username>/remove")
    def blocklist_user_remove(username: str):
        db = open_db()
        try:
            db.unblock_username(username)
        finally:
            db.close()
        flash(f"Removed username block for {username}.", "ok")
        return redirect(url_for("blocklist"))

    @app.post("/blocklist/account/<int:account_id>/remove")
    def blocklist_account_remove(account_id: int):
        db = open_db()
        try:
            db.unblock_account(account_id)
        finally:
            db.close()
        flash(f"Removed account ID block {account_id}.", "ok")
        return redirect(url_for("blocklist"))

    @app.route("/settings", methods=["GET", "POST"])
    def settings_page():
        db = open_db()
        try:
            if request.method == "POST":
                errors = []
                for spec in SETTING_SPECS:
                    key = spec["key"]
                    try:
                        value = _validate_setting(spec, request.form.get(key))
                        db.set_runtime_setting(key, value)
                    except (ValueError, TypeError) as exc:
                        errors.append(f"{spec['label']}: {exc}")
                if errors:
                    flash("; ".join(errors), "warn")
                else:
                    flash("Settings saved. Restart a running bot/backfill worker to apply them.", "ok")
                return redirect(url_for("settings_page"))

            current = load_settings()
            overrides = db.get_runtime_settings()
            rows = []
            for spec in SETTING_SPECS:
                item = dict(spec)
                item["value"] = _setting_value(current, spec["key"])
                item["overridden"] = spec["key"] in overrides
                rows.append(item)
            return render_template("settings.html", setting_rows=rows)
        finally:
            db.close()

    @app.post("/settings/reset")
    def settings_reset():
        db = open_db()
        try:
            for spec in SETTING_SPECS:
                db.delete_runtime_setting(spec["key"])
        finally:
            db.close()
        flash("Runtime overrides cleared; values now come from .env/defaults.", "ok")
        return redirect(url_for("settings_page"))

    @app.get("/reposts")
    def reposts():
        db = open_db()
        try:
            reason = request.args.get("reason") or ""
            if reason not in REPOST_REASON_KEYS:
                reason = ""
            pg = _pagination(db.count_repost_events(reason or None),
                             request.args.get("page"), request.args.get("per_page"))
            events = db.list_repost_events(pg["per_page"], pg["offset"], reason or None)
            return render_template("reposts.html", events=events, pg=pg,
                                   per_page_choices=PER_PAGE_CHOICES,
                                   reason_filters=REPOST_REASON_FILTERS, reason=reason)
        finally:
            db.close()

    @app.get("/posts")
    def posts():
        db = open_db()
        try:
            pg = _pagination(db.count_posts(), request.args.get("page"), request.args.get("per_page"))
            rows = db.list_posts(pg["per_page"], pg["offset"])
            return render_template("posts.html", posts=rows, pg=pg, per_page_choices=PER_PAGE_CHOICES)
        finally:
            db.close()

    @app.get("/api-status")
    def api_status():
        db = open_db()
        try:
            return render_template("api.html", summary=db.api_summary(), events=db.recent_api_events(250))
        finally:
            db.close()

    @app.get("/logs")
    def logs():
        current = load_settings()
        db = open_db()
        try:
            jobs = db.list_web_jobs(30)
            selected = request.args.get("job", type=int)
            log_path = current.log_file
            label = "Main bot log"
            if selected:
                job = db.get_web_job(selected)
                if job and job["log_path"]:
                    log_path = job["log_path"]
                    label = f"Job #{selected} log"
            return render_template("logs.html", text=_tail(log_path), jobs=jobs, selected=selected, label=label, log_path=log_path)
        finally:
            db.close()

    @app.get("/logs/tail")
    def logs_tail():
        # Raw log text for the live auto-refresh poller in logs.html. Resolves
        # the same log path as /logs (main log by default, or a selected job's
        # log) so the front-end can refresh without a full page reload.
        current = load_settings()
        db = open_db()
        try:
            selected = request.args.get("job", type=int)
            log_path = current.log_file
            if selected:
                job = db.get_web_job(selected)
                if job and job["log_path"]:
                    log_path = job["log_path"]
            return Response(_tail(log_path), mimetype="text/plain")
        finally:
            db.close()

    @app.get("/health")
    def health():
        return {"ok": True, "bot": load_settings().bot_username}

    return app
