from repostbot.imgur import ImgurClient


def test_animated_gallery_item_prefers_direct_mp4():
    item = {
        "id": "abc123",
        "title": "animated",
        "datetime": 1,
        "is_album": False,
        "animated": True,
        "link": "https://i.imgur.com/abc123.gifv",
        "mp4": "https://i.imgur.com/abc123.mp4",
        "ups": 1,
        "downs": 0,
    }
    posts = ImgurClient._expand_gallery_item(item)
    assert len(posts) == 1
    post = posts[0]
    assert post.animated is True
    assert post.image_url == "https://i.imgur.com/abc123.mp4"
    assert post.id == "abc123"
    assert post.gallery_id == "abc123"
    assert (post.image_index, post.image_count) == (1, 1)


def test_album_expands_to_one_post_per_image():
    item = {
        "id": "ALB",
        "title": "dump",
        "datetime": 100,
        "is_album": True,
        "images_count": 2,
        "account_id": 5,
        "account_url": "dumper",
        "ups": 10,
        "downs": 1,
        "images": [
            {"id": "img1", "link": "https://i.imgur.com/img1.png", "animated": False, "width": 64, "height": 64},
            {"id": "img2", "link": "https://i.imgur.com/img2.mp4", "mp4": "https://i.imgur.com/img2.mp4",
             "animated": True, "width": 32, "height": 48},
        ],
    }
    posts = ImgurClient._expand_gallery_item(item)
    assert [p.id for p in posts] == ["img1", "img2"]
    assert all(p.gallery_id == "ALB" for p in posts)
    assert [(p.image_index, p.image_count) for p in posts] == [(1, 2), (2, 2)]
    assert posts[1].animated is True
    assert posts[1].image_url == "https://i.imgur.com/img2.mp4"
    # Album-level votes propagate to each image (images carry none of their own).
    assert posts[0].net_score == 9


