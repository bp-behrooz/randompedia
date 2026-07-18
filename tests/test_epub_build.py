"""Tests for EPUB assembly.

These specifically guard against the classes of bugs already seen:
  - HTML wrapper leaking into <h1> / TOC titles
  - Image href in chapter HTML not resolving to an actual file in the archive
"""
import io
import zipfile
from pathlib import Path

from PIL import Image

from randompedia.epub_build import BookMeta, build_epub, stable_book_id
from randompedia.images import ProcessedImage
from randompedia.summaries import ArticleSummary


def _fake_png() -> bytes:
    im = Image.new("L", (50, 50), 200)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=75)
    return buf.getvalue()


class _FakePipeline:
    """Stand-in for ImagePipeline that returns a canned JPEG, no network."""
    def __init__(self, *a, **kw): self.data = _fake_png()
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def close(self): pass
    def fetch_and_process(self, url):
        return ProcessedImage(self.data, "img_fake.jpg", "image/jpeg")


def _sample_articles(with_image_url: bool = False) -> list[ArticleSummary]:
    return [
        ArticleSummary(
            title="Cristiano Ronaldo",
            key="Cristiano_Ronaldo",
            lang="en",
            description="Portuguese footballer",
            extract_html="<p>He plays football.</p>",
            extract_text="He plays football.",
            image_url="https://example.invalid/ronaldo.jpg" if with_image_url else None,
            url="https://en.wikipedia.org/wiki/Cristiano_Ronaldo",
        ),
        ArticleSummary(
            title="Foo & Bar",  # entity-sensitive
            key="Foo_and_Bar",
            lang="en",
            description=None,
            extract_html="<p>Body.</p>",
            extract_text="Body.",
            image_url=None,
            url="https://en.wikipedia.org/wiki/Foo_and_Bar",
        ),
    ]


def _meta() -> BookMeta:
    return BookMeta(
        title="test",
        identifier=stable_book_id("test", "en", "2", "seed"),
        lang="en",
        seed="seed",
    )


def test_text_only_epub_has_no_image_files(tmp_path: Path):
    out = tmp_path / "text.epub"
    build_epub(
        articles=_sample_articles(),
        output_path=out,
        meta=_meta(),
        with_images=False,
    )
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert not any(n.endswith(".png") or n.endswith(".jpg") or n.endswith(".jpeg")
                   for n in names)


def test_every_image_src_resolves_to_a_file_in_the_epub(
    tmp_path: Path, monkeypatch
):
    """This is the regression test for the 'broken image path' bug."""
    from randompedia import epub_build
    monkeypatch.setattr(epub_build, "ImagePipeline", _FakePipeline)

    out = tmp_path / "img.epub"
    build_epub(
        articles=_sample_articles(with_image_url=True),
        output_path=out,
        meta=_meta(),
        with_images=True,
    )

    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        # Find every chapter, parse its <img src=...>, and confirm the target exists.
        import re
        img_re = re.compile(rb'<img[^>]*\bsrc="([^"]+)"')
        chapters = [n for n in names
                    if n.endswith(".xhtml")
                    and "nav" not in n.lower()
                    and "colophon" not in n.lower()]
        assert chapters, "no chapters found"

        found_any_img = False
        for chap in chapters:
            content = zf.read(chap)
            for src in img_re.findall(content):
                found_any_img = True
                src_s = src.decode("utf-8")
                # Resolve src relative to the chapter's directory inside the zip.
                chap_dir = chap.rsplit("/", 1)[0] if "/" in chap else ""
                # Normalize ../ segments.
                parts: list[str] = chap_dir.split("/") if chap_dir else []
                for seg in src_s.split("/"):
                    if seg == "..":
                        if parts:
                            parts.pop()
                    elif seg and seg != ".":
                        parts.append(seg)
                resolved = "/".join(parts)
                assert resolved in names, (
                    f"image src {src_s!r} in {chap} resolves to {resolved!r}, "
                    f"which is not in the archive"
                )

        assert found_any_img, "no <img> tags were emitted despite with_images=True"


def test_chapter_title_is_plain_text_not_html(tmp_path: Path):
    """Regression: displaytitle HTML must not appear inside <h1> or <title>."""
    out = tmp_path / "titles.epub"
    articles = [
        ArticleSummary(
            title="Cristiano Ronaldo",  # already cleaned upstream
            key="Cristiano_Ronaldo",
            lang="en",
            description=None,
            extract_html="<p>x</p>",
            extract_text="x",
            image_url=None,
            url="https://en.wikipedia.org/wiki/Cristiano_Ronaldo",
        )
    ]
    build_epub(articles=articles, output_path=out, meta=_meta(), with_images=False)

    with zipfile.ZipFile(out) as zf:
        chap = next(n for n in zf.namelist()
                    if n.endswith(".xhtml")
                    and "nav" not in n.lower()
                    and "colophon" not in n.lower())
        html = zf.read(chap).decode("utf-8")
    assert "<h1>Cristiano Ronaldo</h1>" in html
    assert "mw-page-title-main" not in html
    assert 'lang="en" dir="ltr"' not in html  # displaytitle wrapper


def test_epub_is_valid_zip_with_mimetype_first(tmp_path: Path):
    """EPUB spec requires 'mimetype' to be the first entry and stored uncompressed."""
    out = tmp_path / "spec.epub"
    build_epub(articles=_sample_articles(), output_path=out, meta=_meta(), with_images=False)
    with zipfile.ZipFile(out) as zf:
        infos = zf.infolist()
    assert infos[0].filename == "mimetype"
    assert infos[0].compress_type == zipfile.ZIP_STORED
    assert zipfile.ZipFile(out).read("mimetype") == b"application/epub+zip"


def test_stable_book_id_is_deterministic():
    a = stable_book_id("v1", "en", "1000", "seed")
    b = stable_book_id("v1", "en", "1000", "seed")
    c = stable_book_id("v1", "en", "1000", "other")
    assert a == b
    assert a != c
    assert a.startswith("urn:randompedia:")


def test_chapter_count_matches_article_count(tmp_path: Path):
    articles = _sample_articles()
    out = tmp_path / "count.epub"
    build_epub(articles=articles, output_path=out, meta=_meta(), with_images=False)
    with zipfile.ZipFile(out) as zf:
        # Exclude the nav page and the colophon; only count real article chapters.
        chapters = [
            n for n in zf.namelist()
            if n.endswith(".xhtml")
            and "nav" not in n.lower()
            and "colophon" not in n.lower()
        ]
    assert len(chapters) == len(articles)
