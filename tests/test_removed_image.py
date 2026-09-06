import base64
from unittest.mock import Mock

from repostbot.hashing import (
    ImageSignature,
    REMOVED_PLACEHOLDER_AHASH,
    is_removed_placeholder,
    signature_from_bytes,
)
from repostbot.imgur import ImgurClient, RemovedImageError

# Imgur's removed.png (161x81) placeholder, embedded so the test is self-contained.
_REMOVED_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAKEAAABRAQMAAACADVTsAAAABlBMVEUiIiL///9ehyAxAAABrElE"
    "QVR4Xu3QL2/bQBgG8NdRlrnMNqxu1eVAahCQVAEF03STbsuBSFVZYEBBoJ2RjZ0Hljuy6IZaUlUl"
    "pfsKRUmZP4JTNJixkEm7nJu/Mxlot0l7JJOfXj06P/D3xvkBQH/lqoEC7WVvzqM0k/f4+Gat2nt7"
    "ppqeCjCbiJX6HmN7vnca4LLc0BljH/yZ0ZejDQXGlA9GmYSthoumVw1wZ6PByxjrpxmeZq0hbMcD"
    "XPCHGVB4hHCAkgUKrrNSulawelPRCH37mu4fR1EdZYPwnTA6UZoQfteoMSmPCFVcgYmUmmCuPMKk"
    "IAtNFjqS+hWyOo+MzmVsb12NS1aFazThe1Ztr2qYBklWvcPKCKG+TA/MGwjqDcI4n1Pko+1E5KM9"
    "TRz75fGB0qWv1Vlq/Bo9Gzqo3oqu7g991G1bVQmp8IQcdeRtEGpyxoVVB5eNLob0qS6xpaJc5+J7"
    "Wx+wkwct5SoSn2vCOORKrHZk0lC69tAbm4a2g0grEuknvd9tb61XhqK8hz+d/xG/cft5fD0dvxA7"
    "qsLrj+EXWqBugRbeHl6qcbCr4Ba+7Tn88/kJk4CIztd1IrIAAAAASUVORK5CYII=")
REMOVED_PNG = base64.b64decode(_REMOVED_PNG_B64)


def _response(url, content=b"data"):
    r = Mock()
    r.url = url
    r.content = content
    r.raise_for_status = Mock()
    return r


def _client():
    c = ImgurClient("x", max_retries=0)
    c.session = Mock()
    return c


def test_download_image_raises_on_removed_redirect():
    c = _client()
    c.session.get = Mock(return_value=_response("https://i.imgur.com/removed.png", REMOVED_PNG))
    try:
        c.download_image("https://i.imgur.com/abc.jpg")
    except RemovedImageError:
        return
    raise AssertionError("expected RemovedImageError for removed.png redirect")


def test_download_image_returns_bytes_for_normal_url():
    c = _client()
    c.session.get = Mock(return_value=_response("https://i.imgur.com/abc.jpg", b"realbytes"))
    assert c.download_image("https://i.imgur.com/abc.jpg") == b"realbytes"


def test_removed_placeholder_signature_is_detected():
    sig = signature_from_bytes(REMOVED_PNG)
    assert sig.ahash == REMOVED_PLACEHOLDER_AHASH
    assert is_removed_placeholder(sig)


def test_is_removed_placeholder_false_for_ordinary_signature():
    sig = ImageSignature("sha", "1" * 64, "2" * 64, "3" * 64, 42, False)
    assert not is_removed_placeholder(sig)
