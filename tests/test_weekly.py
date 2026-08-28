from datetime import datetime
from types import SimpleNamespace

from repostbot.database import RepostDatabase
from repostbot.weekly import (
    build_weekly_post,
    list_templates,
    run_weekly_post,
    week_window,
)


def _settings(tmp_path, **over):
    base = dict(
        bot_username="RepostStatisticsRedux",
        weekly_template_dir=str(tmp_path),
        weekly_post_tags="",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _add_post(db, pid, *, gallery, title, posted_at, exact_hash="h", account="u"):
    db.connection.execute(
        "INSERT INTO posts (id, gallery_id, title, posted_at, account_url, image_url, "
        "animated, exact_hash, bit_count, discovered_at) "
        "VALUES (?,?,?,?,?,?,0,?,0,?)",
        (pid, gallery, title, posted_at, account, "url", exact_hash, posted_at),
    )
    db.connection.commit()


def _add_event(db, current, first, *, reason, detected_at):
    db.connection.execute(
        "INSERT INTO repost_events (current_post_id, first_post_id, last_post_id, "
        "match_count, exact_all, title_similarity, decision, reason, detected_at) "
        "VALUES (?,?,?,1,1,'x','y',?,?)",
        (current, first, first, reason, detected_at),
    )
    db.connection.commit()


def test_week_window_covers_seven_inclusive_days():
    ws, we, start, end = week_window(datetime(2026, 8, 21, 13, 0))  # a Friday
    assert (start.month, start.day) == (8, 15)
    assert (end.month, end.day) == (8, 21)
    assert datetime.fromtimestamp(ws) == datetime(2026, 8, 15, 0, 0)
    assert datetime.fromtimestamp(we) == datetime(2026, 8, 22, 0, 0)


def test_list_templates_sorted_and_excludes_examples(tmp_path):
    (tmp_path / "b.png").write_bytes(b"x")
    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "weeklystatsexample.png").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("x")
    assert list_templates(str(tmp_path)) == ["a.jpg", "b.png"]


def test_pick_weekly_template_prefers_unused_then_lru():
    db = RepostDatabase(":memory:")
    available = ["a.png", "b.png", "c.png"]
    # Nothing used yet -> alphabetical.
    assert db.pick_weekly_template(available) == "a.png"
    db.mark_weekly_template_used("a.png", 100)
    db.mark_weekly_template_used("b.png", 200)
    # c is still unused -> picked before the two used ones.
    assert db.pick_weekly_template(available) == "c.png"
    db.mark_weekly_template_used("c.png", 300)
    # All used -> least recently used (a at t=100).
    assert db.pick_weekly_template(available) == "a.png"
    db.close()


def test_build_weekly_post_counts_live_only_and_lists_commented(tmp_path):
    db = RepostDatabase(":memory:")
    now = datetime(2026, 8, 21, 13, 0)
    in_window = int(datetime(2026, 8, 20, 9, 0).timestamp())
    _add_post(db, "orig1", gallery="ORIG1", title="Hello world", posted_at=in_window - 5000)
    _add_post(db, "rep1", gallery="REP1", title="Hello world", posted_at=in_window)
    _add_post(db, "orig2", gallery="ORIG2", title="Backfilled", posted_at=in_window - 6000)
    _add_post(db, "rep2", gallery="REP2", title="Backfilled", posted_at=in_window)
    # One live commented event, one backfill event that must be ignored.
    _add_event(db, "rep1", "orig1", reason="comment-posted", detected_at=in_window)
    _add_event(db, "rep2", "orig2", reason="backfill-no-comments", detected_at=in_window)

    post = build_weekly_post(_settings(tmp_path), db, now=now)
    assert post.found == 1  # backfill excluded
    assert post.commented == 1
    assert "https://imgur.com/gallery/ORIG1" in post.description  # full scheme in links
    assert "Backfilled" not in post.description  # backfill original not listed
    db.close()


def test_run_weekly_post_idempotent_and_marks_used(tmp_path):
    (tmp_path / "only.png").write_bytes(b"img")
    db = RepostDatabase(":memory:")
    now = datetime(2026, 8, 21, 13, 0)

    calls = []

    class FakeClient:
        def create_gallery_post(self, path, title, description, tags):
            calls.append(path)
            return {"gallery_id": "GAL", "image_id": "IMG",
                    "url": "https://imgur.com/gallery/GAL"}

    settings = _settings(tmp_path)
    first = run_weekly_post(settings, db, FakeClient(), now=now)
    assert first["status"] == "posted"
    assert len(calls) == 1
    # Second run in the same week is a no-op.
    second = run_weekly_post(settings, db, FakeClient(), now=now)
    assert second["status"] == "skipped"
    assert len(calls) == 1
    # Template was recorded as used.
    assert db.pick_weekly_template(["only.png"]) == "only.png"
    row = db.connection.execute(
        "SELECT use_count FROM weekly_templates WHERE filename='only.png'"
    ).fetchone()
    assert row["use_count"] == 1
    db.close()
