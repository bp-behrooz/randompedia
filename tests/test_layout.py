"""Tests for chapter layout inside a bundle xhtml.

Since the 2026-07 crash fix, ALL articles land inside bundle xhtml files
(b0000.xhtml, b0001.xhtml, ...) — one bundle per _BUNDLE_SIZE articles.
This caps CrossPoint's per-spine RAM allocations at ~ceil(N/BUNDLE_SIZE)
instead of N (see BookMetadataCache::buildBookBin in the CrossPoint
source, which allocates several std::deque<T>(spineCount) structures
during first-open indexing). Per-article page breaks are preserved by an
`<a id="a{i}">` anchor before each article's <h1>, listed in the TOC as
`b{n}.xhtml#a{i}` — CrossPoint honors that anchor as a forced page break
(ChapterHtmlSlimParser.cpp:194)."""
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
    """Return [(filename, content), ...] for every bundle xhtml in
    SPINE order. Excludes nav.xhtml and colophon.xhtml."""
    opf = zf.read("EPUB/content.opf").decode("utf-8")
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
        if "nav" not in n.lower() and "colophon" not in n.lower()
    ]
    return [(n, zf.read(n).decode("utf-8")) for n in filtered]


def _article_blocks(bundle_html: str) -> list[str]:
    """Split a bundle xhtml into per-article HTML blocks, one per
    `<a id="aN">` anchor. Each returned block starts at the anchor and
    runs to the next anchor (or end of body)."""
    # Split on the anchor pattern, keeping the anchor with the following
    # content by using a lookahead.
    parts = re.split(r'(?=<a id="a\d+"></a>)', bundle_html)
    return [p for p in parts if '<a id="a' in p]


def _chapter_html(zf: zipfile.ZipFile) -> str:
    """First article block from the first bundle. Legacy helper — most
    tests want the whole bundle content anyway since it's the only
    reading surface."""
    parts = _all_chapter_htmls(zf)
    assert parts, "no bundle file found"
    return parts[0][1]


def test_image_article_lives_in_a_bundle_with_a_lead_image_figure(
    tmp_path: Path, monkeypatch,
):
    """An article with an image gets rendered inline as a single block
    inside a bundle: title, description, lead image, summary, footer.
    No separate 'cover' spine item exists any more — CrossPoint honors
    TOC-anchor page breaks so we don't need a spine split to force a
    page turn."""
    from randompedia import epub_build
    monkeypatch.setattr(epub_build, "ImagePipeline", _FakePipeline)

    out = tmp_path / "img.epub"
    build_epub(articles=[_art()], output_path=out, meta=_meta(), with_images=True)

    with zipfile.ZipFile(out) as zf:
        parts = _all_chapter_htmls(zf)

    # Exactly one bundle (1 article, bundle size >= 1).
    assert len(parts) == 1, f"expected 1 bundle, got {[p[0] for p in parts]}"
    bundle_name, bundle = parts[0]
    assert re.match(r'EPUB/b\d+\.xhtml$', bundle_name), bundle_name

    blocks = _article_blocks(bundle)
    assert len(blocks) == 1, f"expected 1 article block, got {len(blocks)}"
    block = blocks[0]
    # All the pieces coexist in the same block.
    assert '<h1>' in block
    assert 'class="desc"' in block
    assert 'class="lead-image"' in block
    assert 'class="summary"' in block
    assert 'class="attribution"' in block


def test_textonly_article_lives_in_a_bundle_without_a_lead_image(tmp_path: Path):
    """No image = the block skips the <figure>, but everything else is
    the same as an image article. Still exactly one bundle."""
    out = tmp_path / "text.epub"
    build_epub(articles=[_art(with_image=False)], output_path=out,
               meta=_meta(), with_images=False)
    with zipfile.ZipFile(out) as zf:
        parts = _all_chapter_htmls(zf)

    assert len(parts) == 1, [p[0] for p in parts]
    _, bundle = parts[0]
    blocks = _article_blocks(bundle)
    assert len(blocks) == 1
    block = blocks[0]
    assert '<h1>' in block
    assert 'class="desc"' in block
    assert 'class="summary"' in block
    assert 'class="attribution"' in block
    assert 'class="lead-image"' not in block