def test_transient_503_is_retried(monkeypatch):
    class FakeResponse:
        def __init__(self, status_code, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload
            self.text = text
            self.headers = {}

        @property
        def ok(self):
            return 200 <= self.status_code < 300

        def json(self):
            if self._payload is None:
                raise ValueError("not json")
            return self._payload

    class FakeSession:
        def __init__(self):
            self.responses = [
                FakeResponse(503, text="<html>first byte timeout</html>"),
                FakeResponse(200, payload={"data": []}),
            ]
            self.headers = {}
            self.calls = 0

        def request(self, *args, **kwargs):
            response = self.responses[self.calls]
            self.calls += 1
            return response

    client = ImgurClient("x", max_retries=2, retry_backoff_seconds=0)
    client.session = FakeSession()
    assert client.gallery("top", "viral", "all", 12) == []
    assert client.session.calls == 2


def test_account_history_verifies_token_owner_and_reads_larynx_history():
    class FakeResponse:
        def __init__(self, payload):
            self.status_code = 200
            self.text = ""
            self.headers = {}
            self._payload = payload
        @property
        def ok(self): return True
        def json(self): return self._payload

    class FakeSession:
        def __init__(self):
            self.headers = {}
            self.calls = []
            self.responses = [
                FakeResponse({"data": {"id": 42, "url": "RepostStatisticsRedux"}}),
                FakeResponse({"page_token": "abc", "notifications": [{"id": "n1", "type": "commentmention"}]}),
            ]
        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return self.responses.pop(0)

    client = ImgurClient("client", access_token="token", max_retries=0)
    fake = FakeSession()
    client.session = fake
    result = client.account_history("RepostStatisticsRedux")

    assert result[0]["id"] == "n1"
    assert fake.calls[0][1].endswith("/3/account/me")
    assert fake.calls[1][1].endswith("/3/larynx/history")
    assert fake.calls[1][2]["headers"]["Authorization"] == "Bearer token"


def test_account_history_rejects_token_for_wrong_account():
    class FakeResponse:
        status_code = 200
        text = ""
        headers = {}
        @property
        def ok(self): return True
        def json(self): return {"data": {"id": 1, "url": "SomeOtherAccount"}}

    class FakeSession:
        def __init__(self): self.headers = {}; self.calls = 0
        def request(self, *args, **kwargs): self.calls += 1; return FakeResponse()

    from repostbot.imgur import ImgurApiError
    client = ImgurClient("client", access_token="token", max_retries=0)
    fake = FakeSession()
    client.session = fake
    try:
        client.account_history("RepostStatisticsRedux")
    except ImgurApiError as exc:
        assert "SomeOtherAccount" in str(exc)
        assert "RepostStatisticsRedux" in str(exc)
    else:
        raise AssertionError("expected account mismatch to fail")
    assert fake.calls == 1


def test_account_history_accepts_data_wrapped_larynx_shape():
    class FakeResponse:
        def __init__(self, payload):
            self.status_code = 200
            self.text = ""
            self.headers = {}
            self._payload = payload
        @property
        def ok(self): return True
        def json(self): return self._payload

    class FakeSession:
        def __init__(self):
            self.headers = {}
            self.responses = [
                FakeResponse({"data": {"id": 42, "url": "RepostStatisticsRedux"}}),
                FakeResponse({"data": {"notifications": [{"id": "n2", "type": "commentmention"}]}}),
            ]
        def request(self, method, url, **kwargs):
            return self.responses.pop(0)

    client = ImgurClient("client", access_token="token", max_retries=0)
    client.session = FakeSession()
    result = client.account_history("RepostStatisticsRedux")
    assert result == [{"id": "n2", "type": "commentmention"}]


def test_history_diagnostics_never_returns_comment_bodies():
    class FakeResponse:
        status_code = 200
        text = ""
        headers = {}
        @property
        def ok(self): return True
        def json(self):
            return {"data": [{"id": "n1", "type": "commentmention",
                                "comment_definition": {"caption": "secret-ish body"}}]}

    class FakeSession:
        def __init__(self): self.headers = {}
        def request(self, *args, **kwargs): return FakeResponse()

    client = ImgurClient("client", access_token="token", max_retries=0)
    client.session = FakeSession()
    results = client.account_history_diagnostics("RepostStatisticsRedux")
    assert len(results) == 2
    assert all("comment_definition" not in str(r) for r in results)
    assert all("secret-ish body" not in str(r) for r in results)
    assert results[0]["types"] == ["commentmention"]
    assert results[0]["path"] == "/larynx/history"


def test_account_lookup_uses_public_account_endpoint():
    class FakeResponse:
        status_code = 200
        text = ""
        headers = {}
        @property
        def ok(self): return True
        def json(self): return {"data": {"id": 42, "url": "Alice"}}

    class FakeSession:
        def __init__(self):
            self.headers = {}
            self.call = None
        def request(self, method, url, **kwargs):
            self.call = (method, url, kwargs)
            return FakeResponse()

    client = ImgurClient("client", max_retries=0)
    fake = FakeSession()
    client.session = fake
    account = client.account("Alice")
    assert account["id"] == 42
    assert fake.call[1].endswith("/3/account/Alice")


def test_rate_limit_headers_are_emitted_to_telemetry():
    events = []

    class FakeResponse:
        status_code = 200
        text = ""
        headers = {
            "X-RateLimit-ClientLimit": "12500",
            "X-RateLimit-ClientRemaining": "12345",
            "X-RateLimit-UserLimit": "500",
            "X-RateLimit-UserRemaining": "499",
            "X-RateLimit-UserReset": "1234567890",
        }
        @property
        def ok(self): return True
        def json(self): return {"data": []}

    class FakeSession:
        def __init__(self): self.headers = {}
        def request(self, *args, **kwargs): return FakeResponse()

    client = ImgurClient("client", max_retries=0, telemetry_callback=events.append)
    client.session = FakeSession()
    client.gallery("top", "viral", "all", 0)
    assert events[0]["status"] == 200
    assert events[0]["client_remaining"] == 12345
    assert events[0]["user_remaining"] == 499


def test_mark_notification_viewed_posts_authenticated_notification_endpoint():
    class FakeResponse:
        status_code = 200
        text = ""
        headers = {}
        @property
        def ok(self): return True
        def json(self): return {"data": True}

    class FakeSession:
        def __init__(self): self.headers = {}; self.call = None
        def request(self, method, url, **kwargs):
            self.call = (method, url, kwargs)
            return FakeResponse()

    client = ImgurClient("client", access_token="token", max_retries=0)
    fake = FakeSession()
    client.session = fake
    assert client.mark_notification_viewed("98765") is True
    assert fake.call[0] == "POST"
    assert fake.call[1].endswith("/3/notification/98765")
    assert fake.call[2]["headers"]["Authorization"] == "Bearer token"


def test_create_comment_posts_json_reply_body():
    class FakeResponse:
        status_code = 200
        text = ""
        headers = {}
        @property
        def ok(self): return True
        def json(self): return {"data": {"id": 123456}}

    class FakeSession:
        def __init__(self): self.headers = {}; self.call = None
        def request(self, method, url, **kwargs):
            self.call = (method, url, kwargs)
            return FakeResponse()

    client = ImgurClient("client", access_token="token", max_retries=0)
    fake = FakeSession()
    client.session = fake
    reply_id = client.create_comment("abc123", "Blocked.", parent_id=456)
    assert reply_id == 123456
    assert fake.call[0] == "POST"
    assert fake.call[1].endswith("/3/comment")
    assert fake.call[2]["json"] == {"image_id": "abc123", "comment": "Blocked.", "parent_id": 456}
    assert "data" not in fake.call[2]
