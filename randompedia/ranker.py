"""Rank Wikipedia articles by pageviews.

We aggregate the "top per day" list day-by-day until we have enough unique
titles to satisfy the requested count (with slack for later filtering).
The Pageviews API returns the top ~1000 titles per day; deduping across a
year of days gets tens of thousands of unique articles, so we can support
counts well beyond what the "top per month" endpoint (also ~1000/entry)
would ever yield.

Each day's response is cached in SQLite so re-runs skip already-fetched
days entirely.

API docs: https://wikimedia.org/api/rest_v1/#/Pageviews_data
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from . import USER_AGENT
from .summaries import _parse_retry_after, _RateLimiter

log = logging.getLogger(__name__)

# Titles like these are top-viewed but aren't real articles. Drop them.
_JUNK_TITLE_RE = re.compile(
    r"""^(
        Main_Page
        | Special:.*
        | Wikipedia:.*
        | -                     # literal "-" placeholder that appears in the list
        | .*_\(disambiguation\)
    )$""",
    re.VERBOSE | re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RankedTitle:
    title: str  # underscored form as returned by the API
    score: int  # summed views across the aggregation window


class PageviewsCache:
    """Per-day pageviews cache.

    One row per (lang, date). Payload is the raw list of {article, views}
    entries from the API — we don't decode/re-encode the aggregation on
    read, so the same cache serves any count.
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS pageviews_daily ("
            "  lang TEXT NOT NULL,"
            "  date TEXT NOT NULL,"    # ISO YYYY-MM-DD
            "  fetched_at INTEGER NOT NULL,"
            "  payload TEXT NOT NULL,"  # JSON list of {article, views}
            "  PRIMARY KEY (lang, date)"
            ")"
        )
        self.db.commit()

    def get(self, lang: str, day: dt.date) -> list[dict] | None:
        row = self.db.execute(
            "SELECT payload FROM pageviews_daily WHERE lang=? AND date=?",
            (lang, day.isoformat()),
        ).fetchone()
        return None if row is None else json.loads(row[0])

    def put(self, lang: str, day: dt.date, entries: list[dict]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO pageviews_daily(lang, date, fetched_at, payload) "
            "VALUES (?, ?, ?, ?)",
            (lang, day.isoformat(), int(time.time()), json.dumps(entries)),
        )
        self.db.commit()


def _days_back(*, today: dt.date | None = None, limit: int = 365):
    """Yield dates most-recent-first, skipping today (not yet published).

    Also skips yesterday when it's still early in UTC, since same-day
    pageviews often aren't published until several hours in.
    """
    today = today or dt.date.today()
    # Yesterday's data is usually posted by ~03:00 UTC. To avoid flaky
    # first-day 404s we always skip yesterday too.
    start = today - dt.timedelta(days=2)
    for i in range(limit):
        yield start - dt.timedelta(days=i)


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(min=2, max=60),
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    reraise=True,
)
def _fetch_day(client: httpx.Client, project: str, day: dt.date,
               limiter: "_RateLimiter | None" = None) -> list[dict] | None:
    """Fetch one day's top-articles list. Returns None if the day has no
    data (published lag, holiday in the data pipeline, etc.)."""
    if limiter is not None:
        limiter.wait()
    url = (
        "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/"
        f"{project}/all-access/{day.year}/{day.month:02d}/{day.day:02d}"
    )
    r = client.get(url, timeout=30.0)
    if r.status_code == 404:
        return None  # not yet published or genuinely missing
    if r.status_code == 429:
        wait_s = _parse_retry_after(r.headers.get("Retry-After") or "30")
        wait_s = min(max(wait_s, 5.0), 120.0)
        log.warning("429 from pageviews, waiting %.1fs", wait_s)
        time.sleep(wait_s)
    r.raise_for_status()
    return r.json()["items"][0]["articles"]


def rank_top_articles(
    *,
    lang: str = "en",
    count: int,
    max_days: int = 365,
    stability_margin: float = 0.2,
    cache: PageviewsCache | None = None,
    today: dt.date | None = None,
) -> list[RankedTitle]:
    """Return the top `count` articles ranked by aggregated pageviews.

    Iterates the daily-top endpoint most-recent-first, accumulating
    scores. Stops walking days as soon as we have
    ``count * (1 + stability_margin)`` distinct titles — the extra
    margin protects against the tail of the ranking shifting when the
    last few days add high-view articles that would push out titles
    currently in position ``count - 5``. Also stops at ``max_days`` if
    the corpus never produces enough uniques.

    The caller is responsible for any additional over-fetch on top of
    `count` (e.g. to survive 404s and post-fetch filtering).
    """
    project = f"{lang}.wikipedia"
    scores: dict[str, int] = {}
    target = int(count * (1.0 + stability_margin))

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    limiter = _RateLimiter(per_second=3.0)  # match the summaries fetcher
    days_fetched = 0
    days_from_cache = 0
    with httpx.Client(headers=headers, http2=True) as client:
        for day in _days_back(today=today, limit=max_days):
            entries: list[dict] | None = None
            if cache is not None:
                entries = cache.get(lang, day)
                if entries is not None:
                    days_from_cache += 1

            if entries is None:
                try:
                    entries = _fetch_day(client, project, day, limiter=limiter)
                except Exception as e:  # noqa: BLE001
                    log.warning("pageviews fetch failed for %s: %s", day, e)
                    continue
                if entries is None:
                    log.debug("no pageviews for %s (not yet published?)", day)
                    continue
                days_fetched += 1
                if cache is not None:
                    cache.put(lang, day, entries)

            for entry in entries:
                title = entry["article"]
                if _JUNK_TITLE_RE.match(title):
                    continue
                scores[title] = scores.get(title, 0) + int(entry["views"])

            if days_fetched > 0 and days_fetched % 30 == 0:
                log.info(
                    "ranker: %d days fetched, %d from cache, %d unique titles so far",
                    days_fetched, days_from_cache, len(scores),
                )

            if len(scores) >= target:
                log.info(
                    "ranker: reached %d unique titles (target %d) after %d days",
                    len(scores), target, days_fetched + days_from_cache,
                )
                break

    log.info(
        "ranker done: %d unique titles from %d fetched + %d cached days",
        len(scores), days_fetched, days_from_cache,
    )
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [RankedTitle(title=t, score=s) for t, s in ranked[:count]]
