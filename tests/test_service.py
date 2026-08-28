from dataclasses import replace

from repostbot.config import Settings
from repostbot.models import GalleryPost, HistoricalPost
from repostbot.service import (
    build_comments,
    comment_decision,
    normalized_levenshtein,
    should_comment,
)


class DummyDb:
    def is_blocked(self, account_id, account_url=None):
        return False


def settings(**updates):
    base = Settings(
        client_id="x", access_token="y", client_secret=None, refresh_token=None,
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


def test_levenshtein():
    assert normalized_levenshtein("same", "same") == 0
    assert normalized_levenshtein("", "abc") == 1


def test_comment_text_contains_last_seen_link():
    # Single-image post, seen once -> "Last seen ... " with no Image # prefix.
    new = GalleryPost("new", "new title", 200, 2, "b", "url", False, 1, 1, 100, 0)
    old = HistoricalPost("old", "old title", 100, 1, "a", "oldurl", True)
    text = build_comments(new, [old])[0]
    assert text.startswith("Last seen ")
    assert "https://imgur.com/gallery/old" in text
    assert "Title similarity:" in text


def test_comment_multi_image_album_prefixes_image_number():
    # Post that is image #11 of a 20-image album, seen once.
    new = GalleryPost("img", "t", 200, 2, "b", "url", False, 1, 1, 100, 0,
                      gallery_id="ALB", image_index=11, image_count=20)
    old = HistoricalPost("old", "t", 100, 1, "a", "oldurl", True, gallery_id="OLDG")
    text = build_comments(new, [old])[0]
    assert text.startswith("Image #11 last seen ")
    assert "https://imgur.com/gallery/OLDG" in text


def test_comment_multiple_sightings_uses_seen_count():
    new = GalleryPost("img", "t", 200, 2, "b", "url", False, 1, 1, 300, 0)
    d1 = HistoricalPost("g1", "t", 100, 1, "a", "u1", True, gallery_id="G1")
    d2 = HistoricalPost("g2", "t", 200, 1, "a", "u2", True, gallery_id="G2")
    text = build_comments(new, [d1, d2])[0]
    assert text.startswith("Seen 2 times since ")
    # References the earliest sighting's gallery.
    assert "https://imgur.com/gallery/G1" in text


def test_exact_match_comments_regardless_of_age_and_title():
    # Byte-identical repost of a years-old image with a totally different title,
    # by a different account -> still comments via the exact-match short-circuit.
    post = GalleryPost("new", "a brand new unrelated caption", 1_700_000_000, 2, "bob",
                       "url", False, 1, 1, 100, 0)
    old = HistoricalPost("old", "completely different old title", 1_600_000_000, 1, "alice",
                         "oldurl", exact_match=True)
    allowed, reason = comment_decision(settings(min_net_score=5), DummyDb(), post, [old])
    assert allowed is True
    assert reason == "exact-match"


def test_fuzzy_old_different_title_still_suppressed():
    # A perceptual (non-exact) match of an old image with a different title keeps
    # the old guard: not recent, not similar title -> no comment.
    post = GalleryPost("new", "a brand new unrelated caption", 1_700_000_000, 2, "bob",
                       "url", False, 1, 1, 100, 0)
    old = HistoricalPost("old", "completely different old title", 1_600_000_000, 1, "alice",
                         "oldurl", exact_match=False, ahash_distance=2)
    allowed, reason = comment_decision(settings(min_net_score=5), DummyDb(), post, [old])
    assert allowed is False
    assert reason == "outside-comment-threshold"


def test_crawl_continues_after_gallery_page_error():
    from repostbot.imgur import ImgurApiError
    from repostbot.service import RepostService

    class FlakyClient:
        def __init__(self):
            self.pages = []

        def gallery(self, section, sort, window, page):
            self.pages.append(page)
            if page == 0:
                raise ImgurApiError("Imgur API 503: first byte timeout")
            return []

    client = FlakyClient()
    service = RepostService(None, client, None)
    stats = service.crawl(sections=("top",), sort="viral", window="all", pages=2, allow_comment=False)

    assert client.pages == [0, 1]
    assert stats.errors == 1


class _EmptyClient:
    def __init__(self):
        self.pages = []

    def gallery(self, section, sort, window, page):
        self.pages.append(page)
        return []


def test_crawl_early_stops_after_stale_pages():
    from repostbot.service import RepostService

    client = _EmptyClient()
    service = RepostService(None, client, None)
    # Every page is empty (new=0). With a 3-page stale limit it stops after
    # pages 0, 1, 2 instead of crawling all 100.
    service.crawl(sections=("user",), sort="time", window="all", pages=100,
                  allow_comment=False, stop_after_stale_pages=3)
    assert client.pages == [0, 1, 2]


def test_crawl_no_early_stop_when_disabled():
    from repostbot.service import RepostService

    client = _EmptyClient()
    service = RepostService(None, client, None)
    service.crawl(sections=("user",), sort="time", window="all", pages=5,
                  allow_comment=False, stop_after_stale_pages=0)
    assert client.pages == [0, 1, 2, 3, 4]
