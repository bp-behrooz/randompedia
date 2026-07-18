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


_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9]+")


def _chapter_id(key: str, index: int) -> str:
    safe = _SAFE_ID_RE.sub("_", key)[:40].strip("_") or "article"
    return f"ch{index:05d}_{safe}"


def _render_cover_html(art: ArticleSummary, image: ProcessedImage) -> str:
    """The "cover" page of an article: title, subtitle, image. Rendered as a
    separate spine item so the summary starts on a fresh page on readers
    (e.g. CrossPoint / FreeInkBook) that don't honour CSS page-break rules.
    Spine boundaries are the only reliable page break on those engines."""
    title_esc = html.escape(art.title)
    desc = f'<p class="desc">{html.escape(art.description)}</p>' if art.description else ""
    img_html = (
        f'<figure class="lead-image">'
        f'<img src="images/{image.filename}" alt=""/></figure>'
    )
    return f'<h1>{title_esc}</h1>\n{desc}{img_html}\n'


def _render_body_html(
    art: ArticleSummary, *, index: int, total: int, repeat_title: bool,
) -> str:
    """The summary + footer page. When the article had a cover page we still
    repeat the title so a reader landing on the body page has context; when
    there was no cover, this IS the whole article page."""
    title_esc = html.escape(art.title)
    header = f'<h1>{title_esc}</h1>\n' if repeat_title else (
        f'<h1>{title_esc}</h1>\n'
        + (f'<p class="desc">{html.escape(art.description)}</p>' if art.description else "")
    )
    body = art.extract_html or f"<p>{html.escape(art.extract_text)}</p>"
    # Text-based separator + footer. We use <p> (not <div>) because
    # CrossPoint's renderer only honours `display: none` — every other
    # `display` value is ignored, meaning back-to-back <div>s collapse into
    # a single inline run there. <p> is treated as a block by default, so
    # the separator, the footer, and the summary each get their own line.
    attribution = (
        '<p class="footer-rule">· · ·</p>\n'
        f'<p class="attribution">{index}/{total} &middot; '
        f'From <a href="{html.escape(art.url)}">Wikipedia</a>, '
        f'CC BY-SA 4.0</p>'
    )
    return (
        f'{header}'
        f'<div class="summary">{body}</div>\n'
        f'{attribution}\n'
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
    toc_entries: list[epub.EpubHtml] = []  # what shows up in the reader's TOC
    spine_items: list[epub.EpubHtml] = []  # every rendered page, in order
    # Progress cadence: ~5% granularity, floored so we emit something at
    # least every few seconds. GitHub Actions kills long-silent steps.
    step = max(30, min(500, max(1, total // 20)))
    images_placed = 0

    try:
        for i, art in enumerate(articles, start=1):
            image: ProcessedImage | None = None
            if pipeline and art.image_url:
                image = pipeline.fetch_and_process(art.image_url)
                if image is not None:
                    images_placed += 1
                if image and image.filename not in added_image_files:
                    item = epub.EpubItem(
                        uid=f"img_{image.filename}",
                        file_name=f"images/{image.filename}",
                        media_type=image.media_type,
                        content=image.data,
                    )
                    book.add_item(item)
                    added_image_files.add(image.filename)

            cid = _chapter_id(art.key, i)

            if image is not None:
                # Two-page article: cover (title + image) then body (summary).
                # The cover exists purely to occupy its own page on readers
                # that don't support CSS page breaks — spine boundaries are
                # the only reliable page break on CrossPoint / FreeInkBook.
                cover = epub.EpubHtml(
                    title=f"{art.title} (cover)",
                    file_name=f"{cid}_cover.xhtml",
                    lang=meta.lang,
                    uid=f"{cid}_cover",
                )
                cover.content = _render_cover_html(art, image=image)
                cover.add_item(css)
                book.add_item(cover)
                spine_items.append(cover)

                body = epub.EpubHtml(
                    title=art.title,
                    file_name=f"{cid}.xhtml",
                    lang=meta.lang,
                    uid=cid,
                )
                body.content = _render_body_html(
                    art, index=i, total=total, repeat_title=True,
                )
                body.add_item(css)
                book.add_item(body)
                spine_items.append(body)
                toc_entries.append(body)
            else:
                # Single-page article (no image): title + subtitle + summary
                # all in one spine item.
                ch = epub.EpubHtml(
                    title=art.title,
                    file_name=f"{cid}.xhtml",
                    lang=meta.lang,
                    uid=cid,
                )
                ch.content = _render_body_html(
                    art, index=i, total=total, repeat_title=False,
                )
                ch.add_item(css)
                book.add_item(ch)
                spine_items.append(ch)
                toc_entries.append(ch)

            if i % step == 0 or i == total:
                if with_images:
                    log.info("built chapter %d/%d (%.0f%%, %d images placed)",
                             i, total, 100 * i / total, images_placed)
                else:
                    log.info("built chapter %d/%d (%.0f%%)",
                             i, total, 100 * i / total)
    finally:
        if pipeline:
            pipeline.close()

    book.toc = (colophon, *toc_entries)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", colophon, *spine_items]

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
