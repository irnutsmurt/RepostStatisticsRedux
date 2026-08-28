from io import BytesIO

from PIL import Image

from repostbot.hashing import UnsupportedMediaError, hamming_hex, signature_from_bytes


def png_bytes(image: Image.Image) -> bytes:
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_identical_image_hashes_match():
    img = Image.new("RGB", (64, 64), "white")
    for x in range(32):
        for y in range(64):
            img.putpixel((x, y), (0, 0, 0))
    data = png_bytes(img)
    a = signature_from_bytes(data)
    b = signature_from_bytes(data)
    assert a.exact_hash == b.exact_hash
    assert hamming_hex(a.ahash, b.ahash) == 0
    assert hamming_hex(a.dhash_h, b.dhash_h) == 0
    assert hamming_hex(a.dhash_v, b.dhash_v) == 0


def test_reencoded_static_image_keeps_perceptual_hash():
    img = Image.new("RGB", (80, 50), "white")
    for x in range(40):
        for y in range(50):
            img.putpixel((x, y), (20, 20, 20))
    png = png_bytes(img)
    jpeg_out = BytesIO()
    img.save(jpeg_out, format="JPEG", quality=95)
    a = signature_from_bytes(png)
    b = signature_from_bytes(jpeg_out.getvalue())
    assert hamming_hex(a.ahash, b.ahash) <= 3
    assert hamming_hex(a.dhash_h, b.dhash_h) <= 10
    assert hamming_hex(a.dhash_v, b.dhash_v) <= 10


def test_animated_hint_does_not_require_pillow_decoding():
    # Imgur may serve animated posts as MP4/GIFV bytes rather than a GIF file.
    data = b"fake-mp4-container-bytes-that-pillow-cannot-open"
    sig = signature_from_bytes(data, animated_hint=True)
    assert sig.animated is True
    assert sig.ahash is None
    assert sig.dhash_h is None
    assert sig.dhash_v is None
    assert sig.bit_count == 0


def test_non_image_static_payload_is_rejected_cleanly():
    try:
        signature_from_bytes(b"<html>not an image</html>", animated_hint=False)
    except UnsupportedMediaError:
        pass
    else:
        raise AssertionError("expected UnsupportedMediaError")
