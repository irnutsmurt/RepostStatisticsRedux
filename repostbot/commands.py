from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .database import RepostDatabase
from .imgur import ImgurApiError, ImgurClient


LOG = logging.getLogger(__name__)
COMMAND_TYPES = {"commentmention", "commentreplygroup"}
ACTION_RE = re.compile(r"gallery/([^/]+)/comment/(\d+)")


@dataclass
class CommandStats:
    history_entries: int = 0
    actionable: int = 0
    blocked: int = 0
    already_blocked: int = 0
    replied: int = 0
    ignored: int = 0
    already_handled: int = 0
    errors: int = 0


def parse_command(text: str | None, bot_username: str | None) -> str | None:
    """Return a supported command name from an @mention comment.

    Commands are deliberately self-service. `blockme` never accepts a target
    username; the caller is always the account that gets blocked.
    """
    if not text or not bot_username or not isinstance(text, str):
        return None
    if len(text) > 10_000:
        return None

    mention = re.compile(
        rf"(?i)(?<![A-Za-z0-9_-])@{re.escape(bot_username)}(?=\s|$|[,:;.!?])"
    )
    match = mention.search(text)
    if not match:
        return None

    after = text[match.end():].lstrip(" \t\r\n,:;.!?-")
    if not after:
        return None
    verb = after.split(None, 1)[0].strip(" \t\r\n,:;.!?").lower()
    if verb in {"blockme", "help"}:
        return verb
    return None


