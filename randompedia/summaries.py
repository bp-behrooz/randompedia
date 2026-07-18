"""Fetch article summaries from Wikipedia's REST API.

Endpoint: /api/rest_v1/page/summary/{title}
Returns title, short description, HTML extract (1-2 paragraphs), and a
`thumbnail`/`originalimage` URL when available.

We keep a persistent on-disk JSON cache keyed by (lang, title). Re-runs skip
articles that are already cached, so a monthly run only fetches the delta.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import sqlite3
import time
import urllib.parse
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from . import USER_AGENT

log = logging.getLogger(__name__)


_WS_RE = re.compile(r"\s+")


class _TextExtractor(HTMLParser):
    """Collect text content, dropping tags and decoding entities.

    Uses the stdlib html.parser (which handles entities, self-closing tags,
    and malformed markup gracefully) rather than a regex.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def clean_title(raw: str) -> str:
    """Return the plain-text form of a Wikipedia title.

    Wikipedia's REST API ``displaytitle`` field is pre-rendered HTML
    (e.g. ``<span lang="en" dir="ltr"><i>Foo</i></span>``); ``title`` is
    supposedly plain, but occasionally contains ``&amp;`` etc. We normalize
    both by parsing as HTML and taking the text content.
    """
    if not raw:
        return ""
    p = _TextExtractor()
    p.feed(raw)
    p.close()
    return _WS_RE.sub(" ", p.text()).strip()


# Wikimedia serves media from a few hosts. Files under `/wikipedia/commons/` are
# on Wikimedia Commons, which only accepts freely-licensed or public-domain
# works. Files under `/wikipedia/<lang>/` (e.g. `/wikipedia/en/`) are on that
# language's local Wikipedia and typically include fair-use content — safe for
# Wikipedia's own use but not for downstream redistribution.
_FREE_IMAGE_PATH_RE = re.compile(
    r"^https?://upload\.wikimedia\.org/wikipedia/commons/",
    re.IGNORECASE,
)


def _is_freely_licensed_image_url(url: str) -> bool:
    """Return True if the URL points at Wikimedia Commons.

    We deliberately exclude images hosted on individual Wikipedia projects
    (e.g. ``/wikipedia/en/``) because those often carry fair-use rationales
    that are not transferable to a redistributable EPUB.
    """
    return bool(_FREE_IMAGE_PATH_RE.match(url))


@dataclass(slots=True)
class ArticleSummary:
    title: str            # display title
    key: str              # canonical URL key (underscored)
    lang: str
    description: str | None
    extract_html: str     # sanitized HTML, 1-2 paragraphs
    extract_text: str     # plain text fallback
    image_url: str | None       # post-filter image URL (None if fair-use-filtered out)
    url: str              # canonical article URL
    # Unfiltered image URL as returned by the API/dump. We cache this so a later
    # run with a different fair-use policy can decide again without re-fetching.
    # Optional for construction convenience in tests; production code paths in
    # SummaryFetcher / summaries_from_dump always set it explicitly.
    raw_image_url: str | None = None
    # Wikidata Q-ID for this article (e.g. "Q11571"), used to classify the
    # article's kind (Wikimedia list article, timeline, disambiguation, …) via
    # a batched SPARQL query. None for older cache rows or for articles the
    # API doesn't return a wikibase_item for.
    wikibase_item: str | None = None


