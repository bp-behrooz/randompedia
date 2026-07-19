"""Build the EPUB from a sequence of ArticleSummary objects."""

from __future__ import annotations

import hashlib
import html
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ebooklib import epub

from . import __version__, PROJECT_URL
from .images import ImagePipeline, ImageSpec, ProcessedImage
from .summaries import ArticleSummary

log = logging.getLogger(__name__)


_CSS = """\
body { font-family: serif; line-height: 1.4; margin: 0.6em; }
h1 { font-size: 1.25em; margin: 0 0 0.15em 0; page-break-after: avoid; }
.desc { font-style: italic; margin: 0 0 0.6em 0;
        page-break-after: avoid; }
.lead-image { text-align: center; margin: 0.6em 0;
              page-break-inside: avoid; page-break-after: avoid; }
.lead-image img { max-width: 100%; height: auto; }
.summary { text-align: justify; }
p { margin: 0 0 0.5em 0; }
/* Chapter footer: small, dimmed, separated from the body by both whitespace
   and a text rule. We cannot draw a horizontal line (borders are not
   supported by CrossPoint's renderer and we can't rely on <hr> either), so
   we use a centred text separator instead. */
.footer-rule { text-align: center; margin: 1.2em 0 0.2em 0; font-size: 0.9em; }
.attribution { font-size: 0.8em; margin: 0.2em 0 0 0; text-align: center; }
.attribution a { color: inherit; }
"""


@dataclass(slots=True)
class BookMeta:
    title: str
    identifier: str
    lang: str
    seed: str


_SAFE_ID_RE = None  # historical; no longer used after bundling refactor


# XML has only five built-in named entities. Everything else — &nbsp;
# &middot; &hellip; &copy; and the whole HTML5 catalogue — is illegal
# without a DTD, and CrossPoint's expat runs in strict XML mode with no
# DTD (verified on-device: a single stray &middot; in a bundle xhtml
# aborts the parse and leaves the "Indexing" popup stuck forever).
_XML_BUILTIN_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}
# `&name;` where name is [A-Za-z][A-Za-z0-9]* — matches HTML5 named
# entities. Numeric entities (&#123; / &#xABCD;) are skipped by the
# leading `[A-Za-z]` requirement and remain untouched (valid XML).
_NAMED_ENTITY_RE = re.compile(r"&([A-Za-z][A-Za-z0-9]*);")


def _xml_safe_entities(s: str) -> str:
    """Replace HTML-only named entities with their UTF-8 character so
    the result parses as strict XML. Leaves numeric entities and the
    five XML built-ins alone."""
    def repl(m: "re.Match[str]") -> str:
        name = m.group(1)
        if name in _XML_BUILTIN_ENTITIES:
            return m.group(0)
        # html.unescape handles the full HTML5 entity table. If it
        # doesn't recognise the name it returns the original text
        # unchanged, in which case we fall back to emitting the raw
        # ampersand escaped — better than shipping a hard XML error.
        decoded = html.unescape(m.group(0))
        if decoded == m.group(0):
            return "&amp;" + m.group(0)[1:]
        return decoded
    return _NAMED_ENTITY_RE.sub(repl, s)


# How many articles go into one bundle xhtml (== one spine item). (== one spine item).
#
# CrossPoint 1.4.1 allocates several `std::deque<T>(spineCount)`
# structures in RAM during BookMetadataCache::buildBookBin
# (lib/Epub/Epub/BookMetadataCache.cpp:237,269,272,291). On the ~380 KB
# ESP32-C3 heap this crashes past a few hundred spine items — we
# measured 1k working, 5k crashing.
#
# 100 gives 10k articles / 100 = 100 spine items (safely under the
# threshold) while keeping each bundle xhtml at a few hundred KB, which
# CrossPoint's streaming ChapterHtmlSlimParser handles fine (chapter
# parsing is streamed off SD, not held in RAM).
_BUNDLE_SIZE = 100


