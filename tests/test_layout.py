"""Tests for chapter layout and for the two-spine-item split we do for
articles that carry an image.

On CrossPoint (FreeInkBook), no CSS-based page break works — the only
reliable way to guarantee an image and its summary sit on separate pages
is to place them in separate spine items. So an article with an image
gets two xhtml files ("cover" + body); one without an image gets one.
Only the body appears in the TOC either way, so users see one entry per
article regardless."""
import io
import re
import zipfile
from pathlib import Path

from PIL import Image

from randompedia.epub_build import BookMeta, build_epub, stable_book_id
from randompedia.images import ProcessedImage
from randompedia.summaries import ArticleSummary


def _fake_png() -> bytes:
    im = Image.new("L", (400, 300), 200)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=75)
    return buf.getvalue()


class _FakePipeline:
    def __init__(self, *a, **kw): self.data = _fake_png()
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def close(self): pass
    def fetch_and_process(self, url):
        return ProcessedImage(self.data, "img_fake.jpg", "image/jpeg")


def _art(with_image: bool = True, with_desc: bool = True) -> ArticleSummary:
    return ArticleSummary(
        title="Sample",
        key="Sample",
        lang="en",
        description="A short subtitle" if with_desc else None,
        extract_html="<p>The summary body.</p>",
        extract_text="The summary body.",
        image_url="https://x/y.jpg" if with_image else None,
        url="https://en.wikipedia.org/wiki/Sample",
    )


def _meta() -> BookMeta:
    return BookMeta(
        title="test", identifier=stable_book_id("t", "en", "1", "s"),
        lang="en", seed="s",
    )


