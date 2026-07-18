"""Tests for the language-code → display-name helper and its use in the
colophon."""
import io
import zipfile
from pathlib import Path

from randompedia.epub_build import (
    BookMeta,
    build_epub,
    language_display_name,
    stable_book_id,
)
from randompedia.summaries import ArticleSummary


def test_known_codes_map_to_english_names():
    assert language_display_name("en") == "English"
    assert language_display_name("de") == "German"
    assert language_display_name("fr") == "French"
    assert language_display_name("ja") == "Japanese"
    assert language_display_name("zh") == "Chinese"
    assert language_display_name("ar") == "Arabic"


def test_case_insensitive():
    assert language_display_name("EN") == "English"
    assert language_display_name("En") == "English"


def test_unknown_code_falls_back_to_uppercase():
    # We deliberately don't invent names we're not sure of.
    assert language_display_name("sw") == "SW"
    assert language_display_name("qq") == "QQ"


def test_empty_string_returns_generic_fallback():
    # Should never produce something ugly like "-language Wikipedia".
    assert language_display_name("") == "Wikipedia"


def _sample_article() -> ArticleSummary:
    return ArticleSummary(
        title="Sample", key="Sample", lang="en", description=None,
        extract_html="<p>x</p>", extract_text="x",
        image_url=None, raw_image_url=None,
        url="https://en.wikipedia.org/wiki/Sample",
    )


def _read_colophon(zf: zipfile.ZipFile) -> str:
    name = next(n for n in zf.namelist() if "colophon" in n and n.endswith(".xhtml"))
    return zf.read(name).decode("utf-8")


def test_colophon_uses_english_language_name(tmp_path: Path):
    """This is the regression: the colophon used to say 'en-language Wikipedia'."""
    out = tmp_path / "en.epub"
    meta = BookMeta(title="t", identifier=stable_book_id("t", "en", "1", "s"),
                    lang="en", seed="s")
    build_epub(articles=[_sample_article()], output_path=out, meta=meta,
               with_images=False)
    with zipfile.ZipFile(out) as zf:
        text = _read_colophon(zf)
    assert "English Wikipedia" in text
    assert "en-language" not in text
    # The build info line should still cite the domain.
    assert "en.wikipedia.org" in text


def test_colophon_falls_back_for_unknown_language(tmp_path: Path):
    out = tmp_path / "sw.epub"
    meta = BookMeta(title="t", identifier=stable_book_id("t", "sw", "1", "s"),
                    lang="sw", seed="s")
    art = ArticleSummary(
        title="Sample", key="Sample", lang="sw", description=None,
        extract_html="<p>x</p>", extract_text="x",
        image_url=None, raw_image_url=None,
        url="https://sw.wikipedia.org/wiki/Sample",
    )
    build_epub(articles=[art], output_path=out, meta=meta, with_images=False)
    with zipfile.ZipFile(out) as zf:
        text = _read_colophon(zf)
    # We don't know the English name, so we fall back to the code in caps.
    # The important thing: no "sw-language Wikipedia" leak.
    assert "sw-language" not in text
    assert "SW Wikipedia" in text
    assert "sw.wikipedia.org" in text
