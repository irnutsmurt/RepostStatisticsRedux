from __future__ import annotations

import logging
import os
import time
from typing import Any, Iterable

import requests

from .models import GalleryPost


LOG = logging.getLogger(__name__)


class ImgurApiError(RuntimeError):
    pass


class RemovedImageError(RuntimeError):
    """Imgur served its 'image removed' placeholder instead of the real image.

    A deleted image's URL 302-redirects to i.imgur.com/removed.png. Hashing that
    placeholder makes every removed post look like a repost of every other, so
    callers should skip these rather than index them."""


class ImgurClient:
    API_BASE = "https://api.imgur.com/3"
    POST_V1_BASE = "https://api.imgur.com/post/v1"
    TOKEN_URL = "https://api.imgur.com/oauth2/token"

    def __init__(
        self,
        client_id: str,
        access_token: str | None = None,
        client_secret: str | None = None,
        refresh_token: str | None = None,
        timeout: int = 30,
        max_retries: int = 4,
        retry_backoff_seconds: float = 1.0,
        telemetry_callback=None,
        session_cookie: str | None = None,
    ):
        if not client_id:
            raise ValueError("IMGUR_CLIENT_ID is required")
        self.client_id = client_id
        self.access_token = access_token
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        # Optional browser session cookie. Only needed if the gallery-share
        # endpoint ever rejects the Bearer token; when set it is attached to the
        # gallery-post requests as a fallback alongside the OAuth header.
        self.session_cookie = session_cookie
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self.telemetry_callback = telemetry_callback
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "RepostStatisticsRedux/2.1"})

    def _headers(self, authenticated: bool) -> dict[str, str]:
        if authenticated:
            if not self.access_token:
                raise ImgurApiError("An IMGUR_ACCESS_TOKEN is required to post comments")
            return {"Authorization": f"Bearer {self.access_token}"}
        if self.access_token:
            return {"Authorization": f"Bearer {self.access_token}"}
        return {"Authorization": f"Client-ID {self.client_id}"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = False,
        retry_auth: bool = True,
        unwrap_data: bool = True,
        extra_headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Any:
        url = path if path.startswith("http") else f"{self.API_BASE}{path}"
        transient_statuses = {500, 502, 503, 504}
        auth_retry_available = retry_auth

        for attempt in range(self.max_retries + 1):
            try:
                request_headers = self._headers(authenticated)
                if extra_headers:
                    request_headers.update(extra_headers)
                response = self.session.request(
                    method,
                    url,
                    headers=request_headers,
                    timeout=self.timeout,
                    **kwargs,
                )
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise ImgurApiError(f"Imgur request failed after retries: {exc}") from exc
                delay = self.retry_backoff_seconds * (2 ** attempt)
                LOG.warning(
                    "Imgur request error (%s); retry %d/%d in %.1fs",
                    exc,
                    attempt + 1,
                    self.max_retries,
                    delay,
                )
                time.sleep(delay)
                continue

            self._emit_telemetry(method, path, response)

            if response.status_code == 401 and authenticated and auth_retry_available and self._can_refresh():
                self.refresh_access_token()
                auth_retry_available = False
                continue

            if response.status_code in transient_statuses and attempt < self.max_retries:
                delay = self.retry_backoff_seconds * (2 ** attempt)
                LOG.warning(
                    "Imgur API %d for %s %s; retry %d/%d in %.1fs",
                    response.status_code,
                    method,
                    path,
                    attempt + 1,
                    self.max_retries,
                    delay,
                )
                time.sleep(delay)
                continue

            if response.status_code == 429:
                raise ImgurApiError("Imgur API rate limit reached")

            try:
                payload = response.json()
            except ValueError:
                payload = None

            if not response.ok:
                if isinstance(payload, dict):
                    detail = payload.get("data")
                else:
                    detail = " ".join(response.text[:500].split())
                raise ImgurApiError(f"Imgur API {response.status_code}: {detail}")
            if isinstance(payload, dict) and payload.get("success") is False:
                raise ImgurApiError(f"Imgur API request failed: {payload.get('data')}")
            if unwrap_data and isinstance(payload, dict) and "data" in payload:
                return payload.get("data")
            return payload

        raise ImgurApiError("Imgur request failed after retries")

    @staticmethod
    def _header_int(headers, name: str) -> int | None:
        value = headers.get(name)
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _emit_telemetry(self, method: str, path: str, response) -> None:
        if self.telemetry_callback is None:
            return
        event = {
            "method": method,
            "path": path,
            "status": response.status_code,
            "client_limit": self._header_int(response.headers, "X-RateLimit-ClientLimit"),
            "client_remaining": self._header_int(response.headers, "X-RateLimit-ClientRemaining"),
            "user_limit": self._header_int(response.headers, "X-RateLimit-UserLimit"),
            "user_remaining": self._header_int(response.headers, "X-RateLimit-UserRemaining"),
            "user_reset": self._header_int(response.headers, "X-RateLimit-UserReset"),
        }
        try:
            self.telemetry_callback(event)
        except Exception:
            LOG.exception("Imgur telemetry callback failed")

    def _can_refresh(self) -> bool:
        return bool(self.client_secret and self.refresh_token)

    def refresh_access_token(self) -> None:
        if not self._can_refresh():
            raise ImgurApiError("Cannot refresh token without IMGUR_CLIENT_SECRET and IMGUR_REFRESH_TOKEN")
        response = self.session.post(
            self.TOKEN_URL,
            data={
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        self.access_token = payload["access_token"]
        self.refresh_token = payload.get("refresh_token", self.refresh_token)
        LOG.info("Refreshed Imgur access token for account %s", payload.get("account_username", "unknown"))

    def gallery(self, section: str, sort: str, window: str, page: int) -> list[GalleryPost]:
        data = self._request("GET", f"/gallery/{section}/{sort}/{window}/{page}")
        raw = data or []
        posts: list[GalleryPost] = []
        from_albums = bare = empty = 0
        for item in raw:
            images = self._expand_gallery_item(item)
            if not images:
                empty += 1
                LOG.debug(
                    "  skip %s: no extractable media (is_album=%s images_count=%s) title=%r",
                    item.get("id"), item.get("is_album"), item.get("images_count"),
                    str(item.get("title") or "")[:60],
                )
                continue
            if item.get("is_album"):
                from_albums += 1
            else:
                bare += 1
            for post in images:
                if post.image_count > 1:
                    LOG.debug(
                        "  read %s: album %s #%d/%d animated=%s url=%s",
                        post.id, post.gallery_id, post.image_index, post.image_count,
                        post.animated, post.image_url,
                    )
                else:
                    LOG.debug(
                        "  read %s: title=%r animated=%s score=%d url=%s",
                        post.id, (post.title or "")[:60], post.animated, post.net_score, post.image_url,
                    )
                posts.append(post)
        LOG.info(
            "gallery %s/%s/%s/%d: %d raw item(s) -> %d image(s) indexable "
            "(from %d album(s) + %d bare post(s); skipped %d empty)",
            section, sort, window, page, len(raw), len(posts), from_albums, bare, empty,
        )
        return posts

    @classmethod
    def _expand_gallery_item(cls, item: dict[str, Any]) -> list[GalleryPost]:
        """Expand a gallery entry into one GalleryPost per contained image.

        Modern Imgur marks nearly every post as ``is_album`` (even single
        images), carrying the real media inline under ``images``. We index each
        image individually so a repost can be matched to its exact position in
        an album.
        """
        gallery_id = item.get("id")
        if not gallery_id:
            return []
        title = str(item.get("title") or "")
        account_id = int(item["account_id"]) if item.get("account_id") not in (None, 0) else None
        account_url = item.get("account_url")
        # Album-level votes/time; individual images don't carry their own.
        ups = int(item.get("ups") or 0)
        downs = int(item.get("downs") or 0)
        posted_at = int(item.get("datetime") or 0)

        if item.get("is_album"):
            images = [img for img in (item.get("images") or []) if isinstance(img, dict)]
            count = len(images)
            out: list[GalleryPost] = []
            for idx, img in enumerate(images, start=1):
                post = cls._build_image_post(
                    img, gallery_id=str(gallery_id), title=title, account_id=account_id,
                    account_url=account_url, ups=ups, downs=downs, posted_at=posted_at,
                    index=idx, count=count,
                )
                if post is not None:
                    out.append(post)
            return out

        # Bare (non-album) gallery entry: the media lives on the item itself.
        post = cls._build_image_post(
            item, gallery_id=str(gallery_id), title=title, account_id=account_id,
            account_url=account_url, ups=ups, downs=downs, posted_at=posted_at,
            index=1, count=1,
        )
        return [post] if post is not None else []

    @staticmethod
    def _build_image_post(
        media: dict[str, Any], *, gallery_id: str, title: str, account_id: int | None,
        account_url: str | None, ups: int, downs: int, posted_at: int, index: int, count: int,
    ) -> GalleryPost | None:
        image_id = media.get("id")
        animated = bool(media.get("animated", False))
        # Animated media is served as .gifv/.mp4; prefer the direct MP4 so we
        # hash the payload rather than an HTML page.
        link = media.get("mp4") if animated and media.get("mp4") else media.get("link")
        if not image_id or not link:
            return None
        return GalleryPost(
            id=str(image_id),
            title=title,
            timestamp=int(media.get("datetime") or posted_at or 0),
            account_id=account_id,
            account_url=account_url,
            image_url=str(link),
            animated=animated,
            width=int(media["width"]) if media.get("width") is not None else None,
            height=int(media["height"]) if media.get("height") is not None else None,
            ups=ups,
            downs=downs,
            gallery_id=gallery_id,
            image_index=index,
            image_count=count,
        )

    def account(self, username: str) -> dict[str, Any]:
        data = self._request("GET", f"/account/{username}")
        return data if isinstance(data, dict) else {}

    def authenticated_account(self) -> dict[str, Any]:
        """Return the account attached to the configured OAuth access token."""
        data = self._request("GET", "/account/me", authenticated=True)
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _history_items(payload: Any) -> list[dict[str, Any]] | None:
        """Extract a history/notification list without assuming one wrapper shape.

        None means the response did not contain a recognizable list. An empty
        list means the endpoint returned a valid list with no entries.
        """
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return None

        containers: list[Any] = [payload]
        if "data" in payload:
            containers.append(payload.get("data"))
        for container in containers:
            if isinstance(container, list):
                return [item for item in container if isinstance(item, dict)]
            if not isinstance(container, dict):
                continue
            for key in ("items", "history", "notifications", "replies", "events"):
                value = container.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return None

    @staticmethod
    def _history_shape(payload: Any) -> str:
        if payload is None:
            return "null"
        if isinstance(payload, list):
            return f"list[{len(payload)}]"
        if isinstance(payload, dict):
            keys = ",".join(sorted(str(k) for k in payload.keys())[:12])
            data = payload.get("data", "<missing>")
            return f"dict(keys={keys}; data={type(data).__name__})"
        return type(payload).__name__

    def account_history_diagnostics(self, username: str) -> list[dict[str, Any]]:
        """Probe the authenticated Imgur history source without exposing bodies.

        Imgur's web client uses the literal /larynx/history route. ``larynx`` is
        an internal route name, not an account username; the bearer token scopes
        the response to the authenticated account.
        """
        probes = [
            ("larynx-history", "/larynx/history", {}),
            # Keep the public v3 replies endpoint only as a diagnostic comparison.
            # It is not the command source because it does not reliably include
            # commentmention events.
            (
                "v3-reply-notifications",
                f"/account/{username}/notifications/replies",
                {"params": {"new": "false"}},
            ),
        ]
        results: list[dict[str, Any]] = []
        for name, path, kwargs in probes:
            try:
                payload = self._request(
                    "GET", path, authenticated=True, unwrap_data=False, **kwargs
                )
                items = self._history_items(payload)
                results.append({
                    "name": name,
                    "path": path,
                    "ok": True,
                    "shape": self._history_shape(payload),
                    "count": None if items is None else len(items),
                    "types": sorted({
                        str(item.get("type"))
                        for item in (items or [])
                        if item.get("type") is not None
                    })[:20],
                })
            except ImgurApiError as exc:
                results.append({
                    "name": name, "path": path, "ok": False,
                    "shape": None, "count": None, "types": [],
                    "error": str(exc)[:300],
                })
        return results

    def account_history(self, username: str) -> list[dict[str, Any]]:
        """Return command-bearing account history for the OAuth identity.

        ``username`` is used only to verify that the configured bearer token
        belongs to BOT_USERNAME. The history route itself is always the literal
        ``/larynx/history`` endpoint used by Imgur's web client.
        """
        me = self.authenticated_account()
        authenticated_username = str(me.get("url") or me.get("username") or "").strip()
        if authenticated_username and authenticated_username.lower() != username.lower():
            raise ImgurApiError(
                "IMGUR_ACCESS_TOKEN belongs to "
                f"{authenticated_username!r}, not BOT_USERNAME {username!r}"
            )

        payload = self._request(
            "GET", "/larynx/history", authenticated=True, unwrap_data=False
        )
        items = self._history_items(payload)
        if items is None:
            LOG.warning(
                "Unexpected Imgur /larynx/history response shape: %s",
                self._history_shape(payload),
            )
            return []
        if items:
            LOG.info(
                "Imgur command history source larynx-history returned %d entries",
                len(items),
            )
        return items

    def download_image(self, url: str) -> bytes:
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        # A removed image 302-redirects to i.imgur.com/removed.png. Detect it by
        # the final URL so we never index the placeholder as if it were content.
        final = str(response.url or "")
        leaf = final.rsplit("/", 1)[-1].split("?", 1)[0].lower()
        if leaf == "removed.png" and "imgur.com" in final.lower():
            raise RemovedImageError(f"Imgur served the removed-image placeholder for {url}")
        return response.content

    def mark_notification_viewed(self, notification_id: str | int) -> Any:
        """Best-effort mark of an Imgur notification/history entry as viewed.

        This mirrors the working web-client path used by the user's other bot:
        POST /3/notification/{id}. History entries are still deduplicated
        locally because Imgur does not guarantee that /larynx/history state
        changes immediately after this call.
        """
        if notification_id in (None, ""):
            raise ImgurApiError("notification_id is required")
        return self._request(
            "POST", f"/notification/{notification_id}", authenticated=True
        )

    def create_comment(self, gallery_id: str, text: str, parent_id: int | None = None) -> int | None:
        # Match the known-working Imgur v3 reply request: JSON body with
        # image_id, parent_id and comment.  requests supplies application/json.
        body: dict[str, str | int] = {"image_id": gallery_id, "comment": text}
        if parent_id is not None:
            body["parent_id"] = int(parent_id)
        data = self._request("POST", "/comment", authenticated=True, json=body)
        if isinstance(data, dict):
            value = data.get("id")
            return int(value) if value is not None else None
        return int(data) if isinstance(data, int) else None

    def _gallery_headers(self) -> dict[str, str] | None:
        # Attach the browser session cookie only if one was configured. Imgur's
        # newer post/v1 gallery-share endpoint historically wanted it; with a
        # valid Bearer token it is usually unnecessary, so this stays optional.
        if not self.session_cookie:
            return None
        return {"Cookie": self.session_cookie, "Referer": "https://imgur.com/"}

    def create_gallery_post(
        self, image_path: str, title: str, description: str, tags: Iterable[str] = (),
    ) -> dict[str, str]:
        """Publish a single image to the Imgur gallery with a title/description.

        Ports the known-working four-step flow: create an album, upload the
        image into it, share the album to the public gallery, then set the
        description on the image. Returns the gallery id, image id, and link.
        """
        params = {"client_id": self.client_id}
        extra = self._gallery_headers()
        tag_list = [t.strip() for t in tags if str(t).strip()]

        album = self._request(
            "POST", "/album", authenticated=True, params=params,
            extra_headers=extra, json={},
        )
        album_hash = album["id"] if isinstance(album, dict) else album
        LOG.info("Weekly post: created album %s", album_hash)

        name = os.path.basename(image_path)
        with open(image_path, "rb") as fh:
            uploaded = self._request(
                "POST", "/upload", authenticated=True, params=params,
                extra_headers=extra,
                files={"image": (name, fh)},
                data={"type": "file", "name": name, "album": album_hash},
            )
        image_hash = uploaded["id"] if isinstance(uploaded, dict) else uploaded
        LOG.info("Weekly post: uploaded image %s into album %s", image_hash, album_hash)

        self._request(
            "POST", f"{self.POST_V1_BASE}/posts/{album_hash}/share",
            authenticated=True, params=params, extra_headers=extra,
            unwrap_data=False,
            json={
                "title": title,
                "terms": True,
                "mature": False,
                "tags": tag_list,
                "is_album": True,
                "subtype": "desktop",
            },
        )
        LOG.info("Weekly post: shared album %s to the gallery", album_hash)

        self._request(
            "POST", f"/image/{image_hash}", authenticated=True, params=params,
            extra_headers=extra, json={"description": description, "post_id": album_hash},
        )
        link = f"https://imgur.com/gallery/{album_hash}"
        LOG.info("Weekly post: set description; live at %s", link)
        return {"gallery_id": album_hash, "image_id": image_hash, "url": link}
