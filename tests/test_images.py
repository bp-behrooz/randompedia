"""Tests for the image pipeline."""
import io

import pytest
from PIL import Image

from randompedia.images import ImageSpec, process_bytes


def _png_bytes(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_resizes_wider_than_max():
    src = Image.new("RGB", (1600, 800), (128, 200, 50))
    out_png = process_bytes(_png_bytes(src), ImageSpec(max_width=400, shades=16))
    out = Image.open(io.BytesIO(out_png))
    assert out.width == 400
    assert out.height == 200
    assert out.mode == "L"


def test_does_not_upscale_narrower_images():
    src = Image.new("RGB", (120, 90), (10, 20, 30))
    out_png = process_bytes(_png_bytes(src), ImageSpec(max_width=400, shades=16))
    out = Image.open(io.BytesIO(out_png))
    assert out.width == 120  # unchanged


def test_produces_at_most_shades_distinct_grays():
    # Use a gradient so the output would naturally have many shades if we
    # weren't quantizing.
    src = Image.new("L", (256, 32))
    for x in range(256):
        for y in range(32):
            src.putpixel((x, y), x)
    src = src.convert("RGB")

    out_png = process_bytes(_png_bytes(src), ImageSpec(max_width=400, shades=16, dither=False))
    out = Image.open(io.BytesIO(out_png)).convert("L")
    distinct = {out.getpixel((x, y)) for x in range(out.width) for y in range(out.height)}
    assert len(distinct) <= 16, f"got {len(distinct)} shades, expected <=16"


def test_flattens_rgba_transparency_onto_white():
    src = Image.new("RGBA", (200, 200), (0, 0, 0, 0))  # fully transparent
    out_png = process_bytes(_png_bytes(src), ImageSpec(max_width=400, shades=16))
    out = Image.open(io.BytesIO(out_png)).convert("L")
    # Fully-transparent input should become white after flatten.
    assert out.getpixel((100, 100)) >= 240


def test_output_is_valid_png():
    src = Image.new("RGB", (300, 200), (50, 100, 150))
    out_png = process_bytes(_png_bytes(src), ImageSpec())
    assert out_png.startswith(b"\x89PNG\r\n\x1a\n")


def test_reject_absurd_input_gracefully():
    with pytest.raises(Exception):
        process_bytes(b"not an image", ImageSpec())
