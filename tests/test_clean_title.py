"""Tests for title cleanup — the regression that started this whole thing."""
from randompedia.summaries import clean_title


def test_strips_wikipedia_displaytitle_wrapper():
    raw = '<span lang="en" dir="ltr"><span class="mw-page-title-main">Cristiano Ronaldo</span></span>'
    assert clean_title(raw) == "Cristiano Ronaldo"


def test_preserves_apostrophe():
    assert clean_title("Catherine O'Hara") == "Catherine O'Hara"


def test_decodes_entities():
    assert clean_title("Foo &amp; Bar") == "Foo & Bar"
    assert clean_title("Rock &#38; Roll") == "Rock & Roll"


def test_flattens_italic_tags():
    # Some pages have italic titles via displaytitle (species, film titles).
    raw = '<i>Homo sapiens</i>'
    assert clean_title(raw) == "Homo sapiens"


def test_handles_nested_tags():
    raw = '<span><b><i>The <u>Godfather</u></i></b></span>'
    assert clean_title(raw) == "The Godfather"


def test_collapses_whitespace():
    assert clean_title("Foo\n\t  Bar") == "Foo Bar"


def test_handles_empty_and_none_ish():
    assert clean_title("") == ""
    assert clean_title(None) == ""  # type: ignore[arg-type]


def test_handles_malformed_html_gracefully():
    # Unclosed tag — parser should tolerate.
    assert clean_title("<span>Foo") == "Foo"


def test_self_closing_and_void_tags():
    raw = 'Line 1<br/>Line 2<br>Line 3'
    # We collapse whitespace, so <br> boundaries become spaces or nothing;
    # both are acceptable — assert the important part: no tag leakage.
    result = clean_title(raw)
    assert "<" not in result and ">" not in result
    assert "Line 1" in result and "Line 3" in result


def test_leaves_unicode_alone():
    assert clean_title("Bj\u00f6rk") == "Bj\u00f6rk"
    assert clean_title("\u4e2d\u56fd") == "\u4e2d\u56fd"
