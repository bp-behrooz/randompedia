"""Tests for the --include-fair-use opt-in and the cache's raw-URL storage."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from randompedia.summaries import (
    ArticleSummary,
    SummaryCache,
    SummaryFetcher,
    _is_freely_licensed_image_url,
)


def _fake_response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload,
                          request=httpx.Request("GET", "https://x"))


COMMONS_URL = "https://upload.wikimedia.org/wikipedia/commons/1/2/Free.jpg"
FAIR_USE_URL = "https://upload.wikimedia.org/wikipedia/en/1/2/Movie_Poster.jpg"


def _payload(image_url: str | None) -> dict:
    p = {
        "type": "standard",
        "title": "Sample",
        "displaytitle": "Sample",
        "description": "desc",
        "extract": "text",
        "extract_html": "<p>text</p>",
        "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Sample"}},
    }
    if image_url is not None:
        p["originalimage"] = {"source": image_url}
    return p


def _make_fetcher(tmp_path: Path, *, include_fair_use: bool,
                  fake_payload: dict) -> SummaryFetcher:
    cache = SummaryCache(tmp_path / "c.sqlite")
    f = SummaryFetcher(
        lang="en", cache=cache, rate_per_second=1000.0,
        include_fair_use_images=include_fair_use,
    )
    # Neutralize the network. `_get` is the network entry point.
    f._get = MagicMock(return_value=_fake_response(fake_payload))
    return f


def test_default_excludes_fair_use_images(tmp_path: Path):
    f = _make_fetcher(tmp_path, include_fair_use=False,
                      fake_payload=_payload(FAIR_USE_URL))
    s = f.fetch("Sample")
    assert s is not None
    assert s.image_url is None
    # But the raw URL is preserved for future re-projection.
    assert s.raw_image_url == FAIR_USE_URL


def test_opt_in_includes_fair_use_images(tmp_path: Path):
    f = _make_fetcher(tmp_path, include_fair_use=True,
                      fake_payload=_payload(FAIR_USE_URL))
    s = f.fetch("Sample")
    assert s is not None
    assert s.image_url == FAIR_USE_URL
    assert s.raw_image_url == FAIR_USE_URL


def test_commons_images_always_included(tmp_path: Path):
    for include_fu in (False, True):
        f = _make_fetcher(tmp_path / str(include_fu), include_fair_use=include_fu,
                          fake_payload=_payload(COMMONS_URL))
        s = f.fetch("Sample")
        assert s is not None, f"include_fair_use={include_fu}"
        assert s.image_url == COMMONS_URL, f"include_fair_use={include_fu}"


def test_no_image_when_api_returns_none(tmp_path: Path):
    f = _make_fetcher(tmp_path, include_fair_use=True,
                      fake_payload=_payload(None))
    s = f.fetch("Sample")
    assert s is not None
    assert s.image_url is None
    assert s.raw_image_url is None


def test_cache_stores_raw_url_and_reprojects_on_policy_change(tmp_path: Path):
    """The regression this guards against: fetching once with fair-use OFF
    and again with fair-use ON must NOT return the stale cached (None)
    image_url. The raw URL is cached; the filter is applied at read time."""
    cache_path = tmp_path / "shared.sqlite"

    # First fetcher: fair-use OFF, primes the cache from a fake response
    # containing a fair-use image.
    cache1 = SummaryCache(cache_path)
    f1 = SummaryFetcher(lang="en", cache=cache1, rate_per_second=1000.0,
                        include_fair_use_images=False)
    f1._get = MagicMock(return_value=_fake_response(_payload(FAIR_USE_URL)))
    s1 = f1.fetch("Sample")
    assert s1.image_url is None
    assert s1.raw_image_url == FAIR_USE_URL

    # Second fetcher: reuses the SAME sqlite file, fair-use ON.
    # It must NOT hit the network (the cache is warm) but must return the
    # fair-use image because policy changed.
    cache2 = SummaryCache(cache_path)
    f2 = SummaryFetcher(lang="en", cache=cache2, rate_per_second=1000.0,
                        include_fair_use_images=True)
    net = MagicMock(side_effect=AssertionError("network was called on a cache hit"))
    f2._get = net
    s2 = f2.fetch("Sample")
    net.assert_not_called()
    assert s2.image_url == FAIR_USE_URL


def test_legacy_cache_row_without_raw_image_url_is_readable(tmp_path: Path):
    """Backfill path for pre-versioned caches (user_version = 0): rows
    written before we added `raw_image_url` should still be readable."""
    import sqlite3, time
    p = tmp_path / "legacy.sqlite"
    db = sqlite3.connect(p)
    db.execute(
        "CREATE TABLE summaries ("
        "  lang TEXT NOT NULL, key TEXT NOT NULL,"
        "  fetched_at INTEGER NOT NULL, payload TEXT NOT NULL,"
        "  PRIMARY KEY (lang, key))"
    )
    legacy = {
        "title": "Old", "key": "Old", "lang": "en",
        "description": None, "extract_html": "<p>x</p>", "extract_text": "x",
        "image_url": COMMONS_URL, "url": "https://en.wikipedia.org/wiki/Old",
    }
    db.execute(
        "INSERT INTO summaries VALUES (?, ?, ?, ?)",
        ("en", "Old", int(time.time()), json.dumps(legacy)),
    )
    # No PRAGMA user_version = ... — simulates a really old cache.
    db.commit()
    db.close()

    cache = SummaryCache(p)
    got = cache.get("en", "Old")
    assert got is not None
    assert got.image_url == COMMONS_URL
    # Backfilled from image_url so future re-projections have something to work with.
    assert got.raw_image_url == COMMONS_URL


def test_stale_schema_versioned_cache_gets_dropped(tmp_path: Path):
    """If the cache was written by a version whose schema is older than the
    current SCHEMA_VERSION (and the file is versioned, e.g. user_version=1),
    the table must be rebuilt so we don't silently serve stale rows missing
    new fields."""
    import sqlite3, time
    p = tmp_path / "v1.sqlite"
    db = sqlite3.connect(p)
    db.execute(
        "CREATE TABLE summaries ("
        "  lang TEXT NOT NULL, key TEXT NOT NULL,"
        "  fetched_at INTEGER NOT NULL, payload TEXT NOT NULL,"
        "  PRIMARY KEY (lang, key))"
    )
    db.execute(
        "INSERT INTO summaries VALUES (?, ?, ?, ?)",
        ("en", "Old", int(time.time()),
         json.dumps({"title": "Old", "key": "Old", "lang": "en",
                     "description": None, "extract_html": "<p>x</p>",
                     "extract_text": "x", "image_url": None,
                     "url": "https://en.wikipedia.org/wiki/Old"})),
    )
    # Mark as an older versioned schema (SCHEMA_VERSION at time of writing is 2).
    db.execute("PRAGMA user_version = 1")
    db.commit()
    db.close()

    cache = SummaryCache(p)
    assert cache.get("en", "Old") is None, (
        "stale row should have been dropped when the schema version bumped"
    )
    # Bumped-up user_version so subsequent opens don't re-drop.
    import sqlite3
    db2 = sqlite3.connect(p)
    assert db2.execute("PRAGMA user_version").fetchone()[0] == SummaryCache.SCHEMA_VERSION