def _render_article_block(
    art: ArticleSummary, *, index: int, total: int, image: ProcessedImage | None,
) -> str:
    """Render one article as a self-contained HTML block inside a bundle
    xhtml. The leading `<a id="a{index}">` is what CrossPoint anchors on
    to force a page break at the article boundary — see
    lib/Epub/Epub/parsers/ChapterHtmlSlimParser.cpp:194 in crosspoint-reader,
    which calls flushPendingAnchor -> completePageFn whenever a `<a id>`
    matches a TOC entry for the current spine item. As long as the TOC
    lists `bundle.xhtml#a{index}` for each article, CrossPoint starts a
    fresh page here."""
    title_esc = html.escape(art.title)
    desc = (f'<p class="desc">{html.escape(art.description)}</p>'
            if art.description else "")
    img_html = ""
    if image is not None:
        img_html = (
            f'<figure class="lead-image">'
            f'<img src="images/{image.filename}" alt=""/></figure>\n'
        )
    body = _xml_safe_entities(art.extract_html or f"<p>{html.escape(art.extract_text)}</p>")
    footer = (
        '<p class="footer-rule">· · ·</p>\n'
        # Numeric entity, NOT `&middot;`: bundle xhtml is served to
        # CrossPoint's expat parser in strict XML mode with no DTD,
        # so any HTML-only named entity aborts the parse silently and
        # leaves the "Indexing" popup stuck on screen forever (verified
        # on-device on Xteink X4 / CrossPoint 1.4.1). Numeric entities
        # and the five built-in XML entities (&amp; &lt; &gt; &quot;
        # &apos;) are the only entity forms safe to emit here.
        f'<p class="attribution">{index}/{total} &#183; '
        f'From <a href="{html.escape(art.url)}">Wikipedia</a>, '
        f'CC BY-SA 4.0</p>'
    )
    # The anchor must be an EMPTY element that precedes the first
    # renderable block; CrossPoint's parser only fires the page break
    # when it sees the anchor id, not the content that follows.
    return (
        f'<a id="a{index}"></a>\n'
        f'<h1>{title_esc}</h1>\n'
        f'{desc}'
        f'{img_html}'
        f'<div class="summary">{body}</div>\n'
        f'{footer}\n'
    )


