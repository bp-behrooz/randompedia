"""Classify Wikipedia articles by their Wikidata `instance of` (P31) values.

Some articles are "meta" — Wikimedia list articles, timelines, indices,
disambiguation pages, glossaries, category pages. Their REST-summary
`extract` is typically just meta-boilerplate ("The following is a list of…",
"This article contains a timeline of…") which makes for a useless short
book entry.

We identify them by their Wikidata Q-ID rather than by title prefix so
the filter works for every language. The classification is done in one
SPARQL query per ~500 items (Wikidata Query Service accepts up to
~10k characters per query; 500 Q-IDs each ~10 chars fits easily).

Result caching lives in a small SQLite table so re-runs skip already-known items.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Iterable

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from . import USER_AGENT

log = logging.getLogger(__name__)


# Wikidata Q-IDs for article types that should NOT appear in the book.
#
# These are Q-IDs the Wikidata community treats as "kinds of meta pages":
# their content is a directory of other articles, not a self-contained topic.
#
# Curated conservatively — everything here is either a documented Wikimedia
# infrastructure type or a top-level "list article" concept. If a title is
# a member of one of these classes (directly or via subclass, `wdt:P31/wdt:P279*`),
# we drop it.
META_ARTICLE_CLASSES: dict[str, str] = {
    "Q13406463": "Wikimedia list article",
    "Q17524420": "aspect of history",           # sometimes flags meta pages
    "Q17633526": "Wikinews article",            # shouldn't appear on Wikipedia but harmless
    "Q4167410":  "Wikimedia disambiguation page",
    "Q4167836":  "Wikimedia category",
    "Q11266439": "Wikimedia template",
    "Q11753321": "Wikimedia navigational template",
    "Q13417114": "Wikimedia navigation help page",
    "Q14204246": "Wikimedia project page",
    "Q15184295": "Wikimedia module",
    "Q20010800": "user template",
    "Q22808320": "Wikimedia disambiguation category",
    "Q22808324": "Wikimedia set index article",
    "Q24046192": "Wikimedia category of stubs",
    "Q24575110": "Wikimedia list of lists",
    "Q26267864": "Wikimedia KML file",
    "Q30432511": "metaclass in Wikidata",
    "Q97545100": "chronological list",
    "Q98645843": "Wikimedia timeline article",
    "Q101352":   "family name",                 # dropped: not an article about a topic
}


class MetaClassifierCache:
    """SQLite-backed cache mapping Wikidata Q-ID → is_meta_bool."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS meta_articles ("
            "  qid TEXT PRIMARY KEY,"
            "  is_meta INTEGER NOT NULL,"
            "  reason TEXT,"
            "  checked_at INTEGER NOT NULL"
            ")"
        )
        self.db.commit()

    def get(self, qid: str) -> bool | None:
        row = self.db.execute(
            "SELECT is_meta FROM meta_articles WHERE qid=?", (qid,)
        ).fetchone()
        return None if row is None else bool(row[0])

    def get_many(self, qids: Iterable[str]) -> dict[str, bool]:
        qids = list(qids)
        if not qids:
            return {}
        # SQLite parameter limit is 999 by default; chunk to be safe.
        out: dict[str, bool] = {}
        for i in range(0, len(qids), 500):
            chunk = qids[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            for qid, is_meta in self.db.execute(
                f"SELECT qid, is_meta FROM meta_articles WHERE qid IN ({placeholders})",
                chunk,
            ):
                out[qid] = bool(is_meta)
        return out

    def put_many(self, entries: dict[str, tuple[bool, str | None]]) -> None:
        if not entries:
            return
        now = int(time.time())
        self.db.executemany(
            "INSERT OR REPLACE INTO meta_articles(qid, is_meta, reason, checked_at) "
            "VALUES (?, ?, ?, ?)",
            [(qid, int(is_meta), reason, now)
             for qid, (is_meta, reason) in entries.items()],
        )
        self.db.commit()


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(min=2, max=30),
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    reraise=True,
)
def _sparql_query(client: httpx.Client, query: str) -> list[dict]:
    """Run a single SPARQL query against Wikidata Query Service."""
    r = client.get(
        "https://query.wikidata.org/sparql",
        params={"query": query, "format": "json"},
        timeout=60.0,
    )
    r.raise_for_status()
    return r.json()["results"]["bindings"]


def _build_query(qid_chunk: list[str]) -> str:
    """Return a SPARQL query that, given a chunk of Q-IDs, yields those that
    are (directly or via subclass) instances of any META_ARTICLE_CLASSES entry.

    We use `wdt:P31/wdt:P279*` so subclasses count. E.g. "Wikimedia timeline
    article" is a subclass of "Wikimedia list article" — either way, out."""
    values_articles = " ".join(f"wd:{q}" for q in qid_chunk)
    values_meta = " ".join(f"wd:{q}" for q in META_ARTICLE_CLASSES)
    return f"""\
SELECT DISTINCT ?item ?class WHERE {{
  VALUES ?item {{ {values_articles} }}
  VALUES ?class {{ {values_meta} }}
  ?item wdt:P31/wdt:P279* ?class .
}}
"""


def classify_articles(
    qids: Iterable[str],
    *,
    cache: MetaClassifierCache,
    chunk_size: int = 400,
) -> dict[str, bool]:
    """Return a mapping ``qid -> is_meta`` for every input Q-ID.

    Uses the on-disk cache first; only queries Wikidata for the unknowns.
    Batch size of 400 keeps well under the SPARQL query length limit and
    the 60-second timeout on the query service.
    """
    qids = [q for q in qids if q]  # drop Nones / empties
    if not qids:
        return {}

    known = cache.get_many(qids)
    unknown = [q for q in qids if q not in known]
    log.info("meta-article classifier: %d cached, %d to query",
             len(known), len(unknown))

    if not unknown:
        return known

    result = dict(known)
    to_cache: dict[str, tuple[bool, str | None]] = {}

    headers = {"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"}
    with httpx.Client(headers=headers, http2=True) as client:
        for i in range(0, len(unknown), chunk_size):
            chunk = unknown[i:i + chunk_size]
            log.debug("querying Wikidata for %d Q-IDs (chunk %d)",
                      len(chunk), i // chunk_size + 1)
            try:
                bindings = _sparql_query(client, _build_query(chunk))
            except Exception as e:  # noqa: BLE001
                log.warning("Wikidata SPARQL query failed: %s "
                            "(treating this chunk as non-meta)", e)
                # Fail open: don't drop articles just because Wikidata was flaky.
                for qid in chunk:
                    result[qid] = False
                    to_cache[qid] = (False, "sparql-failed")
                continue

            hit_reason: dict[str, str] = {}
            for row in bindings:
                item_url = row["item"]["value"]
                class_url = row["class"]["value"]
                item_qid = item_url.rsplit("/", 1)[-1]
                class_qid = class_url.rsplit("/", 1)[-1]
                hit_reason[item_qid] = META_ARTICLE_CLASSES.get(class_qid, class_qid)

            for qid in chunk:
                is_meta = qid in hit_reason
                result[qid] = is_meta
                to_cache[qid] = (is_meta, hit_reason.get(qid))

    cache.put_many(to_cache)
    return result
