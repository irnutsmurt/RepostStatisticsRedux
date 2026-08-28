from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import timedelta

from .config import Settings
from .database import RepostDatabase
from .hashing import UnsupportedMediaError, signature_from_bytes
from .imgur import ImgurApiError, ImgurClient
from .models import GalleryPost, HistoricalPost


LOG = logging.getLogger(__name__)


def _fmt_ts(epoch: int | None) -> str:
    if not epoch:
        return "?"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(int(epoch)))
    except (TypeError, ValueError, OSError):
        return str(epoch)


@dataclass
class RunStats:
    scanned: int = 0
    inserted: int = 0
    known: int = 0
    skipped: int = 0
    reposts: int = 0
    comments: int = 0
    errors: int = 0


def normalized_levenshtein(left: str, right: str) -> float:
    if left == right:
        return 0.0
    if not left:
        return 1.0 if right else 0.0
    if not right:
        return 1.0
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, start=1):
        current = [i]
        for j, b in enumerate(right, start=1):
            current.append(min(
                current[j - 1] + 1,
                previous[j] + 1,
                previous[j - 1] + (a != b),
            ))
        previous = current
    return previous[-1] / max(len(left), len(right))


def human_age(seconds: int) -> str:
    seconds = max(0, int(seconds))
    units = (
        (365 * 24 * 3600, "year"),
        (30 * 24 * 3600, "month"),
        (7 * 24 * 3600, "week"),
        (24 * 3600, "day"),
        (3600, "hour"),
        (60, "minute"),
    )
    for size, label in units:
        if seconds >= size:
            count = seconds // size
            return f"{count} {label}{'' if count == 1 else 's'}"
    return f"{seconds} second{'' if seconds == 1 else 's'}"


def _stars(count: int) -> str:
    count = max(0, min(5, int(count)))
    return "★" * count + "☆" * (5 - count) + f" ({count}/5)"


def title_similarity_stars(old_title: str, new_title: str) -> str:
    distance = normalized_levenshtein(old_title.lower().strip(), new_title.lower().strip())
    stars = max(0, min(5, round((1.0 - distance) * 5)))
    return _stars(stars)


def image_similarity_stars(
    ahash_distance: int | None,
    dhash_h_distance: int | None,
    dhash_v_distance: int | None,
    ahash_max: int,
    dhash_h_max: int,
    dhash_v_max: int,
) -> str:
    """Star rating for how close an image match is.

    A byte-identical (exact) match has NULL distances and scores a perfect 5/5.
    Otherwise each perceptual distance is normalised against the threshold that
    admitted it; the worst of the three drives the score, so a match right at the
    limit reads 1/5 and a distance-zero perceptual match reads 5/5.
    """
    if ahash_distance is None or dhash_h_distance is None or dhash_v_distance is None:
        return _stars(5)
    worst = max(
        ahash_distance / max(1, ahash_max),
        dhash_h_distance / max(1, dhash_h_max),
        dhash_v_distance / max(1, dhash_v_max),
    )
    stars = max(1, min(5, round((1.0 - worst) * 4) + 1))
    return _stars(stars)


def comment_decision(settings: Settings, db: RepostDatabase, post: GalleryPost, duplicates: list[HistoricalPost]) -> tuple[bool, str]:
    if settings.dry_run:
        return False, "dry-run"
    if post.net_score < settings.min_net_score:
        return False, "below-min-score"
    if int(time.time()) >= post.timestamp + settings.max_post_age_seconds:
        return False, "post-too-old"
    if db.is_blocked(post.account_id, post.account_url):
        return False, "blocked-user"
    if not duplicates:
        return False, "no-duplicate"

    last = duplicates[-1]
    if settings.ignore_same_account and post.account_id is not None and last.account_id == post.account_id:
        return False, "same-account"

    # A byte-identical (exact SHA-256) match is a near-certain deliberate repost,
    # so comment regardless of age or title. The age/title guards below exist to
    # suppress false positives on common lookalike images; those only apply to
    # perceptual (fuzzy) matches, never to an exact file match.
    if any(d.exact_match for d in duplicates):
        return True, "exact-match"

    title = post.title.lower().strip()
    old_title = last.title.lower().strip()
    title_distance = normalized_levenshtein(title, old_title)
    is_reaction_phrase = "mrw" in title.split() or "mfw" in title.split() or title.startswith("when")
    recent_duplicate = abs(post.timestamp - last.timestamp) < 60 * 24 * 3600
    if title_distance < 0.4:
        return True, "similar-title"
    if recent_duplicate and not is_reaction_phrase:
        return True, "recent-repost"
    if is_reaction_phrase:
        return False, "reaction-title-needs-similarity"
    return False, "outside-comment-threshold"


def should_comment(settings: Settings, db: RepostDatabase, post: GalleryPost, duplicates: list[HistoricalPost]) -> bool:
    return comment_decision(settings, db, post, duplicates)[0]