def _wrap_bundle_html(inner: str, *, lang: str, title: str) -> str:
    """Wrap a concatenation of article blocks in a minimal xhtml
    document. epub:type / role attributes are dropped — CrossPoint
    doesn't use them and they only inflate the file."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}" lang="{lang}">\n'
        f'<head><meta charset="utf-8"/><title>{html.escape(title)}</title>'
        '<link rel="stylesheet" type="text/css" href="styles/main.css"/></head>\n'
        f'<body>\n{inner}</body>\n</html>\n'
    )


def build_epub(
    *,
    articles: Iterable[ArticleSummary],
    output_path: Path,
    meta: BookMeta,
    with_images: bool,
    image_cache_dir: Path | None = None,
    image_spec: ImageSpec | None = None,
) -> None:
    articles = list(articles)
    total = len(articles)
    log.info("building %s with %d articles (images=%s)", output_path, total, with_images)

    book = epub.EpubBook()
    book.set_identifier(meta.identifier)
    book.set_title(meta.title)
    book.set_language(meta.lang)
    book.add_author("Wikipedia contributors")
    book.add_metadata("DC", "publisher", f"randompedia v{__version__}")
    book.add_metadata(
        "DC", "description",
        f"Top {total} Wikipedia articles in random order (seed={meta.seed}). "
        "Article text and images are the work of Wikipedia contributors and "
        "are licensed under CC BY-SA 4.0. See the colophon for details.",
    )
    book.add_metadata("DC", "rights",
                      "CC BY-SA 4.0 — https://creativecommons.org/licenses/by-sa/4.0/")
    book.add_metadata("DC", "source", f"https://{meta.lang}.wikipedia.org/")

    # No generated cover. A prior version called book.set_cover(...) to
    # register a Pillow-rendered JPEG as the library thumbnail, but on
    # CrossPoint 1.4.1 the resulting <meta name="cover"> / cover-image /
    # cover.xhtml chain crashed the reader at file open (verified: the
    # 2026-07 release aborted immediately after 'Hardware detect'; the
    # same book without set_cover opens fine). Capable readers fall back
    # to a title-based thumbnail, which is fine — e-ink covers are
    # transient noise and the on-device reader library thumbnails are
    # tiny anyway.

    css = epub.EpubItem(
        uid="style_main", file_name="styles/main.css",
        media_type="text/css", content=_CSS,
    )
    book.add_item(css)

    colophon = epub.EpubHtml(
        title="About this book",
        file_name="colophon.xhtml",
        lang=meta.lang,
        uid="colophon",
    )
    colophon.content = _colophon_html(meta, total=total)
    colophon.add_item(css)
    book.add_item(colophon)

    pipeline: ImagePipeline | None = None
    if with_images:
        pipeline = ImagePipeline(spec=image_spec, cache_dir=image_cache_dir)

    added_image_files: set[str] = set()
    # Bundle articles into groups of _BUNDLE_SIZE and emit ONE xhtml per
    # group as a single spine item. This is the crucial workaround for
    # CrossPoint's spine-count RAM ceiling (see the note near
    # book.spine below and lib/Epub/Epub/BookMetadataCache.cpp:237,269
    # in the CrossPoint source: several std::deque<T>(spineCount)
    # allocations sit in RAM during build_bin, so 10k spine items
    # blows the ~380 KB heap on ESP32-C3).
    #
    # Per-article page breaks are preserved by putting an empty
    # `<a id="a{index}"></a>` before each article and listing that
    # anchor in the TOC as `b{bundle}.xhtml#a{index}`. CrossPoint's
    # ChapterHtmlSlimParser::flushPendingAnchor
    # (lib/Epub/Epub/parsers/ChapterHtmlSlimParser.cpp:194) forces a
    # page break whenever it sees an anchor id that appears in the
    # current spine item's TOC-anchor list. Verified from source.
    spine_items: list[epub.EpubHtml] = []
    # (bundle_item, [(title, "b{n}.xhtml#a{index}"), ...]) — used to
    # emit the TOC nav+NCX after the whole loop completes so ebooklib
    # can build the ordered hierarchy in one pass.
    toc_entries: list[tuple[epub.EpubHtml, list[tuple[str, str]]]] = []
    # Progress cadence: ~5% granularity, floored so we emit something at
    # least every few seconds. GitHub Actions kills long-silent steps.
    step = max(30, min(500, max(1, total // 20)))
    images_placed = 0

    # State for the currently-open bundle.
    bundle_index = 0
    bundle_html_parts: list[str] = []
    bundle_toc_entries: list[tuple[str, str]] = []

    def flush_bundle() -> None:
        """Commit the current bundle as a spine item and start a fresh one."""
        nonlocal bundle_index, bundle_html_parts, bundle_toc_entries
        if not bundle_html_parts:
            return
        file_name = f"b{bundle_index:04d}.xhtml"
        item = epub.EpubItem(
            uid=f"b{bundle_index}",
            file_name=file_name,
            media_type="application/xhtml+xml",
            content=_wrap_bundle_html(
                "".join(bundle_html_parts),
                lang=meta.lang,
                title=f"Bundle {bundle_index + 1}",
            ).encode("utf-8"),
        )
        book.add_item(item)
        spine_items.append(item)
        toc_entries.append((item, bundle_toc_entries))
        bundle_index += 1
        bundle_html_parts = []
        bundle_toc_entries = []

    try:
        for i, art in enumerate(articles, start=1):
            image: ProcessedImage | None = None
            if pipeline and art.image_url:
                image = pipeline.fetch_and_process(art.image_url)
                if image is not None:
                    images_placed += 1
                if image and image.filename not in added_image_files:
                    img_item = epub.EpubItem(
                        uid=image.filename.rsplit(".", 1)[0],
                        file_name=f"images/{image.filename}",
                        media_type=image.media_type,
                        content=image.data,
                    )
                    book.add_item(img_item)
                    added_image_files.add(image.filename)

            bundle_html_parts.append(_render_article_block(
                art, index=i, total=total, image=image,
            ))
            file_name = f"b{bundle_index:04d}.xhtml"
            bundle_toc_entries.append((art.title, f"{file_name}#a{i}"))

            if len(bundle_html_parts) >= _BUNDLE_SIZE:
                flush_bundle()

            if i % step == 0 or i == total:
                if with_images:
                    log.info("built chapter %d/%d (%.0f%%, %d images placed)",
                             i, total, 100 * i / total, images_placed)
                else:
                    log.info("built chapter %d/%d (%.0f%%)",
                             i, total, 100 * i / total)
        flush_bundle()  # tail bundle if the last group wasn't full
    finally:
        if pipeline:
            pipeline.close()

    # TOC: nav + NCX list each article as `b{n}.xhtml#a{i}`. These files
    # grow linearly with article count (~150 bytes/entry), reaching ~1.5 MB
    # for a 10k book, but that's fine — CrossPoint STREAMS the nav/NCX to
    # disk (BookMetadataCache) rather than holding them in RAM. What
    # actually blows the heap is spine count, which the bundling above
    # caps at ceil(total / _BUNDLE_SIZE). At 10k articles that's 100
    # spine items instead of the original 10001, cutting the
    # BookMetadataCache::buildBookBin std::deque<T>(spineCount)
    # allocations from ~260 KB to ~2.6 KB.
    #
    # The nav and NCX are declared in the manifest (spec-required for
    # EPUB 3) but stay OUT of the spine — CrossPoint has no interactive
    # TOC UI so a spine entry pointing at nav would just be a dead page.
    book.toc = tuple(
        (epub.Section(f"Bundle {i + 1}"),
         tuple(epub.Link(href, title, f"toc_b{i}_a{n}")
               for n, (title, href) in enumerate(entries)))
        for i, (item, entries) in enumerate(toc_entries)
    )
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = [colophon, *spine_items]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    log.info("serializing epub to %s...", output_path)
    epub.write_epub(str(output_path), book)
    log.info("wrote %s (%.1f MB)", output_path, output_path.stat().st_size / 1e6)


def stable_book_id(*parts: str) -> str:
    h = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"urn:randompedia:{h}"


# English names for the Wikipedia language editions we're most likely to see.
# Covers the 14 languages the original wikitok project supports plus a handful
# of the other largest Wikipedias. For anything not listed we fall back to the
# uppercase code (e.g. "SW" for Swahili) rather than making up a name.
_LANGUAGE_NAMES: dict[str, str] = {
    "ar": "Arabic",
    "de": "German",
    "en": "English",
    "es": "Spanish",
    "fa": "Persian",
    "fi": "Finnish",
    "fr": "French",
    "he": "Hebrew",
    "hi": "Hindi",
    "id": "Indonesian",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "nl": "Dutch",
    "no": "Norwegian",
    "pl": "Polish",
    "pt": "Portuguese",
    "ru": "Russian",
    "sv": "Swedish",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "vi": "Vietnamese",
    "zh": "Chinese",
}


def language_display_name(code: str) -> str:
    """Return an English name for a Wikipedia language code (e.g. "en" -> "English").

    Falls back to the code in upper case if we don't have a mapping, so the
    output is always something sensible rather than "en-language".
    """
    if not code:
        return "Wikipedia"
    return _LANGUAGE_NAMES.get(code.lower(), code.upper())


def _colophon_html(meta: BookMeta, *, total: int) -> str:
    lang_code = meta.lang.lower()
    lang_name_esc = html.escape(language_display_name(lang_code))
    lang_code_esc = html.escape(lang_code)
    seed_esc = html.escape(meta.seed)
    return f"""\
