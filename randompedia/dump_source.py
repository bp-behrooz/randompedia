"""Alternative content source: the Wikimedia Enterprise HTML dumps.

These are monthly NDJSON dumps where each line is one article, containing
pre-rendered HTML. We stream the file, keep only the top-N titles, and extract
a short summary (roughly the lead section, trimmed to ~2 paragraphs) plus the
lead image URL.

Dump index: https://dumps.wikimedia.org/other/enterprise_html/runs/
File name pattern (as of 2024/2025):
  {project}-NS0-{YYYYMMDD}-ENTERPRISE-HTML.json.tar.gz

We pick the latest available run and stream it without unpacking to disk.
"""

from __future__ import annotations

import json
import logging
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

import httpx
from bs4 import BeautifulSoup

from . import USER_AGENT
from .summaries import ArticleSummary, _is_freely_licensed_image_url, clean_title

log = logging.getLogger(__name__)


@dataclass(slots=True)
class DumpLocation:
    run_date: str  # e.g. "20260101"
    url: str


def _list_runs(client: httpx.Client) -> list[str]:
    """List the available run-date directories, newest first."""
    r = client.get("https://dumps.wikimedia.org/other/enterprise_html/runs/")
    r.raise_for_status()
    dates = sorted(set(re.findall(r"(\d{8})/", r.text)), reverse=True)
    return dates


def find_latest_dump(lang: str) -> DumpLocation:
    project = f"{lang}wiki"
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60.0) as client:
        for run in _list_runs(client):
            url = (
                f"https://dumps.wikimedia.org/other/enterprise_html/runs/"
                f"{run}/{project}-NS0-{run}-ENTERPRISE-HTML.json.tar.gz"
            )
            head = client.head(url)
            if head.status_code == 200:
                return DumpLocation(run_date=run, url=url)
    raise RuntimeError(f"no enterprise HTML dump found for {project}")


def _iter_dump_articles(tar_path: Path) -> Iterator[dict]:
    """Yield parsed JSON objects from every file inside the tarball."""
    with tarfile.open(tar_path, mode="r|gz") as tf:
        for member in tf:
            if not member.isfile():
                continue
            f = tf.extractfile(member)
            if f is None:
                continue
            for line in f:
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def _extract_summary_html(article_html: str, *, max_paragraphs: int = 2) -> tuple[str, str]:
    """Return (html, plain_text) for the first `max_paragraphs` non-empty <p>s
    of the lead section, stripped of citations/infoboxes."""
    soup = BeautifulSoup(article_html, "lxml")

    # Remove obvious noise.
    for sel in [
        "table.infobox", "table.sidebar", "table.navbox",
        "sup.reference", "span.mw-editsection", ".hatnote", ".shortdescription",
        "figure", "style", "script",
    ]:
        for node in soup.select(sel):
            node.decompose()

    kept: list[str] = []
    plain: list[str] = []
    body = soup.body or soup
    for p in body.find_all("p", recursive=True):
        text = p.get_text(" ", strip=True)
        if not text or len(text) < 40:
            continue
        # Strip attributes to keep EPUB clean.
        for tag in p.find_all(True):
            tag.attrs = {}
        kept.append(str(p))
        plain.append(text)
        if len(kept) >= max_paragraphs:
            break

    return "\n".join(kept), "\n\n".join(plain)


def _extract_lead_image(article_html: str) -> str | None:
    """Return the URL of the first plausible lead image, without any
    licensing filter. The caller decides whether to keep it."""
    soup = BeautifulSoup(article_html, "lxml")
    for img in soup.find_all("img"):
        src = img.get("src") or ""
        # Skip icons / math svgs.
        if any(s in src for s in ("/static/", "math/render", "wikimedia-button")):
            continue
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = "https://en.wikipedia.org" + src
        return src
    return None


def download_dump(dump: DumpLocation, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / dump.url.rsplit("/", 1)[-1]
    if out.exists() and out.stat().st_size > 0:
        log.info("using cached dump at %s", out)
        return out

    log.info("downloading %s", dump.url)
    with httpx.stream(
        "GET", dump.url, headers={"User-Agent": USER_AGENT}, timeout=None
    ) as r:
        r.raise_for_status()
        tmp = out.with_suffix(out.suffix + ".part")
        with tmp.open("wb") as f:
            for chunk in r.iter_bytes(chunk_size=1 << 20):
                f.write(chunk)
        tmp.rename(out)
    return out


def summaries_from_dump(
    *,
    lang: str,
    wanted_keys: Iterable[str],
    cache_dir: Path,
    include_fair_use_images: bool = False,
) -> dict[str, ArticleSummary]:
    """Stream the latest enterprise HTML dump and return summaries for the
    subset of titles we care about."""
    wanted = set(wanted_keys)
    dump = find_latest_dump(lang)
    tar_path = download_dump(dump, cache_dir / "dumps")

    out: dict[str, ArticleSummary] = {}
    seen = 0
    for obj in _iter_dump_articles(tar_path):
        seen += 1
        if seen % 100_000 == 0:
            log.info("scanned %d articles, matched %d/%d", seen, len(out), len(wanted))

        name = obj.get("name") or ""
        key = name.replace(" ", "_")
        if key not in wanted:
            continue

        html = (obj.get("article_body") or {}).get("html") or ""
        if not html:
            continue

        html_snippet, plain = _extract_summary_html(html)
        if not plain:
            continue

        url = (obj.get("url")
               or f"https://{lang}.wikipedia.org/wiki/{key}")

        raw_img = _extract_lead_image(html)
        keep_img = bool(raw_img) and (
            include_fair_use_images or _is_freely_licensed_image_url(raw_img)
        )

        out[key] = ArticleSummary(
            title=clean_title(name),
            key=key,
            lang=lang,
            description=(obj.get("description") or None),
            extract_html=html_snippet,
            extract_text=plain,
            image_url=raw_img if keep_img else None,
            raw_image_url=raw_img,
            url=url,
        )
        if len(out) >= len(wanted):
            break

    log.info("dump scan complete: matched %d/%d", len(out), len(wanted))
    return out