def build_comment_line(post: GalleryPost, duplicates: list[HistoricalPost]) -> str:
    """One comment line for a single reposted image.

    Multi-image albums are expanded into one GalleryPost per image upstream, so
    each call describes exactly one image. When the current post is part of a
    multi-image album we prefix ``Image #N`` so a threaded reply on the album
    makes clear which image we mean; single-image posts omit that prefix.

        Image #11 last seen 1.0 years before at <url> Title similarity: ★★★★★ (5/5)
        Image #29 Seen 3 times since 1.0 years before at <url> Title similarity: ★★☆☆☆ (2/5)
        Last seen 1.0 years before at <url> Title similarity: ★★★★★ (5/5)
        Seen 3 times since 1.0 years before at <url> Title similarity: ☆☆☆☆☆ (0/5)
    """
    count = len(duplicates)
    # For a single prior sighting we point at that post; for many, we point at
    # the earliest one and count everything since.
    ref = duplicates[0] if count > 1 else duplicates[-1]
    if count > 1:
        phrase = (
            f"Seen {count} times since {human_age(post.timestamp - ref.timestamp)} before at "
            f"https://imgur.com/gallery/{ref.link_id}"
        )
    else:
        phrase = (
            f"last seen {human_age(post.timestamp - ref.timestamp)} before at "
            f"https://imgur.com/gallery/{ref.link_id}"
        )
    title_info = f"Title similarity: {title_similarity_stars(ref.title, post.title)}"

    if post.image_count and post.image_count > 1:
        return f"Image #{post.image_index} {phrase} {title_info}"
    return f"{phrase[0].upper()}{phrase[1:]} {title_info}"


def build_comments(post: GalleryPost, duplicates: list[HistoricalPost]) -> list[str]:
    if not duplicates:
        return []
    return [build_comment_line(post, duplicates)]


