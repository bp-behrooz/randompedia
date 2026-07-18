"""Generate a simple cover image for the EPUB.

Pillow renders text with a default bitmap font that ships with the
library — no external fonts to bundle. The output is a portrait
grayscale JPEG sized for the smallest target device (Xteink X4 in
portrait, 480x800) at the same quality as lead images.

Deliberately minimal: just the project name and a subtitle line. E-ink
covers don't benefit from decoration; the reader library thumbnails are
tiny and the on-device open-page view is transient.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont


@dataclass(slots=True)
class CoverSpec:
    width: int = 480
    height: int = 800
    quality: int = 75          # matches lead-image encoding
    background: int = 255      # e-ink is happier with white background
    foreground: int = 0        # black text
    title_size: int = 56       # points, roughly 1/9 of the height
    subtitle_size: int = 28


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont,
              max_width: int) -> ImageFont.ImageFont:
    """Shrink the font until the given text fits within max_width."""
    size = font.size
    while size > 10:
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= max_width:
            return font
        size = int(size * 0.9)
        # Pillow's default font is a fixed bitmap — the size parameter
        # only works on TrueType. We can still shrink by using the
        # size-supporting call; if it fails, return the original.
        try:
            font = ImageFont.load_default(size=size)
        except Exception:  # noqa: BLE001
            return font
    return font


def render_cover(*, title: str, subtitle: str, spec: CoverSpec | None = None) -> bytes:
    """Return the cover as JPEG bytes."""
    spec = spec or CoverSpec()
    im = Image.new("L", (spec.width, spec.height), spec.background)
    draw = ImageDraw.Draw(im)

    horizontal_margin = int(spec.width * 0.08)
    max_text_width = spec.width - 2 * horizontal_margin

    title_font = ImageFont.load_default(size=spec.title_size)
    subtitle_font = ImageFont.load_default(size=spec.subtitle_size)

    # Shrink either line if it doesn't fit at the default size.
    title_font = _fit_text(draw, title, title_font, max_text_width)
    subtitle_font = _fit_text(draw, subtitle, subtitle_font, max_text_width)

    # Center vertically as a block: title above subtitle with a small gap.
    title_bbox = draw.textbbox((0, 0), title, font=title_font)
    subtitle_bbox = draw.textbbox((0, 0), subtitle, font=subtitle_font)
    title_h = title_bbox[3] - title_bbox[1]
    subtitle_h = subtitle_bbox[3] - subtitle_bbox[1]
    gap = int(spec.height * 0.03)
    block_h = title_h + gap + subtitle_h
    y0 = (spec.height - block_h) // 2

    # Centered horizontally per line.
    title_w = title_bbox[2] - title_bbox[0]
    subtitle_w = subtitle_bbox[2] - subtitle_bbox[0]
    draw.text(
        ((spec.width - title_w) // 2 - title_bbox[0], y0 - title_bbox[1]),
        title, font=title_font, fill=spec.foreground,
    )
    draw.text(
        ((spec.width - subtitle_w) // 2 - subtitle_bbox[0],
         y0 + title_h + gap - subtitle_bbox[1]),
        subtitle, font=subtitle_font, fill=spec.foreground,
    )

    out = io.BytesIO()
    im.save(out, format="JPEG", quality=spec.quality, optimize=True, progressive=False)
    return out.getvalue()
