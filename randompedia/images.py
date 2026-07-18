"""Image pipeline: download → grayscale → resize → dither → PNG.

Tuned for e-ink displays (xteink / crosspoint-reader is 16-shade grayscale).
"""

from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image
from tenacity import retry, stop_after_attempt, wait_exponential

from . import USER_AGENT

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ImageSpec:
    """Image processing parameters.

    Defaults are tuned for the target hardware — the Xteink X3/X4 running
    CrossPoint firmware. Native panel resolutions from the CrossPoint SDK:

      * X4 — 800 x 480, 4.3" (SSD1677 driver, ~220 PPI)
      * X3 — 792 x 528, 3.7" (UC8253 driver, ~259 PPI)

    Both panels are landscape-native but readers typically use them in
    portrait, giving a usable page of roughly 480 x 800 (X4) / 528 x 792
    (X3). We size images so the title (which may wrap to 2-3 lines), an
    optional subtitle, and the image all fit on a single page before a
    forced page break to the summary body. Empirically that means the
    image should occupy at most ~40% of the shorter panel dimension.
    """
    max_width: int = 400
    max_height: int = 320
    shades: int = 16   # 4-bit grayscale
    dither: bool = True


@dataclass(slots=True)
class ProcessedImage:
    data: bytes        # PNG bytes
    filename: str      # stable name, safe for EPUB
    media_type: str    # "image/png"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=15))
def _download(client: httpx.Client, url: str) -> bytes:
    r = client.get(url, timeout=30.0)
    r.raise_for_status()
    return r.content


def _quantize_to_shades(img: Image.Image, shades: int, dither: bool) -> Image.Image:
    """Reduce a grayscale image to `shades` distinct gray levels."""
    if shades >= 256:
        return img
    # Build an evenly-spaced grayscale palette.
    palette: list[int] = []
    for i in range(shades):
        v = round(i * 255 / (shades - 1))
        palette.extend([v, v, v])
    palette.extend([0, 0, 0] * (256 - shades))

    pal_img = Image.new("P", (1, 1))
    pal_img.putpalette(palette)

    rgb = img.convert("RGB")
    d = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
    quantized = rgb.quantize(palette=pal_img, dither=d)
    # Convert back to L so downstream PNG encoding is simple/small.
    return quantized.convert("L")


def process_bytes(raw: bytes, spec: ImageSpec) -> bytes:
    with Image.open(io.BytesIO(raw)) as im:
        im.load()
        # Flatten transparency onto white before grayscale.
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
            im = bg
        else:
            im = im.convert("RGB")

        if im.width > spec.max_width:
            new_h = round(im.height * spec.max_width / im.width)
            im = im.resize((spec.max_width, new_h), Image.Resampling.LANCZOS)

        # Also cap height so tall portrait images (posters, book covers)
        # don't overflow a single e-ink page.
        if im.height > spec.max_height:
            new_w = round(im.width * spec.max_height / im.height)
            im = im.resize((new_w, spec.max_height), Image.Resampling.LANCZOS)

        im = im.convert("L")
        im = _quantize_to_shades(im, spec.shades, spec.dither)

        out = io.BytesIO()
        # optimize=True + reduced palette makes these very small.
        im.save(out, format="PNG", optimize=True)
        return out.getvalue()


class ImagePipeline:
    def __init__(self, spec: ImageSpec | None = None, *, cache_dir: Path | None = None):
        self.spec = spec or ImageSpec()
        self.cache_dir = cache_dir
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            http2=True,
            timeout=30.0,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def fetch_and_process(self, url: str) -> ProcessedImage | None:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        filename = f"img_{digest}.png"

        if self.cache_dir:
            cached = self.cache_dir / filename
            if cached.exists():
                return ProcessedImage(cached.read_bytes(), filename, "image/png")

        try:
            raw = _download(self.client, url)
        except Exception as e:  # noqa: BLE001
            log.warning("image download failed for %s: %s", url, e)
            return None

        try:
            png = process_bytes(raw, self.spec)
        except Exception as e:  # noqa: BLE001
            log.warning("image processing failed for %s: %s", url, e)
            return None

        if self.cache_dir:
            (self.cache_dir / filename).write_bytes(png)

        return ProcessedImage(png, filename, "image/png")
