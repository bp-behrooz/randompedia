"""Rank Wikipedia articles by pageviews.

We aggregate the "top per month" list over the last N completed months, so a
single news spike in one month cannot dominate the ranking. The Pageviews API
returns the top ~1000 articles per day/month; we use monthly granularity to
stay well under any rate limit (~12 requests per run).

API docs: https://wikimedia.org/api/rest_v1/#/Pageviews_data
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from . import USER_AGENT

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


def _months_back(n: int, *, today: dt.date | None = None) -> list[tuple[int, int]]:
    """Return (year, month) tuples for the N most recent *completed* months."""
    today = today or dt.date.today()
    # Pageviews for month M are usually published a couple of days into M+1.
    # Skip the current month entirely to be safe.
    y, m = today.year, today.month
    out: list[tuple[int, int]] = []
    for _ in range(n):
        m -= 1
        if m == 0:
            m = 12
            y -= 1
        out.append((y, m))
    return out


@retry(stop=stop_after_attempt(4), wait=wait_exponential(min=1, max=30))
def _fetch_month(client: httpx.Client, project: str, year: int, month: int) -> list[dict]:
    url = (
        "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/"
        f"{project}/all-access/{year}/{month:02d}/all-days"
    )
    r = client.get(url, timeout=30.0)
    r.raise_for_status()
    return r.json()["items"][0]["articles"]


def rank_top_articles(
    *,
    lang: str = "en",
    count: int,
    months: int = 12,
) -> list[RankedTitle]:
    """Return the top `count` articles aggregated over the last `months` months."""
    project = f"{lang}.wikipedia"
    scores: dict[str, int] = {}

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    with httpx.Client(headers=headers, http2=True) as client:
        for year, month in _months_back(months):
            log.info("fetching pageviews top for %s %d-%02d", project, year, month)
            for entry in _fetch_month(client, project, year, month):
                title = entry["article"]
                if _JUNK_TITLE_RE.match(title):
                    continue
                scores[title] = scores.get(title, 0) + int(entry["views"])

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [RankedTitle(title=t, score=s) for t, s in ranked[:count]]