class SummaryCache:
    """Tiny SQLite-backed cache. One row per (lang, key).

    The schema version below is bumped whenever the ArticleSummary shape
    changes in a way that requires re-fetching. When the on-disk version
    is older, the table is dropped and rebuilt — the alternative would be
    silently serving stale rows missing new fields.
    """

    SCHEMA_VERSION = 2

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        current = self.db.execute("PRAGMA user_version").fetchone()[0]
        if current and current < self.SCHEMA_VERSION:
            log.info("summary cache schema v%d < v%d; discarding old rows",
                     current, self.SCHEMA_VERSION)
            self.db.execute("DROP TABLE IF EXISTS summaries")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS summaries ("
            "  lang TEXT NOT NULL,"
            "  key  TEXT NOT NULL,"
            "  fetched_at INTEGER NOT NULL,"
            "  payload TEXT NOT NULL,"
            "  PRIMARY KEY (lang, key)"
            ")"
        )
        self.db.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
        self.db.commit()

    def get(self, lang: str, key: str) -> ArticleSummary | None:
        row = self.db.execute(
            "SELECT payload FROM summaries WHERE lang=? AND key=?", (lang, key)
        ).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        # Backfill for older cache rows that predate raw_image_url.
        data.setdefault("raw_image_url", data.get("image_url"))
        return ArticleSummary(**data)

    def put(self, s: ArticleSummary) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO summaries(lang, key, fetched_at, payload) "
            "VALUES (?, ?, ?, ?)",
            (s.lang, s.key, int(time.time()), json.dumps(dataclasses.asdict(s))),
        )
        self.db.commit()


class _RateLimiter:
    """Simple token-bucket-ish limiter: min interval between requests."""

    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self._next = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        if now < self._next:
            time.sleep(self._next - now)
        self._next = max(now, self._next) + self.interval


class SummaryFetcher:
    def __init__(
        self,
        *,
        lang: str,
        cache: SummaryCache,
        rate_per_second: float = 5.0,
        include_fair_use_images: bool = False,
    ):
        self.lang = lang
        self.cache = cache
        self.include_fair_use_images = include_fair_use_images
        self.limiter = _RateLimiter(rate_per_second)
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            http2=True,
            timeout=30.0,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(min=1, max=30),
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        reraise=True,
    )
    def _get(self, url: str) -> httpx.Response:
        self.limiter.wait()
        r = self.client.get(url)
        # Retry on 5xx and 429; give up on other 4xx.
        if r.status_code >= 500 or r.status_code == 429:
            r.raise_for_status()
        return r

    def fetch(self, key: str) -> ArticleSummary | None:
        cached = self.cache.get(self.lang, key)
        if cached is not None:
            return self._apply_image_policy(cached)

        quoted = urllib.parse.quote(key, safe="")
        url = f"https://{self.lang}.wikipedia.org/api/rest_v1/page/summary/{quoted}"
        r = self._get(url)
        if r.status_code == 404:
            log.warning("404 for %s:%s", self.lang, key)
            return None
        if r.status_code >= 400:
            log.warning("HTTP %d for %s:%s", r.status_code, self.lang, key)
            return None

        data = r.json()
        # Skip disambiguation, redirects to non-articles, etc.
        if data.get("type") not in (None, "standard"):
            return None

        raw_img_url = None
        if "originalimage" in data:
            raw_img_url = data["originalimage"]["source"]
        elif "thumbnail" in data:
            raw_img_url = data["thumbnail"]["source"]

        summary = ArticleSummary(
            title=clean_title(data.get("title") or data.get("displaytitle") or key.replace("_", " ")),
            key=key,
            lang=self.lang,
            description=data.get("description"),
            extract_html=data.get("extract_html") or f"<p>{data.get('extract', '')}</p>",
            extract_text=data.get("extract") or "",
            image_url=None,           # filled in by _apply_image_policy
            raw_image_url=raw_img_url,
            wikibase_item=data.get("wikibase_item"),
            url=data.get("content_urls", {}).get("desktop", {}).get("page")
            or f"https://{self.lang}.wikipedia.org/wiki/{quoted}",
        )
        # Cache the raw payload so switching --include-fair-use later doesn't
        # require re-fetching.
        self.cache.put(summary)
        return self._apply_image_policy(summary)

    def _apply_image_policy(self, s: ArticleSummary) -> ArticleSummary:
        """Set ``image_url`` from ``raw_image_url`` subject to the fair-use policy."""
        raw = s.raw_image_url
        keep = bool(raw) and (
            self.include_fair_use_images or _is_freely_licensed_image_url(raw)
        )
        if s.image_url == (raw if keep else None):
            return s
        return dataclasses.replace(s, image_url=raw if keep else None)
