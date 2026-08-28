import time
from dataclasses import replace

from repostbot.commands import CommandPoller, parse_command
from repostbot.config import Settings
from repostbot.database import RepostDatabase


def settings(**updates):
    base = Settings(
        client_id="x", access_token="token", client_secret=None, refresh_token=None,
        dry_run=False, database_path=":memory:", log_level="INFO", poll_interval_seconds=300,
        gallery_sections=("user",), gallery_sort="viral", gallery_window="day", gallery_pages=1,
        min_net_score=40, max_post_age_seconds=9999999999, comment_cooldown_seconds=0,
        ahash_max_distance=3, dhash_h_max_distance=10, dhash_v_max_distance=10,
        bitcount_padding=22, ignore_same_account=True,
        bot_username="RepostStatisticsRedux", commands_enabled=True,
        command_poll_interval_seconds=60, command_max_age_seconds=600,
        blockme_reply="Blocked.", blockme_already_blocked_reply="Already blocked.",
    )
    return replace(base, **updates)


def test_parse_blockme_case_insensitive():
    assert parse_command("hey @repoststatisticsredux BLOCKME", "RepostStatisticsRedux") == "blockme"


def test_parse_does_not_accept_targeted_block():
    # The parser recognizes blockme, but the implementation never consumes a
    # target token; only the notification author can be blocked.
    assert parse_command("@RepostStatisticsRedux blockme SomebodyElse", "RepostStatisticsRedux") == "blockme"


def test_blockme_blocks_requester_and_is_restart_safe():
    now = int(time.time())
    history = [{
        "id": "notification-1",
        "state": "unseen",
        "updated_at": now,
        "type": "commentmention",
        "action": "imgur://imgur.com/gallery/abc123/comment/456",
        "comment_definition": {
            "hash": "abc123",
            "author": "Alice",
            "id": 456,
            "caption": "@RepostStatisticsRedux blockme Bob",
            "datetime": now,
        },
    }]

    class FakeClient:
        access_token = "token"
        def __init__(self):
            self.replies = []
            self.viewed = []
        def account_history(self, username):
            return history
        def mark_notification_viewed(self, notification_id):
            self.viewed.append(str(notification_id))
            return True
        def account(self, username):
            assert username == "Alice"
            return {"id": 12345, "url": "Alice"}
        def create_comment(self, image_id, text, parent_id=None):
            self.replies.append((image_id, text, parent_id))
            return 999

    db = RepostDatabase(":memory:")
    client = FakeClient()
    poller = CommandPoller(settings(), client, db)

    first = poller.poll_once()
    second = poller.poll_once()

    assert first.blocked == 1
    assert db.is_blocked(None, "alice") is True
    assert db.is_blocked(12345, None) is True
    assert db.is_blocked(None, "Bob") is False
    assert len(client.replies) == 1
    assert client.viewed == ["notification-1", "notification-1"]
    assert second.blocked == 0
    assert len(client.replies) == 1


def test_blockme_in_dry_run_honors_opt_out_and_replies():
    now = int(time.time())
    history = [{
        "id": "notification-2",
        "state": "unseen",
        "updated_at": now,
        "type": "commentmention",
        "action": "imgur://imgur.com/gallery/abc123/comment/456",
        "comment_definition": {
            "author": "Alice",
            "caption": "@RepostStatisticsRedux blockme",
            "datetime": now,
        },
    }]

    class FakeClient:
        access_token = "token"
        def __init__(self):
            self.replies = []
            self.viewed = []
        def account_history(self, username): return history
        def mark_notification_viewed(self, notification_id):
            self.viewed.append(str(notification_id))
            return True
        def account(self, username): return {"id": 24680, "url": username}
        def create_comment(self, image_id, text, parent_id=None):
            self.replies.append((image_id, text, parent_id))
            return 999

    db = RepostDatabase(":memory:")
    client = FakeClient()
    poller = CommandPoller(settings(dry_run=True), client, db)
    result = poller.poll_once()
    assert result.blocked == 1
    assert result.replied == 1
    assert db.is_blocked(None, "ALICE") is True
    assert db.is_blocked(24680, None) is True
    assert client.viewed == ["notification-2"]
    assert client.replies == [("abc123", "Blocked.", 456)]


def test_processed_unseen_notification_is_still_cleared_remotely_without_replying_twice():
    now = int(time.time())
    history = [{
        "id": "notification-3",
        "state": "unseen",
        "updated_at": now,
        "type": "commentmention",
        "action": "imgur://imgur.com/gallery/abc123/comment/789",
        "comment_definition": {
            "author": "Alice",
            "caption": "@RepostStatisticsRedux blockme",
            "datetime": now,
        },
    }]

    class FakeClient:
        access_token = "token"
        def __init__(self): self.viewed = []; self.replies = []
        def account_history(self, username): return history
        def mark_notification_viewed(self, notification_id): self.viewed.append(str(notification_id)); return True
        def account(self, username): raise AssertionError("processed notification must not execute again")
        def create_comment(self, *args, **kwargs): raise AssertionError("processed notification must not reply again")

    db = RepostDatabase(":memory:")
    db.mark_notification_processed("notification-3")
    client = FakeClient()
    result = CommandPoller(settings(dry_run=True), client, db).poll_once()
    assert result.blocked == 0
    assert result.replied == 0
    assert client.viewed == ["notification-3"]
