"""Tests for the Wikidata-based meta-article classifier and its cache."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from randompedia.meta_classifier import (
    META_ARTICLE_CLASSES,
    MetaClassifierCache,
    classify_articles,
)


def test_meta_class_table_includes_list_and_disambig():
    """The two most important classes must be present or the filter is useless."""
    assert "Q13406463" in META_ARTICLE_CLASSES  # Wikimedia list article
    assert "Q4167410" in META_ARTICLE_CLASSES   # disambiguation


def test_cache_roundtrip(tmp_path: Path):
    cache = MetaClassifierCache(tmp_path / "c.sqlite")
    cache.put_many({
        "Q11571": (False, None),
        "Q134185145": (True, "Wikimedia list article"),
    })

    assert cache.get("Q11571") is False
    assert cache.get("Q134185145") is True
    assert cache.get("Q999999999") is None

    got = cache.get_many(["Q11571", "Q134185145", "Q999999999"])
    assert got == {"Q11571": False, "Q134185145": True}


def test_get_many_handles_large_batches(tmp_path: Path):
    """SQLite's default parameter limit is 999; ensure we chunk correctly."""
    cache = MetaClassifierCache(tmp_path / "big.sqlite")
    entries = {f"Q{i}": (i % 3 == 0, None) for i in range(2500)}
    cache.put_many(entries)

    got = cache.get_many([f"Q{i}" for i in range(2500)])
    assert len(got) == 2500
    assert got["Q0"] is True
    assert got["Q1"] is False
    assert got["Q3"] is True


def test_classify_articles_returns_empty_for_no_input(tmp_path: Path):
    cache = MetaClassifierCache(tmp_path / "c.sqlite")
    assert classify_articles([], cache=cache) == {}
    assert classify_articles([None, "", None], cache=cache) == {}  # type: ignore[list-item]


def test_classify_articles_uses_cache_without_hitting_network(tmp_path: Path):
    """A fully-warm cache means no HTTP call at all."""
    cache = MetaClassifierCache(tmp_path / "warm.sqlite")
    cache.put_many({
        "Q11571":    (False, None),
        "Q134185145":(True, "Wikimedia list article"),
    })

    # If _sparql_query got called we'd fail loudly.
    with patch("randompedia.meta_classifier._sparql_query",
               side_effect=AssertionError("network was called on a warm cache")):
        got = classify_articles(["Q11571", "Q134185145"], cache=cache)

    assert got == {"Q11571": False, "Q134185145": True}


def test_classify_articles_queries_only_unknowns(tmp_path: Path):
    """Half cached, half not: only the unknowns should hit the SPARQL query."""
    cache = MetaClassifierCache(tmp_path / "half.sqlite")
    cache.put_many({"Q11571": (False, None)})  # Cristiano Ronaldo, not meta

    fake_bindings = [
        # Simulate Wikidata's response: Q999 IS a list, Q111 is not returned
        # (i.e. it's not an instance of any META_ARTICLE_CLASSES entry).
        {"item":  {"value": "http://www.wikidata.org/entity/Q999"},
         "class": {"value": "http://www.wikidata.org/entity/Q13406463"}},
    ]
    with patch("randompedia.meta_classifier._sparql_query",
               return_value=fake_bindings) as sparql:
        got = classify_articles(["Q11571", "Q111", "Q999"], cache=cache)

    sparql.assert_called_once()  # one batched query for the two unknowns
    assert got == {"Q11571": False, "Q111": False, "Q999": True}

    # And the new results were persisted.
    cache2 = MetaClassifierCache(tmp_path / "half.sqlite")
    assert cache2.get("Q999") is True
    assert cache2.get("Q111") is False


def test_classifier_fails_open_on_network_error(tmp_path: Path):
    """If Wikidata is down, we should NOT drop every article silently.
    Fail open: treat the whole chunk as non-meta so the book still builds."""
    cache = MetaClassifierCache(tmp_path / "fail.sqlite")
    import httpx
    with patch("randompedia.meta_classifier._sparql_query",
               side_effect=httpx.ConnectError("nope")):
        got = classify_articles(["Q1", "Q2", "Q3"], cache=cache)

    assert got == {"Q1": False, "Q2": False, "Q3": False}
    # And it caches the "false" so we don't hammer the failing endpoint on
    # every subsequent run.
    cache2 = MetaClassifierCache(tmp_path / "fail.sqlite")
    assert cache2.get("Q1") is False


def test_batching_respects_chunk_size(tmp_path: Path):
    """1200 unknown QIDs at chunk_size=400 must produce 3 queries."""
    cache = MetaClassifierCache(tmp_path / "b.sqlite")
    call_count = 0

    def fake(client, query):
        nonlocal call_count
        call_count += 1
        return []  # nothing is meta

    with patch("randompedia.meta_classifier._sparql_query", side_effect=fake):
        classify_articles(
            [f"Q{i}" for i in range(1200)],
            cache=cache,
            chunk_size=400,
        )

    assert call_count == 3