def test_multiple_articles_share_a_bundle(tmp_path: Path, monkeypatch):
    """Two articles (one with image, one without) go into ONE bundle
    xhtml — not one spine item per article. Each is delimited by its
    own <a id="aN"> anchor so CrossPoint's TOC-anchor page-break logic
    starts each on a fresh screen."""
    from randompedia import epub_build
    monkeypatch.setattr(epub_build, "ImagePipeline", _FakePipeline)

    out = tmp_path / "mixed.epub"
    build_epub(articles=[_art(with_image=True), _art(with_image=False)],
               output_path=out, meta=_meta(), with_images=True)

    with zipfile.ZipFile(out) as zf:
        parts = _all_chapter_htmls(zf)

    # Both articles share the single bundle.
    assert len(parts) == 1, [p[0] for p in parts]
    _, bundle = parts[0]
    blocks = _article_blocks(bundle)
    assert len(blocks) == 2, f"expected 2 article blocks, got {len(blocks)}"
    # First article has the image, second doesn't.
    assert 'class="lead-image"' in blocks[0]
    assert 'class="lead-image"' not in blocks[1]
    # The anchor ids are consecutive (a1, a2) so the TOC can point at them.
    assert '<a id="a1"></a>' in blocks[0]
    assert '<a id="a2"></a>' in blocks[1]


def test_bundling_splits_at_bundle_size_boundary(tmp_path: Path):
    """More than _BUNDLE_SIZE articles produces more than one bundle
    xhtml. This is the whole point of the refactor: caps the spine
    count at ceil(N / _BUNDLE_SIZE) so CrossPoint's per-spine RAM
    allocations don't blow the ~380 KB ESP32-C3 heap."""
    from randompedia.epub_build import _BUNDLE_SIZE
    n = _BUNDLE_SIZE + 5
    out = tmp_path / "over.epub"
    articles = [_art(with_image=False) for _ in range(n)]
    build_epub(articles=articles, output_path=out, meta=_meta(),
               with_images=False)

    with zipfile.ZipFile(out) as zf:
        parts = _all_chapter_htmls(zf)

    assert len(parts) == 2, (
        f"expected 2 bundles for N={n}, got {len(parts)}: {[p[0] for p in parts]}"
    )
    # First bundle is full, second holds the tail.
    assert len(_article_blocks(parts[0][1])) == _BUNDLE_SIZE
    assert len(_article_blocks(parts[1][1])) == 5


def test_toc_lists_every_article_by_anchor(tmp_path: Path, monkeypatch):
    """The nav document must contain one entry per article, each pointing
    at `b{n}.xhtml#a{i}`. Without these anchors CrossPoint would only
    break pages at bundle boundaries and 100 articles would flow
    together on a few pages."""
    from randompedia import epub_build
    monkeypatch.setattr(epub_build, "ImagePipeline", _FakePipeline)

    articles = [_art(with_image=(i % 2 == 0)) for i in range(3)]
    out = tmp_path / "toc.epub"
    build_epub(articles=articles, output_path=out, meta=_meta(),
               with_images=True)

    with zipfile.ZipFile(out) as zf:
        nav_name = next(n for n in zf.namelist()
                        if n.endswith("nav.xhtml") and "nav" in n.lower())
        nav = zf.read(nav_name).decode("utf-8")

    # One anchored href per article.
    anchored = re.findall(r'href="b\d+\.xhtml#a\d+"', nav)
    assert len(anchored) == len(articles), (
        f"expected {len(articles)} anchored TOC entries, got {len(anchored)}: "
        f"{anchored}"
    )


