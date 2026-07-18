"""Tests for the ranker's junk-title filter and month calculation."""
import datetime as dt

from randompedia.ranker import _JUNK_TITLE_RE, _months_back


def test_junk_regex_rejects_main_page():
    assert _JUNK_TITLE_RE.match("Main_Page")


def test_junk_regex_rejects_special_namespace():
    assert _JUNK_TITLE_RE.match("Special:Search")
    assert _JUNK_TITLE_RE.match("Special:Random")


def test_junk_regex_rejects_wikipedia_namespace():
    assert _JUNK_TITLE_RE.match("Wikipedia:Featured_articles")


def test_junk_regex_rejects_disambiguation():
    assert _JUNK_TITLE_RE.match("Mercury_(disambiguation)")


def test_junk_regex_rejects_dash_placeholder():
    assert _JUNK_TITLE_RE.match("-")


def test_junk_regex_accepts_real_articles():
    for good in [
        "Cristiano_Ronaldo",
        "World_War_II",
        "Python_(programming_language)",
        "Barack_Obama",
        "OpenAI",
    ]:
        assert not _JUNK_TITLE_RE.match(good), good


def test_months_back_wraps_years():
    # From March 2026, 12 months back should land on March 2025 through Feb 2026.
    months = _months_back(12, today=dt.date(2026, 3, 15))
    assert months[0] == (2026, 2)   # most-recent completed month
    assert months[-1] == (2025, 3)
    assert len(months) == 12
    # Every month distinct.
    assert len(set(months)) == 12


def test_months_back_skips_current_month():
    # Even if today is the 1st, current month is skipped.
    months = _months_back(1, today=dt.date(2026, 6, 1))
    assert months == [(2026, 5)]


def test_months_back_january_edge():
    months = _months_back(3, today=dt.date(2026, 1, 20))
    assert months == [(2025, 12), (2025, 11), (2025, 10)]
