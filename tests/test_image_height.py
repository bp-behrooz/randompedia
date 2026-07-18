"""Tests for the image height cap.

Tall portrait images (movie posters, book covers) must be scaled down so
they don't overflow a single page on an e-ink reader.
"""
import io

from PIL import Image

from randompedia.images import ImageSpec, process_bytes


def _png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_tall_portrait_is_capped_by_height():
    # Movie-poster proportions: much taller than wide.
    src = Image.new("RGB", (300, 900), (100, 100, 100))
    out = Image.open(io.BytesIO(
        process_bytes(_png(src), ImageSpec(max_width=400, max_height=500))
    ))
    assert out.height <= 500
    # Aspect ratio preserved (within rounding).
    ratio = out.width / out.height
    src_ratio = 300 / 900
    assert abs(ratio - src_ratio) < 0.01


def test_wide_landscape_is_capped_by_width_not_height():
    # 1600×400 → should be scaled by width to 400×100, height untouched.
    src = Image.new("RGB", (1600, 400), (0, 0, 0))
    out = Image.open(io.BytesIO(
        process_bytes(_png(src), ImageSpec(max_width=400, max_height=500))
    ))
    assert out.width == 400
    assert out.height == 100
    assert out.height <= 500


def test_square_within_bounds_is_untouched():
    src = Image.new("RGB", (300, 300), (0, 0, 0))
    out = Image.open(io.BytesIO(
        process_bytes(_png(src), ImageSpec(max_width=400, max_height=500))
    ))
    assert out.size == (300, 300)


def test_both_dimensions_over_limit():
    # 2000×3000 with cap 400×500 → width scale gives 400×600, then height
    # cap kicks in → final should be ~333×500.
    src = Image.new("RGB", (2000, 3000), (0, 0, 0))
    out = Image.open(io.BytesIO(
        process_bytes(_png(src), ImageSpec(max_width=400, max_height=500))
    ))
    assert out.width <= 400
    assert out.height <= 500
