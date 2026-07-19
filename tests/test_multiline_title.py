"""Tests for the CSS hints that help title + image stay together on
readers that DO honour page-break CSS (Kindle, Calibre, iBooks). On
CrossPoint / FreeInkBook the guarantee comes from the per-article
`<a id="aN">` anchor listed in the TOC — see test_layout.py."""
import io
import zipfile
from pathlib import Path

from PIL import Image

from randompedia.epub_build import BookMeta, build_epub, stable_book_id
from randompedia.images import ProcessedImage
from randompedia.summaries import ArticleSummary


def _fake_png() -> bytes:
    buf = io.BytesIO()
    Image.new("L", (400, 300), 200).save(buf, format="JPEG", quality=75)
    return buf.getvalue()


class _FakePipeline:
    def __init__(self, *a, **kw): self.data = _fake_png()
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def close(self): pass
    def fetch_and_process(self, url):
        return ProcessedImage(self.data, "img_fake.jpg", "image/jpeg")


def _meta():
    return BookMeta(title="t", identifier=stable_book_id("t", "en", "1", "s"),
                    lang="en", seed="s")


def _read_css(zf: zipfile.ZipFile) -> str:
    css_name = next(n for n in zf.namelist() if n.endswith(".css"))
    return zf.read(css_name).decode("utf-8")


def test_h1_and_subtitle_avoid_page_break_after(tmp_path: Path):
    """CSS hint for capable readers: keep header + first content lines
    together. CrossPoint ignores this and relies on spine splits instead."""
    art = ArticleSummary(
        title="A Really Long Article Title That Will Definitely Wrap",
        key="Long", lang="en",
        description="An equally long subtitle to make the header multi-line",
        extract_html="<p>body</p>", extract_text="body",
        image_url=None, raw_image_url=None,
        url="https://en.wikipedia.org/wiki/Long",
    )
    out = tmp_path / "long.epub"
    build_epub(articles=[art], output_path=out, meta=_meta(), with_images=False)
    with zipfile.ZipFile(out) as zf:
        css = _read_css(zf)

    assert "page-break-after: avoid" in css
    # Applied to at least the h1 and .desc.
    h1_block = css[css.index("h1"):css.index("h1") + 200]
    assert "page-break-after: avoid" in h1_block
    desc_block = css[css.index(".desc"):css.index(".desc") + 200]
    assert "page-break-after: avoid" in desc_block


def test_lead_image_does_not_break_across_pages(tmp_path: Path, monkeypatch):
    """CSS hint that the image itself shouldn't split across a page break.
    Ignored by CrossPoint (which auto-paginates around images anyway) but
    useful for capable readers."""
    from randompedia import epub_build
    monkeypatch.setattr(epub_build, "ImagePipeline", _FakePipeline)
    art = ArticleSummary(
        title="Sample", key="Sample", lang="en", description=None,
        extract_html="<p>x</p>", extract_text="x",
        image_url="https://x/y", raw_image_url="https://x/y",
        url="https://en.wikipedia.org/wiki/Sample",
    )
    out = tmp_path / "img2.epub"
    build_epub(articles=[art], output_path=out, meta=_meta(), with_images=True)
    with zipfile.ZipFile(out) as zf:
        css = _read_css(zf)
    lead_block = css[css.index(".lead-image"):css.index(".lead-image") + 300]
    assert "page-break-inside: avoid" in lead_block