class RepostService:
    def __init__(self, settings: Settings, client: ImgurClient, db: RepostDatabase):
        self.settings = settings
        self.client = client
        self.db = db
        self._last_comment_time = 0.0
        # gallery_id -> id of the first comment we posted on that album this run.
        # Lets subsequent reposted images in the same album reply under one
        # thread instead of spamming the album with separate top-level comments.
        self._album_thread_parent: dict[str, int] = {}

    def process_post(self, post: GalleryPost, *, allow_comment: bool = True) -> str:
        kind = "animated" if post.animated else "image"
        LOG.info(
            "Post %s: name=%r type=%s dims=%sx%s account=%s(id=%s) score=%d(+%d/-%d) posted=%s url=%s",
            post.id, post.title or "", kind, post.width, post.height,
            post.account_url, post.account_id, post.net_score, post.ups, post.downs,
            _fmt_ts(post.timestamp), post.image_url,
        )
        if self.db.has_post(post.id):
            LOG.info("Post %s already indexed; skipping.", post.id)
            return "known"

        try:
            data = self.client.download_image(post.image_url)
        except Exception as exc:
            LOG.error("Download failed for post %s (%s): %s", post.id, post.image_url, exc)
            raise
        LOG.debug("Post %s: downloaded %d bytes from %s", post.id, len(data), post.image_url)

        try:
            signature = signature_from_bytes(data, post.animated)
        except UnsupportedMediaError:
            LOG.warning("Skipping unsupported media for Imgur post %s (%s)", post.id, post.image_url)
            return "skipped"
        if signature.animated:
            LOG.info(
                "Post %s hashes: ANIMATED -> exact-only. exact_sha256=%s",
                post.id, signature.exact_hash,
            )
        else:
            LOG.info(
                "Post %s hashes: exact_sha256=%s bit_count=%d",
                post.id, signature.exact_hash, signature.bit_count,
            )
            LOG.info(
                "Post %s perceptual: ahash=%s dhash_h=%s dhash_v=%s",
                post.id, signature.ahash, signature.dhash_h, signature.dhash_v,
            )

        duplicates = self.db.find_duplicates(
            post,
            signature,
            ahash_max_distance=self.settings.ahash_max_distance,
            dhash_h_max_distance=self.settings.dhash_h_max_distance,
            dhash_v_max_distance=self.settings.dhash_v_max_distance,
            bitcount_padding=self.settings.bitcount_padding,
        )
        self.db.insert_post(post, signature)
        LOG.info(
            "Indexed post %s name=%r type=%s (%d prior match(es))",
            post.id, post.title or "", kind, len(duplicates),
        )

        if not duplicates:
            return "inserted"

        LOG.info(
            "Repost candidate %s matched %d earlier post(s): %s",
            post.id, len(duplicates), ", ".join(d.id for d in duplicates[:5]),
        )
        allowed, reason = comment_decision(self.settings, self.db, post, duplicates)
        if not allow_comment:
            allowed = False
            reason = "backfill-no-comments"
        # Compare titles against the same post the comment references so the
        # dashboard's Title column matches the published comment.
        ref = duplicates[0] if len(duplicates) > 1 else duplicates[-1]
        self.db.record_repost_event(
            post, duplicates,
            title_similarity=title_similarity_stars(ref.title, post.title),
            decision="eligible" if allowed else "suppressed",
            reason=reason,
        )
        if not allowed:
            LOG.info("Not commenting on repost %s (reason=%s)", post.id, reason)
            return "repost"

        comments = build_comments(post, duplicates)
        gallery_id = post.link_id
        # If we've already commented on this album this run, thread under that
        # first comment; otherwise this becomes the album's top-level comment.
        top_parent = self._album_thread_parent.get(gallery_id)
        parent_id: int | None = top_parent
        first_comment_id: int | None = None
        for text in comments:
            wait = self.settings.comment_cooldown_seconds - (time.time() - self._last_comment_time)
            if wait > 0:
                LOG.debug("Comment cooldown: waiting %.1fs before commenting on %s", wait, gallery_id)
                time.sleep(wait)
            new_id = self.client.create_comment(gallery_id, text, parent_id=parent_id)
            self._last_comment_time = time.time()
            LOG.info(
                "Commented on gallery %s (image %s #%d/%d, parent=%s): %s",
                gallery_id, post.id, post.image_index, post.image_count, parent_id, text,
            )
            if first_comment_id is None:
                first_comment_id = new_id
            parent_id = new_id  # chain the second line under the first
        if top_parent is None and first_comment_id is not None:
            self._album_thread_parent[gallery_id] = first_comment_id
        self.db.mark_commented(post.id, first_comment_id)
        return "commented"

    def crawl(
        self, *, sections: tuple[str, ...], sort: str, window: str, pages: int,
        allow_comment: bool, start_page: int = 0, progress_callback=None,
        stop_after_stale_pages: int = 0,
    ) -> RunStats:
        """Crawl gallery pages. When stop_after_stale_pages > 0, a section stops
        early after that many consecutive pages yield no newly-indexed post
        (empty pages and all-already-known pages both count as stale). This lets
        a deep backfill quit once it has exhausted new content instead of
        grinding through thousands of empty/known pages. 0 disables it."""
        stats = RunStats()
        for section in sections:
            stale = 0
            for page in range(start_page, start_page + pages):
                LOG.info("Fetching gallery section=%s sort=%s window=%s page=%d", section, sort, window, page)
                try:
                    posts = self.client.gallery(section, sort, window, page)
                except ImgurApiError as exc:
                    stats.errors += 1
                    LOG.error(
                        "Failed fetching gallery section=%s sort=%s window=%s page=%d after retries: %s; continuing",
                        section, sort, window, page, exc,
                    )
                    if progress_callback is not None:
                        progress_callback(section, page, start_page + pages, stats)
                    continue
                LOG.debug("section=%s page=%d returned %d indexable post(s)", section, page, len(posts))
                pc = {"new": 0, "known": 0, "skipped": 0, "repost": 0, "commented": 0, "errors": 0}
                for post in posts:
                    stats.scanned += 1
                    try:
                        result = self.process_post(post, allow_comment=allow_comment)
                        if result == "known":
                            stats.known += 1
                            pc["known"] += 1
                        elif result == "inserted":
                            stats.inserted += 1
                            pc["new"] += 1
                        elif result == "skipped":
                            stats.skipped += 1
                            pc["skipped"] += 1
                        elif result == "repost":
                            stats.reposts += 1
                            pc["repost"] += 1
                        elif result == "commented":
                            stats.reposts += 1
                            stats.comments += 1
                            pc["commented"] += 1
                    except Exception:
                        stats.errors += 1
                        pc["errors"] += 1
                        LOG.exception("Failed processing Imgur post %s", post.id)
                LOG.info(
                    "section=%s sort=%s window=%s page=%d done: fetched=%d new=%d known=%d "
                    "repost=%d commented=%d skipped=%d errors=%d (totals: scanned=%d indexed=%d reposts=%d)",
                    section, sort, window, page, len(posts), pc["new"], pc["known"], pc["repost"],
                    pc["commented"], pc["skipped"], pc["errors"], stats.scanned, stats.inserted, stats.reposts,
                )
                if progress_callback is not None:
                    progress_callback(section, page, start_page + pages, stats)

                if pc["new"] > 0:
                    stale = 0
                elif stop_after_stale_pages:
                    stale += 1
                    if stale >= stop_after_stale_pages:
                        LOG.info(
                            "Backfill early-stop: %d consecutive pages with no new posts "
                            "(section=%s, last page=%d). Content exhausted; stopping this section.",
                            stale, section, page,
                        )
                        break
        return stats