def _all_chapter_htmls(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    """Return [(filename, content), ...] for every non-nav, non-colophon
    xhtml, in SPINE order (not alphabetical) so cover pages come before
    their body counterparts."""
    opf = zf.read("EPUB/content.opf").decode("utf-8")
    # Build a map of manifest id -> href, tolerant of attribute ordering.
    # (Regex-based to keep the test dependency-free; matches self-closing
    # <item .../> tags without stumbling on `/` inside attribute values.)
    manifest: dict[str, str] = {}
    for item in re.findall(r'<item\b[^>]*>', opf):
        m_id = re.search(r'\bid="([^"]+)"', item)
        m_href = re.search(r'\bhref="([^"]+)"', item)
        if m_id and m_href:
            manifest[m_id.group(1)] = m_href.group(1)
    spine_ids = re.findall(r'<itemref[^>]*\bidref="([^"]+)"', opf)
    names_in_order = [
        f"EPUB/{manifest[i]}" for i in spine_ids
        if i in manifest and manifest[i].endswith(".xhtml")
    ]
    filtered = [
        n for n in names_in_order
        # Skip: nav xhtml, colophon, and the generated book cover (which
        # is exactly `cover.xhtml`, distinct from per-article cover pages
        # named `chNNNNN_..._cover.xhtml`).
        if "nav" not in n.lower()
        and "colophon" not in n.lower()
        and not n.endswith("/cover.xhtml")
    ]
    return [(n, zf.read(n).decode("utf-8")) for n in filtered]


def _chapter_html(zf: zipfile.ZipFile) -> str:
    parts = _all_chapter_htmls(zf)
    assert parts, "no chapter file found"
    return parts[0][1]


def test_image_article_produces_two_spine_items(tmp_path: Path, monkeypatch):
    """One cover page (title + image, no summary), one body page (title +
    summary + footer). Kept as separate xhtml files so CrossPoint puts
    them on separate pages."""
    from randompedia import epub_build
    monkeypatch.setattr(epub_build, "ImagePipeline", _FakePipeline)

    out = tmp_path / "img.epub"
    build_epub(articles=[_art()], output_path=out, meta=_meta(), with_images=True)

    with zipfile.ZipFile(out) as zf:
        parts = _all_chapter_htmls(zf)

    assert len(parts) == 2, f"expected 2 spine items, got {len(parts)}: {[p[0] for p in parts]}"
    cover_name, cover = parts[0]
    body_name, body = parts[1]
    assert cover_name.endswith("_cover.xhtml")
    assert not body_name.endswith("_cover.xhtml")

    # Cover has: h1, subtitle, image; NO summary body, NO footer.
    assert '<h1>' in cover
    assert 'class="lead-image"' in cover
    assert 'class="summary"' not in cover
    assert 'class="attribution"' not in cover

    # Body has: h1 (repeated for context), summary, footer; NO image.
    assert '<h1>' in body
    assert 'class="summary"' in body
    assert 'class="attribution"' in body
    assert 'class="lead-image"' not in body


def test_textonly_article_produces_one_spine_item(tmp_path: Path):
    """No image = no cover page = no wasted title-only opening page."""
    out = tmp_path / "text.epub"
    build_epub(articles=[_art(with_image=False)], output_path=out,
               meta=_meta(), with_images=False)
    with zipfile.ZipFile(out) as zf:
        parts = _all_chapter_htmls(zf)

    assert len(parts) == 1, [p[0] for p in parts]
    _, html = parts[0]
    # All content on one page.
    assert '<h1>' in html
    assert 'class="desc"' in html
    assert 'class="summary"' in html
    assert 'class="attribution"' in html
    assert 'class="lead-image"' not in html


def test_mixed_articles_split_correctly(tmp_path: Path, monkeypatch):
    """One article with image (2 pages), one without (1 page) = 3 spine items."""
    from randompedia import epub_build
    monkeypatch.setattr(epub_build, "ImagePipeline", _FakePipeline)

    out = tmp_path / "mixed.epub"
    build_epub(articles=[_art(with_image=True), _art(with_image=False)],
               output_path=out, meta=_meta(), with_images=True)

    with zipfile.ZipFile(out) as zf:
        parts = _all_chapter_htmls(zf)
        # And the OPF spine should list them in order, along with colophon.
        opf = zf.read("EPUB/content.opf").decode("utf-8")

    assert len(parts) == 3, [p[0] for p in parts]
    covers = [p for p in parts if p[0].endswith("_cover.xhtml")]
    assert len(covers) == 1


def test_toc_has_one_entry_per_article_not_per_spine_item(
    tmp_path: Path, monkeypatch,
):
    """A user browsing the TOC should see one entry per article, not two
    (cover + body). The nav document lists only the body pages."""
    from randompedia import epub_build
    monkeypatch.setattr(epub_build, "ImagePipeline", _FakePipeline)

    out = tmp_path / "toc.epub"
    articles = [_art(with_image=True), _art(with_image=False)]
    build_epub(articles=articles, output_path=out, meta=_meta(),
               with_images=True)

    with zipfile.ZipFile(out) as zf:
        nav_name = next(n for n in zf.namelist() if "nav" in n.lower() and n.endswith(".xhtml"))
        nav = zf.read(nav_name).decode("utf-8")

    # Cover pages must not appear in nav; body pages must.
    assert "_cover.xhtml" not in nav, (
        "cover pages leak into the TOC — users would see two entries per article"
    )
    # Exactly two article entries + colophon.
    body_links = re.findall(r'href="[^"]*?ch\d+_[^"]+\.xhtml"', nav)
    assert len(body_links) == len(articles), (
        f"expected {len(articles)} article TOC entries, got {len(body_links)}: {body_links}"
    )


def test_nav_and_ncx_exist_but_are_not_in_the_reading_flow(tmp_path: Path):
    """The nav and NCX must exist as manifest items (EPUB 3 requires nav,
    and capable readers use them to render a TOC menu) — but they must
    NOT be spine items. CrossPoint has no interactive TOC, so a spine
    entry pointing at nav would force readers to page through thousands
    of link-only entries before reaching the first article."""
    out = tmp_path / "nav_placement.epub"
    articles = [_art(with_image=False) for _ in range(3)]
    build_epub(articles=articles, output_path=out, meta=_meta(),
               with_images=False)

    with zipfile.ZipFile(out) as zf:
        opf = zf.read("EPUB/content.opf").decode("utf-8")

    # Nav exists in the manifest.
    assert re.search(r'<item[^>]*\bproperties="nav"', opf), (
        "EPUB 3 requires a nav document in the manifest"
    )
    assert re.search(r'<item[^>]*\bid="ncx"', opf), (
        "NCX should also be present for EPUB 2 readers"
    )

    # But NEITHER appears in the spine.
    spine_ids = re.findall(r'<itemref[^>]*\bidref="([^"]+)"', opf)
    assert "nav" not in spine_ids, (
        f"nav must not be in the spine (would waste hundreds of pages on "
        f"CrossPoint). spine={spine_ids}"
    )
    assert "ncx" not in spine_ids

    # First spine item should be the colophon (users open on 'About this
    # book'). We deliberately do NOT ship a generated cover: on
    # CrossPoint 1.4.1 the `set_cover`-produced <meta name="cover"> /
    # cover-image / cover.xhtml chain crashed the reader at file open
    # (2026-07 release aborted immediately after 'Hardware detect').
    # Removing set_cover fixed the crash. Capable readers fall back to
    # a title-based library thumbnail, which is fine.
    assert spine_ids[0] == "colophon", (
        f"expected spine to start on colophon, got {spine_ids[0]}"
    )
    assert "cover" not in spine_ids, (
        f"a generated cover crashes CrossPoint 1.4.1 — don't ship one. "
        f"spine={spine_ids}"
    )


def test_no_generated_cover_is_shipped(tmp_path: Path):
    """Regression for the 2026-07 release: shipping a Pillow-rendered
    cover via `book.set_cover` crashed CrossPoint 1.4.1 at file open.
    The abort happened immediately after 'Hardware detect', before any
    content log line. Removing set_cover — even with cover.xhtml added
    to the spine as linear=\"no\" — was the only thing that made the
    book openable on-device. Pin the absence."""
    out = tmp_path / "no_cover.epub"
    articles = [_art(with_image=False) for _ in range(3)]
    build_epub(articles=articles, output_path=out, meta=_meta(),
               with_images=False)

    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        opf = zf.read("EPUB/content.opf").decode("utf-8")

    # No cover files in the archive.
    assert not any("cover.jpg" in n or n.endswith("/cover.xhtml")
                   or n == "EPUB/cover.xhtml"
                   for n in names), (
        f"no generated cover files should ship. found: "
        f"{[n for n in names if 'cover' in n.lower()]}"
    )
    # No cover metadata in the OPF.
    assert 'properties="cover-image"' not in opf, (
        "no cover-image manifest property (crashes CrossPoint)"
    )
    assert not re.search(r'<meta[^>]*\bname="cover"', opf), (
        "no <meta name=\"cover\"> in the OPF (crashes CrossPoint)"
    )


def test_layout_order_within_cover(tmp_path: Path, monkeypatch):
    """Cover: h1 → desc → image, in that order."""
    from randompedia import epub_build
    monkeypatch.setattr(epub_build, "ImagePipeline", _FakePipeline)

    out = tmp_path / "cover_order.epub"
    build_epub(articles=[_art()], output_path=out, meta=_meta(), with_images=True)

    with zipfile.ZipFile(out) as zf:
        parts = _all_chapter_htmls(zf)
    cover = next(html for name, html in parts if name.endswith("_cover.xhtml"))

    positions = {}
    for label, needle in [
        ("h1", "<h1>"),
        ("desc", 'class="desc"'),
        ("image", '<figure class="lead-image">'),
    ]:
        idx = cover.find(needle)
        assert idx != -1, f"missing {label} in cover"
        positions[label] = idx
    order = ["h1", "desc", "image"]
    for a, b in zip(order, order[1:]):
        assert positions[a] < positions[b], f"{a} should precede {b}: {positions}"


def test_layout_without_desc(tmp_path: Path):
    out = tmp_path / "no_desc.epub"
    build_epub(articles=[_art(with_desc=False, with_image=False)],
               output_path=out, meta=_meta(), with_images=False)
    with zipfile.ZipFile(out) as zf:
        html = _chapter_html(zf)
    assert '<h1>' in html
    assert 'class="desc"' not in html


def test_attribution_footer_has_wikipedia_link_and_license_text(tmp_path: Path):
    """Per-chapter footer is short and text-only for CrossPoint compatibility:
    a link back to the article and a plain-text 'CC BY-SA 4.0' — the full
    license URL and prose live in the colophon."""
    out = tmp_path / "attr.epub"
    build_epub(articles=[_art(with_image=False)], output_path=out,
               meta=_meta(), with_images=False)
    with zipfile.ZipFile(out) as zf:
        html = _chapter_html(zf)
    assert "https://en.wikipedia.org/wiki/Sample" in html
    assert "CC BY-SA 4.0" in html
    # Chapter footer should NOT carry the full license URL (it's in the colophon).
    assert "creativecommons.org" not in html


def test_chapter_footer_uses_text_separator_not_hr_or_border(tmp_path: Path):
    """CrossPoint's renderer ignores <hr> and border/padding CSS, so the
    footer's visual separator must be an actual character in the text.
    It also ignores `display` (except `display: none`), so the separator
    and attribution must use naturally-block tags (<p>) to avoid running
    together on one line."""
    out = tmp_path / "sep.epub"
    build_epub(articles=[_art(with_image=False)], output_path=out,
               meta=_meta(), with_images=False)
    with zipfile.ZipFile(out) as zf:
        html = _chapter_html(zf)
        css_name = next(n for n in zf.namelist() if n.endswith(".css"))
        css = zf.read(css_name).decode("utf-8")

    # The separator element must be a <p> (a natural block on FreeInkBook),
    # not a <div> (which flows inline there because `display: block` is ignored).
    assert '<p class="footer-rule">' in html, (
        "footer separator must use <p>, not <div>: CrossPoint ignores "
        "`display: block`, causing <div>s to concatenate inline"
    )
    assert '<div class="footer-rule">' not in html
    # Same requirement for the attribution line itself.
    assert '<p class="attribution">' in html
    assert '<div class="attribution">' not in html

    # The separator's text content must include visible glyphs (not just markup),
    # since we cannot draw a horizontal rule via CSS.
    m = re.search(r'<p class="footer-rule">([^<]+)</p>', html)
    assert m and m.group(1).strip(), "the .footer-rule element should contain visible text"

    # CSS deliberately avoids border and padding on the attribution block
    # (unsupported by CrossPoint's FreeInkBook renderer). This will fail
    # if someone re-adds them.
    attr_block_start = css.index(".attribution")
    attr_block = css[attr_block_start:attr_block_start + 400]
    assert "border" not in attr_block, "CrossPoint's renderer ignores borders"
    assert "padding" not in attr_block, "CrossPoint's renderer ignores padding"


def test_css_has_no_page_break_rules(tmp_path: Path):
    """CrossPoint ignores every `page-break-*` and `break-*` CSS property.
    We should not be relying on them for correctness; pagination comes
    from spine boundaries, not markup. The properties MAY still appear as
    hints for other readers, but we assert here that no `.pagebreak`
    element (which we used to synthesise) leaks into rendered chapters."""
    out = tmp_path / "no_pb.epub"
    build_epub(articles=[_art(with_image=False)], output_path=out,
               meta=_meta(), with_images=False)
    with zipfile.ZipFile(out) as zf:
        for _, html in _all_chapter_htmls(zf):
            assert 'class="pagebreak"' not in html, (
                "the ad-hoc .pagebreak hack was removed; use spine splits instead"
            )


def test_colophon_page_exists_and_has_attribution(tmp_path: Path):
    out = tmp_path / "colo.epub"
    build_epub(articles=[_art(with_image=False)], output_path=out,
               meta=_meta(), with_images=False)
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert any("colophon" in n for n in names), names
        colophon = zf.read(next(n for n in names if "colophon" in n and n.endswith(".xhtml")))
        text = colophon.decode("utf-8")
    assert "Wikipedia" in text
    assert "CC BY-SA" in text or "Creative Commons" in text
    assert "Wikimedia Commons" in text  # our fair-use exclusion policy


def test_epub_metadata_declares_cc_by_sa(tmp_path: Path):
    out = tmp_path / "meta.epub"
    build_epub(articles=[_art(with_image=False)], output_path=out,
               meta=_meta(), with_images=False)
    with zipfile.ZipFile(out) as zf:
        opf = zf.read("EPUB/content.opf").decode("utf-8")
    assert "CC BY-SA 4.0" in opf
