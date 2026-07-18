"""Tests for the ranker: junk-title filter, date iteration, cache, and the
adaptive early-stop when enough unique titles have been collected."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from unittest.mock import patch

from randompedia.ranker import (
    _JUNK_TITLE_RE,
    _days_back,
    PageviewsCache,
    RankedTitle,
    rank_top_articles,
)


# --- junk-title filter ------------------------------------------------------

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


# --- _days_back iteration ----------------------------------------------------

def test_days_back_yields_most_recent_first_and_skips_today():
    days = list(_days_back(today=dt.date(2026, 3, 15), limit=5))
    # Skip today (Mar 15) and yesterday (Mar 14) — yesterday's data is
    # often not yet published when the workflow runs. Start at Mar 13.
    assert days == [
        dt.date(2026, 3, 13), dt.date(2026, 3, 12), dt.date(2026, 3, 11),
        dt.date(2026, 3, 10), dt.date(2026, 3, 9),
    ]


def test_days_back_crosses_month_boundary():
    days = list(_days_back(today=dt.date(2026, 3, 3), limit=4))
    assert days == [
        dt.date(2026, 3, 1), dt.date(2026, 2, 28),
        dt.date(2026, 2, 27), dt.date(2026, 2, 26),
    ]


def test_days_back_crosses_year_boundary():
    days = list(_days_back(today=dt.date(2026, 1, 3), limit=3))
    assert days == [dt.date(2026, 1, 1), dt.date(2025, 12, 31), dt.date(2025, 12, 30)]


# --- PageviewsCache ---------------------------------------------------------

def test_pageviews_cache_roundtrip(tmp_path: Path):
    cache = PageviewsCache(tmp_path / "pv.sqlite")
    entries = [{"article": "Cristiano_Ronaldo", "views": 100000},
               {"article": "World_War_II", "views": 90000}]
    cache.put("en", dt.date(2026, 5, 12), entries)

    got = cache.get("en", dt.date(2026, 5, 12))
    assert got == entries

    # Miss on unknown date.
    assert cache.get("en", dt.date(2026, 5, 13)) is None
    # Miss on different lang.
    assert cache.get("de", dt.date(2026, 5, 12)) is None


def test_pageviews_cache_overwrites_same_day(tmp_path: Path):
    cache = PageviewsCache(tmp_path / "pv.sqlite")
    cache.put("en", dt.date(2026, 5, 12), [{"article": "A", "views": 1}])
    cache.put("en", dt.date(2026, 5, 12), [{"article": "B", "views": 2}])
    got = cache.get("en", dt.date(2026, 5, 12))
    assert got == [{"article": "B", "views": 2}]


# --- rank_top_articles ------------------------------------------------------

def _fake_day_entries(seed: int, n: int = 1000) -> list[dict]:
    """Fake response: 1000 entries, mostly unique to `seed`, with some
    overlap with other days (matches real behavior — the same handful of
    articles top the list every day)."""
    entries = []
    # Constant "always trending" set.
    for i in range(50):
        entries.append({"article": f"Trending_{i}", "views": 100_000 - i})
    # Day-specific novelties.
    for i in range(n - 50):
        entries.append({"article": f"Day{seed}_Article_{i}", "views": 10_000 - i})
    return entries


def test_ranker_stops_early_when_target_met(tmp_path: Path):
    """The core bug this whole file is fixing: the ranker used to walk a
    fixed 12 months and quietly cap out around 5300 unique titles. It
    must now keep walking days until it has enough for the requested
    count, then stop."""
    cache = PageviewsCache(tmp_path / "pv.sqlite")
    day_calls: list[dt.date] = []

    def fake_fetch(client, project, day):
        day_calls.append(day)
        return _fake_day_entries(seed=day.toordinal())

    with patch("randompedia.ranker._fetch_day", side_effect=fake_fetch):
        # Ask for 1000 unique titles. With target_multiplier=2.0 we
        # aim for 2000 uniques. Each fake day adds ~950 new titles,
        # so 2-3 days should suffice.
        result = rank_top_articles(
            lang="en", count=1000, max_days=365, cache=cache,
        )

    assert len(result) == 1000
    # Should NOT have walked anywhere near 365 days.
    assert len(day_calls) < 10, (
        f"expected early stop, but walked {len(day_calls)} days"
    )


def test_ranker_walks_all_max_days_if_target_never_met(tmp_path: Path):
    """If real-world uniqueness is so low that we never reach the target,
    the ranker should walk up to max_days without infinite-looping."""
    cache = PageviewsCache(tmp_path / "pv.sqlite")

    # Every day returns the SAME 100 articles — no new uniques ever.
    stuck_entries = [{"article": f"Same_{i}", "views": 100} for i in range(100)]

    def fake_fetch(client, project, day):
        return list(stuck_entries)

    with patch("randompedia.ranker._fetch_day", side_effect=fake_fetch):
        result = rank_top_articles(
            lang="en", count=1000, max_days=15, cache=cache,
        )

    # We only have 100 unique articles across all days; result must be
    # exactly that, capped at count.
    assert len(result) == 100


def test_ranker_uses_cache_and_skips_fetch(tmp_path: Path):
    """A warm cache means zero network calls."""
    cache = PageviewsCache(tmp_path / "pv.sqlite")
    today = dt.date(2026, 3, 15)
    # Pre-populate: 5 recent days, each with a big-enough entries set.
    for day in list(_days_back(today=today, limit=5)):
        cache.put("en", day, _fake_day_entries(seed=day.toordinal()))

    with patch("randompedia.ranker._fetch_day",
               side_effect=AssertionError("network was called on a warm cache")):
        result = rank_top_articles(
            lang="en", count=500, max_days=365, cache=cache, today=today,
        )

    assert len(result) == 500


def test_ranker_filters_junk_titles(tmp_path: Path):
    cache = PageviewsCache(tmp_path / "pv.sqlite")

    def fake_fetch(client, project, day):
        return [
            {"article": "Main_Page",              "views": 10_000_000},
            {"article": "Special:Search",         "views": 5_000_000},
            {"article": "Wikipedia:Featured",     "views": 1_000_000},
            {"article": "-",                      "views": 500_000},
            {"article": "Mercury_(disambiguation)", "views": 400_000},
            {"article": "Cristiano_Ronaldo",      "views": 300_000},
            {"article": "World_War_II",           "views": 200_000},
        ]

    with patch("randompedia.ranker._fetch_day", side_effect=fake_fetch):
        result = rank_top_articles(
            lang="en", count=10, max_days=5, cache=cache,
        )

    titles = {r.title for r in result}
    assert "Main_Page" not in titles
    assert "Special:Search" not in titles
    assert "Wikipedia:Featured" not in titles
    assert "-" not in titles
    assert "Mercury_(disambiguation)" not in titles
    assert "Cristiano_Ronaldo" in titles
    assert "World_War_II" in titles


def test_ranker_scores_are_sums_across_days(tmp_path: Path):
    """A title that appears every day should rank higher than one that
    appears just once, even if the once-day view count is huge."""
    cache = PageviewsCache(tmp_path / "pv.sqlite")

    def fake_fetch(client, project, day):
        # A constant title (100 views every day) plus a spike-day title.
        entries = [{"article": "Steady", "views": 100}]
        if day == dt.date(2026, 3, 10):
            entries.append({"article": "Spike", "views": 500})
        return entries

    with patch("randompedia.ranker._fetch_day", side_effect=fake_fetch):
        result = rank_top_articles(
            lang="en", count=2, max_days=10, cache=cache,
            today=dt.date(2026, 3, 15),
        )

    scores = {r.title: r.score for r in result}
    # Steady appears 8 days (from Mar 13 back to Mar 6) × 100 views = 800.
    # Spike appears once × 500 views = 500.
    assert scores["Steady"] > scores["Spike"], scores