class CommandPoller:
    def __init__(self, settings: Settings, client: ImgurClient, db: RepostDatabase):
        self.settings = settings
        self.client = client
        self.db = db

    def _mark_remote_viewed(self, notification_id: str) -> bool:
        """Best-effort clear of a command notification in Imgur itself.

        Local processed-notification storage remains the source of truth for
        deduplication.  A remote read-state failure must never cause blockme to
        execute twice or undo an already-honored opt-out.
        """
        try:
            self.client.mark_notification_viewed(notification_id)
            LOG.info("Marked Imgur notification %s as viewed", notification_id)
            return True
        except ImgurApiError as exc:
            LOG.warning("Could not mark Imgur notification %s as viewed: %s", notification_id, exc)
            return False

    @staticmethod
    def _notification_fields(notification: dict[str, Any]) -> tuple[str | None, str | None, int | None, str | None, int]:
        comment = notification.get("comment_definition") or {}
        requester = comment.get("author")
        text = comment.get("caption")
        image_id = comment.get("hash")
        parent_id = comment.get("id")
        timestamp = int(comment.get("datetime") or notification.get("updated_at") or 0)

        action = notification.get("action")
        match = ACTION_RE.search(action) if isinstance(action, str) else None
        if match:
            image_id = match.group(1)
            try:
                parent_id = int(match.group(2))
            except ValueError:
                parent_id = None

        if parent_id is not None:
            try:
                parent_id = int(parent_id)
            except (TypeError, ValueError):
                parent_id = None

        return requester, text, parent_id, image_id, timestamp

    def poll_once(self) -> CommandStats:
        stats = CommandStats()
        if not self.settings.commands_enabled:
            return stats
        if not self.settings.bot_username:
            LOG.warning("COMMANDS_ENABLED=true but BOT_USERNAME is empty; command polling skipped")
            return stats
        if not self.client.access_token:
            LOG.warning("COMMANDS_ENABLED=true but IMGUR_ACCESS_TOKEN is empty; command polling skipped")
            return stats

        try:
            history = self.client.account_history(self.settings.bot_username)
        except ImgurApiError as exc:
            stats.errors += 1
            LOG.error("Failed fetching Imgur mention history: %s", exc)
            return stats

        stats.history_entries = len(history)
        now = int(time.time())
        candidates: list[dict[str, Any]] = []
        for notification in history:
            if not isinstance(notification, dict):
                stats.ignored += 1
                continue
            notification_id = notification.get("id")
            if notification_id is None:
                stats.ignored += 1
                continue
            notification_id = str(notification_id)
            if notification.get("type") not in COMMAND_TYPES:
                self.db.mark_notification_processed(notification_id)
                stats.ignored += 1
                continue

            # Mirror the known-working bot: clear command-bearing notifications
            # through POST /notification/{id}. Do this before the local dedupe
            # check so an entry processed by an older build can still be cleared
            # remotely after upgrading. Only unseen entries need the API call.
            if str(notification.get("state") or "").lower() == "unseen":
                self._mark_remote_viewed(notification_id)

            if self.db.notification_processed(notification_id):
                stats.already_handled += 1
                continue

            _, _, _, _, timestamp = self._notification_fields(notification)
            if timestamp and now - timestamp > self.settings.command_max_age_seconds:
                self.db.mark_notification_processed(notification_id)
                stats.ignored += 1
                continue
            candidates.append(notification)

        candidates.sort(key=lambda n: int(n.get("updated_at") or 0))
        stats.actionable = len(candidates)

        for notification in candidates:
            notification_id = str(notification["id"])
            requester, text, parent_id, image_id, _ = self._notification_fields(notification)

            if not requester or not isinstance(requester, str) or not text or not parent_id or not image_id:
                self.db.mark_notification_processed(notification_id)
                stats.ignored += 1
                continue
            requester = requester.strip()
            if not requester or requester.lower() == self.settings.bot_username.lower():
                self.db.mark_notification_processed(notification_id)
                stats.ignored += 1
                continue

            command = parse_command(text, self.settings.bot_username)
            if command is None:
                self.db.mark_notification_processed(notification_id)
                stats.ignored += 1
                continue

            if command == "help":
                # DRY_RUN applies to unsolicited repost-statistics comments, not
                # explicit commands. A user who addresses the bot should get the
                # requested response even while repost commenting is in dry-run.
                try:
                    reply_id = self.client.create_comment(
                        image_id,
                        f"To stop my repost comments on your posts: @{self.settings.bot_username} blockme",
                        parent_id=parent_id,
                    )
                    stats.replied += 1
                    LOG.info("Replied to help command from %s with comment %s", requester, reply_id)
                except ImgurApiError as exc:
                    stats.errors += 1
                    LOG.error("Failed replying to help command from %s: %s", requester, exc)
                self.db.mark_notification_processed(notification_id)
                continue

            # `blockme` is intentionally self-service only. Never parse or trust
            # a target supplied in the body; the notification author is blocked.
            newly_blocked = self.db.block_username(requester)
            if newly_blocked:
                stats.blocked += 1
                LOG.info("Blocked Imgur user %s via blockme command", requester)
                # Also persist the stable numeric account ID when Imgur will
                # give it to us. The username block remains authoritative if
                # this best-effort lookup fails.
                try:
                    account = self.client.account(requester)
                    account_id = account.get("id") if isinstance(account, dict) else None
                    if account_id not in (None, 0, "0", ""):
                        self.db.block_account(int(account_id))
                        LOG.info("Also blocked stable Imgur account ID %s for %s", account_id, requester)
                except (ImgurApiError, TypeError, ValueError) as exc:
                    LOG.warning("Could not resolve numeric Imgur account ID for %s: %s", requester, exc)
            else:
                stats.already_blocked += 1
                LOG.info("Imgur user %s was already blocked", requester)

            # Command replies stay live in DRY_RUN. Dry-run only suppresses the
            # bot's automatic repost-statistics comments; blockme is an explicit
            # user request and should confirm that the opt-out was honored.
            reply = (
                self.settings.blockme_reply
                if newly_blocked
                else self.settings.blockme_already_blocked_reply
            )
            try:
                reply_id = self.client.create_comment(image_id, reply, parent_id=parent_id)
                stats.replied += 1
                LOG.info("Replied to blockme command from %s with comment %s", requester, reply_id)
            except ImgurApiError as exc:
                # The privacy request has already been honored locally. Do not
                # undo it just because the confirmation reply failed.
                stats.errors += 1
                LOG.error("User %s was blocked, but confirmation reply failed: %s", requester, exc)

            self.db.mark_notification_processed(notification_id)

        # Per-poll heartbeat so command activity is visible in the live log even
        # when nothing was actionable (e.g. every mention was already handled).
        LOG.info(
            "Command poll: history=%d actionable=%d blocked=%d already_blocked=%d "
            "replied=%d already_handled=%d ignored=%d errors=%d",
            stats.history_entries, stats.actionable, stats.blocked,
            stats.already_blocked, stats.replied, stats.already_handled,
            stats.ignored, stats.errors,
        )
        return stats