<h1>About this book</h1>

<p>This book contains short summaries of the top {total} most-viewed
articles from the {lang_name_esc} Wikipedia over the past twelve
months, presented in a shuffled order.</p>

<h2>Attribution</h2>

<p>All article text and images are the work of the many volunteer editors
of Wikipedia. Each entry links back to the source article, where the full
edit history — and therefore the list of contributors — can be viewed.</p>

<p>Article text is licensed under the
<a href="https://creativecommons.org/licenses/by-sa/4.0/">Creative Commons
Attribution-ShareAlike 4.0 International License (CC BY-SA 4.0)</a> and the
<a href="https://www.gnu.org/licenses/fdl-1.3.html">GNU Free Documentation
License (GFDL)</a>.</p>

<p>Lead images included here come exclusively from
<a href="https://commons.wikimedia.org/">Wikimedia Commons</a> and are
freely licensed or in the public domain. Non-free (fair-use) images that
appear on Wikipedia itself have been deliberately excluded so that this
book can be freely redistributed. Each image on Commons carries its own
license; see the corresponding File page on Commons for details of the
image on any given article page.</p>

<h2>License of this book</h2>

<p>This compilation is a derivative work of Wikipedia and is itself
released under
<a href="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA 4.0</a>.
You are free to share and adapt it, provided you give appropriate
attribution, indicate any changes, and license derivative works under
compatible terms.</p>

<h2>Build information</h2>

<ul>
<li>Language: {lang_name_esc} ({lang_code_esc}.wikipedia.org)</li>
<li>Article count: {total}</li>
<li>Shuffle seed: <code>{seed_esc}</code></li>
<li>Generator: <a href="{html.escape(PROJECT_URL)}">randompedia</a> v{__version__}</li>
</ul>

<p><em>randompedia is not affiliated with, endorsed by, or sponsored by
the Wikimedia Foundation.</em></p>
"""
