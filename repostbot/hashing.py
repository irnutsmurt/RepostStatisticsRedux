from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError


class UnsupportedMediaError(ValueError):
    """Downloaded payload is not a still image Pillow can decode."""


@dataclass(frozen=True)
class ImageSignature:
    exact_hash: str
    ahash: str | None
    dhash_h: str | None
    dhash_v: str | None
    bit_count: int
    animated: bool


# Imgur's 161x81 "image removed" placeholder (i.imgur.com/removed.png). Any
# deleted image resolves to it, so every removed image hashes to these exact
# values -- which would otherwise make them all look like reposts of each other.
# Copies re-encoded to different bytes still share this perceptual triple, so we
# match on the perceptual hashes (not the exact SHA-256). See is_removed_placeholder.
REMOVED_PLACEHOLDER_AHASH = "ffff23df800ffbff80000000ffff80010001feffffffffffffff03ffdfffffff"
REMOVED_PLACEHOLDER_DHASH_H = "2a90b560d964c943b936edb44c93957c97680a5a00844600a800c900d0002c00"
REMOVED_PLACEHOLDER_DHASH_V = "fff07ff60000c81fffff00000000fffe00010001fffe0000fc00fc0002008400"


def is_removed_placeholder(signature: "ImageSignature") -> bool:
    """True when a signature matches Imgur's removed-image placeholder."""
    return (
        signature.ahash == REMOVED_PLACEHOLDER_AHASH
        and signature.dhash_h == REMOVED_PLACEHOLDER_DHASH_H
        and signature.dhash_v == REMOVED_PLACEHOLDER_DHASH_V
    )


def _pixels(image: Image.Image) -> list[int]:
    getter = getattr(image, "get_flattened_data", None)
    if getter is not None:
        return list(getter())
    return list(image.getdata())


def _bits_to_hex(bits: list[bool]) -> str:
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return f"{value:0{len(bits) // 4}x}"


def _ahash_256(image: Image.Image) -> str:
    resized = image.resize((16, 16), Image.Resampling.LANCZOS)
    pixels = _pixels(resized)
    average = sum(pixels) / len(pixels)
    # Preserve the original project's polarity: darker than average => 1.
    return _bits_to_hex([pixel < average for pixel in pixels])


def _dhash_h_256(image: Image.Image) -> str:
    resized = image.resize((17, 16), Image.Resampling.LANCZOS)
    pixels = _pixels(resized)
    bits: list[bool] = []
    for y in range(16):
        row = y * 17
        for x in range(16):
            bits.append(pixels[row + x] < pixels[row + x + 1])
    return _bits_to_hex(bits)


def _dhash_v_256(image: Image.Image) -> str:
    resized = image.resize((16, 17), Image.Resampling.LANCZOS)
    pixels = _pixels(resized)
    bits: list[bool] = []
    for y in range(16):
        for x in range(16):
            bits.append(pixels[y * 16 + x] < pixels[(y + 1) * 16 + x])
    return _bits_to_hex(bits)


def hamming_hex(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


def signature_from_bytes(data: bytes, animated_hint: bool = False) -> ImageSignature:
    exact = sha256(data).hexdigest()

    # Imgur commonly serves animated gallery posts as MP4/GIFV. Pillow cannot
    # decode those video containers, so honor the API's animated flag before
    # attempting to open the payload as an image. Legacy behavior for animated
    # media is intentionally conservative: compare exact downloaded bytes only.
    if animated_hint:
        return ImageSignature(exact, None, None, None, 0, True)

    try:
        with Image.open(BytesIO(data)) as raw:
            if getattr(raw, "is_animated", False):
                return ImageSignature(exact, None, None, None, 0, True)

            # EXIF transpose handles phone/camera orientation before hashing.
            image = ImageOps.exif_transpose(raw).convert("L")
            ahash = _ahash_256(image)
            dhash_h = _dhash_h_256(image)
            dhash_v = _dhash_v_256(image)
            bit_count = int(ahash, 16).bit_count() + int(dhash_h, 16).bit_count() + int(dhash_v, 16).bit_count()
            if bit_count == 0:
                raise ValueError("image hash is all zeroes")
            return ImageSignature(exact, ahash, dhash_h, dhash_v, bit_count, False)
    except (UnidentifiedImageError, OSError) as exc:
        raise UnsupportedMediaError("downloaded payload is not a supported still image") from exc