def test_nav_and_ncx_exist_but_are_not_in_the_reading_flow(tmp_path: Path):
    """The nav and NCX must exist as manifest items (EPUB 3 requires nav,
    and CrossPoint reads the nav to build its per-spine tocAnchors list
    that drives the anchor-triggered page breaks) — but they must NOT
    be spine items. CrossPoint has no interactive TOC UI, so a spine
    entry pointing at nav would force readers to page through a
    link-only document before reaching the first article."""
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
        f"nav must not be in the spine (would waste pages on CrossPoint). "
        f"spine={spine_ids}"
    )
    assert "ncx" not in spine_ids

    # First spine item should be the colophon (users open on 'About this
    # book'). We deliberately do NOT ship a generated cover: on
    # CrossPoint 1.4.1 the `set_cover`-produced <meta name="cover"> /
    # cover-image / cover.xhtml chain crashed the reader at file open
    # (2026-07 release aborted immediately after 'Hardware detect').
    assert spine_ids[0] == "colophon", (
        f"expected spine to start on colophon, got {spine_ids[0]}"
    )
    assert "cover" not in spine_ids, (
        f"a generated cover crashes CrossPoint 1.4.1 — don't ship one. "
        f"spine={spine_ids}"
    )


def test_spine_count_is_bounded_by_bundle_size(tmp_path: Path):
    """CrossPoint's BookMetadataCache::buildBookBin allocates several
    std::deque<T>(spineCount) structures in RAM during first-open
    indexing. With the 2026-07 release (one spine item per article)
    5k+ books crashed the reader immediately after 'Hardware detect';
    1k books opened. The bundling refactor caps spineCount at
    ceil(N / _BUNDLE_SIZE) + 1 (the +1 is the colophon). Pin that."""
    from randompedia.epub_build import _BUNDLE_SIZE
    # Pick N such that we get several bundles.
    n = _BUNDLE_SIZE * 3 + 7
    out = tmp_path / "bounded.epub"
    articles = [_art(with_image=False) for _ in range(n)]
    build_epub(articles=articles, output_path=out, meta=_meta(),
               with_images=False)

    with zipfile.ZipFile(out) as zf:
        opf = zf.read("EPUB/content.opf").decode("utf-8")
    spine_ids = re.findall(r'<itemref[^>]*\bidref="([^"]+)"', opf)
    expected_bundles = (n + _BUNDLE_SIZE - 1) // _BUNDLE_SIZE
    # colophon + bundles
    assert len(spine_ids) == 1 + expected_bundles, (
        f"expected {1 + expected_bundles} spine items for N={n} "
        f"(bundle_size={_BUNDLE_SIZE}), got {len(spine_ids)}: {spine_ids}"
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


def test_layout_order_within_article_block(tmp_path: Path, monkeypatch):
    """Inside an image-bearing article block: anchor -> h1 -> desc ->
    image -> summary -> footer, in that order. The anchor MUST come
    first (it's what CrossPoint uses to force the page break)."""
    from randompedia import epub_build
    monkeypatch.setattr(epub_build, "ImagePipeline", _FakePipeline)

    out = tmp_path / "order.epub"
    build_epub(articles=[_art()], output_path=out, meta=_meta(), with_images=True)

    with zipfile.ZipFile(out) as zf:
        _, bundle = _all_chapter_htmls(zf)[0]
    block = _article_blocks(bundle)[0]

    positions = {}
    for label, needle in [
        ("anchor", '<a id="a1"></a>'),
        ("h1", "<h1>"),
        ("desc", 'class="desc"'),
        ("image", '<figure class="lead-image">'),
        ("summary", 'class="summary"'),
        ("footer", 'class="attribution"'),
    ]:
        idx = block.find(needle)
        assert idx != -1, f"missing {label} in article block"
        positions[label] = idx
    order = ["anchor", "h1", "desc", "image", "summary", "footer"]
    for a, b in zip(order, order[1:]):
        assert positions[a] < positions[b], (
            f"{a} should precede {b}: {positions}"
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
