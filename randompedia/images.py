"""Image pipeline: download → grayscale → resize → baseline grayscale JPEG.

Tuned for e-ink displays (xteink / crosspoint-reader is 16-shade grayscale).

Design notes:

  * **JPEG, not PNG.** EPUB 3 mandates JPEG support in every conformant
    reader, and it compresses ~2x better than a dithered PNG at
    equivalent perceived quality on e-ink. PNG buys us nothing here.

  * **Baseline, not progressive.** CrossPoint's TJpgDec decodes both,
    but the progressive path is a DC-only preview (blurry). Ship
    baseline so the reader shows the fully-decoded image.

  * **Grayscale (single component), not RGB/YCbCr.** CrossPoint is
    configured for 8-bit grayscale output (`JD_FORMAT = 2` in
    `freeink-sdk/.../tjpgdcnf.h`); a colour JPEG would be decoded to
    grayscale anyway. Grayscale JPEGs are half the size.

  * **No pre-dither.** CrossPoint does its own Bayer 4x4 dither at draw
    time when quantising to the panel's 16 shades. Pre-dithering just
    creates high-frequency noise that hurts JPEG compression and then
    the reader dithers on top of the dither. A smooth grayscale JPEG
    gives the reader a cleaner input.
"""

from __future__ import annotations

import hashlib
import io
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image
from tenacity import retry, stop_after_attempt, wait_exponential

from . import USER_AGENT
from .summaries import _parse_retry_after

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
    # JPEG quality 1-100. 75 is a good sweet spot for e-ink at these
    # dimensions: noticeably smaller than 85, and the reader's own
    # quantisation to 16 shades hides most compression artefacts.
    quality: int = 75


@dataclass(slots=True)
class ProcessedImage:
    data: bytes
    filename: str      # e.g. "img_a1b2c3d4e5f60718.jpg"
    media_type: str    # always "image/jpeg"


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=2, max=60))
def _download(client: httpx.Client, url: str) -> bytes:
    r = client.get(url, timeout=30.0)
    if r.status_code == 429:
        wait_s = _parse_retry_after(r.headers.get("Retry-After") or "30")
        wait_s = min(max(wait_s, 5.0), 120.0)
        log.warning("429 for image %s, waiting %.1fs", url, wait_s)
        time.sleep(wait_s)
    r.raise_for_status()
    return r.content


def process_bytes(raw: bytes, spec: ImageSpec) -> bytes:
    """Decode `raw`, resize + convert to grayscale, encode as baseline
    grayscale JPEG."""
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

        out = io.BytesIO()
        im.save(
            out,
            format="JPEG",
            quality=spec.quality,
            optimize=True,
            progressive=False,  # CrossPoint's decoder can only preview progressive
        )
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
        filename = f"img_{digest}.jpg"

        if self.cache_dir:
            cached = self.cache_dir / filename
            if cached.exists():
                return ProcessedImage(cached.read_bytes(), filename, "image/jpeg")

        try:
            raw = _download(self.client, url)
        except Exception as e:  # noqa: BLE001
            log.warning("image download failed for %s: %s", url, e)
            return None

        try:
            processed = process_bytes(raw, self.spec)
        except Exception as e:  # noqa: BLE001
            log.warning("image processing failed for %s: %s", url, e)
            return None

        if self.cache_dir:
            (self.cache_dir / filename).write_bytes(processed)

        return ProcessedImage(processed, filename, "image/jpeg")
