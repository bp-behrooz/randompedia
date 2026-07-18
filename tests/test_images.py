"""Tests for the image pipeline.

The pipeline outputs baseline grayscale JPEG at a configurable quality.
See `randompedia.images` for the design rationale."""
import io

import pytest
from PIL import Image

from randompedia.images import ImageSpec, process_bytes


def _png_bytes(im: Image.Image) -> bytes:
    """Encode a PIL image as PNG bytes for feeding to `process_bytes`.
    (Using PNG as INPUT to the pipeline exercises the decode path we
    actually see in production — Wikimedia serves both.)"""
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_resizes_wider_than_max():
    src = Image.new("RGB", (1600, 800), (128, 200, 50))
    out = Image.open(io.BytesIO(process_bytes(_png_bytes(src), ImageSpec(max_width=400))))
    assert out.width == 400
    assert out.height == 200
    # JPEG grayscale decodes as "L" in PIL.
    assert out.mode == "L"


def test_does_not_upscale_narrower_images():
    src = Image.new("RGB", (120, 90), (10, 20, 30))
    out = Image.open(io.BytesIO(process_bytes(_png_bytes(src), ImageSpec(max_width=400))))
    assert out.width == 120  # unchanged


def test_flattens_rgba_transparency_onto_white():
    src = Image.new("RGBA", (200, 200), (0, 0, 0, 0))  # fully transparent
    out = Image.open(io.BytesIO(process_bytes(_png_bytes(src), ImageSpec())))
    out = out.convert("L")
    # Fully-transparent input should become white after flatten.
    assert out.getpixel((100, 100)) >= 240


def test_output_is_baseline_grayscale_jpeg():
    """The output must be:
      * a real JPEG (SOI marker `FF D8`),
      * baseline (SOF0 = FF C0), NOT progressive (SOF2 = FF C2) —
        CrossPoint's TJpgDec only fully decodes baseline,
      * grayscale (single component in SOF)."""
    src = Image.new("RGB", (300, 200), (50, 100, 150))
    out = process_bytes(_png_bytes(src), ImageSpec())

    # SOI
    assert out.startswith(b"\xff\xd8"), "not a JPEG (missing SOI)"

    # Walk segments looking for the SOF marker.
    i = 2
    sof_marker = None
    sof_ncomp = None
    while i < len(out) - 1:
        if out[i] != 0xFF:
            break
        marker = out[i + 1]
        # SOI/EOI/RSTn/TEM have no length field; SOF0..SOF15 do.
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            i += 2
            continue
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            # Start of Frame
            sof_marker = marker
            # SOF payload: 2B length, 1B precision, 2B height, 2B width, 1B ncomponents
            sof_ncomp = out[i + 2 + 5]
            break
        # Skip a variable-length segment: 2B length includes itself.
        length = (out[i + 2] << 8) | out[i + 3]
        i += 2 + length

    assert sof_marker is not None, "no Start-of-Frame marker found"
    assert sof_marker == 0xC0, (
        f"expected baseline SOF0 (0xC0), got 0x{sof_marker:02X} "
        f"({'progressive SOF2' if sof_marker == 0xC2 else 'other SOF'}) — "
        f"CrossPoint's TJpgDec cannot fully decode this"
    )
    assert sof_ncomp == 1, (
        f"expected grayscale (1 component), got {sof_ncomp} — "
        f"colour JPEGs are 2x the size for no benefit on a grayscale panel"
    )


def test_jpeg_is_much_smaller_than_source_png():
    """Sanity: the pipeline should compress typical Wikipedia lead-image
    dimensions to something modest, not something the same size as the
    source. Guards against a regression that stops the resize+encode."""
    # A ~1600x1000 photo-ish image with random-looking noise so it
    # doesn't compress to nothing regardless.
    import random
    rng = random.Random(0)
    src = Image.new("RGB", (1600, 1000))
    px = src.load()
    for y in range(1000):
        for x in range(1600):
            v = (x * 3 + y * 7 + rng.randint(0, 30)) % 256
            px[x, y] = (v, v, v)
    src_png = _png_bytes(src)

    out = process_bytes(src_png, ImageSpec())

    # Source is ~1MB+, output must be a small fraction.
    assert len(out) < len(src_png) // 10, (
        f"expected significant shrinkage; got {len(out)} bytes from "
        f"{len(src_png)} source bytes"
    )
    # And an absolute cap — 400x250 grayscale JPEG q75 should be well
    # under 40 KB for anything short of pure noise.
    assert len(out) < 40_000, f"unexpectedly large: {len(out)} bytes"


def test_quality_knob_actually_changes_size():
    """Higher quality → bigger file. Guards against a regression that
    silently ignores the quality parameter."""
    src = Image.new("RGB", (400, 300))
    px = src.load()
    for y in range(300):
        for x in range(400):
            v = ((x * y) % 251)
            px[x, y] = (v, v, v)
    src_bytes = _png_bytes(src)

    low = len(process_bytes(src_bytes, ImageSpec(quality=40)))
    mid = len(process_bytes(src_bytes, ImageSpec(quality=75)))
    high = len(process_bytes(src_bytes, ImageSpec(quality=95)))
    assert low < mid < high, (low, mid, high)


def test_reject_absurd_input_gracefully():
    with pytest.raises(Exception):
        process_bytes(b"not an image", ImageSpec())
