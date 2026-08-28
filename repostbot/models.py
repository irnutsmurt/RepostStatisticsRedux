from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GalleryPost:
    """A single indexable image.

    Modern Imgur wraps almost every gallery post as an album, so one gallery
    entry can expand into several of these (one per image). ``id`` is the image
    id (primary key / dedup unit); ``gallery_id`` is the post/album id used for
    the public link and as the comment target. ``image_index``/``image_count``
    give the image's 1-based position within its album (both 1 for a bare post
    or a single-image album).
    """

    id: str
    title: str
    timestamp: int
    account_id: int | None
    account_url: str | None
    image_url: str
    animated: bool
    width: int | None
    height: int | None
    ups: int
    downs: int
    gallery_id: str = ""
    image_index: int = 1
    image_count: int = 1

    @property
    def net_score(self) -> int:
        return self.ups - self.downs

    @property
    def link_id(self) -> str:
        """Post/album id to use for links and as the comment target."""
        return self.gallery_id or self.id


@dataclass(frozen=True)
class HistoricalPost:
    id: str
    title: str
    timestamp: int
    account_id: int | None
    account_url: str | None
    image_url: str
    exact_match: bool
    ahash_distance: int | None = None
    dhash_h_distance: int | None = None
    dhash_v_distance: int | None = None
    gallery_id: str = ""
    image_index: int = 1
    image_count: int = 1

    @property
    def link_id(self) -> str:
        return self.gallery_id or self.id
