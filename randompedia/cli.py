"""randompedia command-line interface."""

from __future__ import annotations

import argparse
import logging
import random
import sys
from pathlib import Path

from .epub_build import BookMeta, build_epub, language_display_name, stable_book_id
from .images import ImageSpec
from .meta_classifier import MetaClassifierCache, classify_articles
from .ranker import RankedTitle, rank_top_articles
from .summaries import ArticleSummary, SummaryCache, SummaryFetcher

log = logging.getLogger("randompedia")

DEFAULT_SEED = "randompedia-v1"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="randompedia")
    p.add_argument("--count", type=int, required=True,
                   help="Number of top articles to include (e.g. 1000, 10000).")
    p.add_argument("--lang", default="en",
                   help="Wikipedia language edition (default: en).")
    p.add_argument("--seed", default=DEFAULT_SEED,
                   help="Shuffle seed (default: %(default)s).")
    p.add_argument("--months", type=int, default=12,
                   help="Aggregate pageviews over the last N months (default: 12).")
    p.add_argument("--with-images", action="store_true",
                   help="Include lead images (grayscale, dithered).")
    p.add_argument("--include-fair-use", action="store_true",
                   help="Include images that are not on Wikimedia Commons "
                        "(typically fair-use content such as film posters or "
                        "album covers). Off by default so the resulting EPUB "
                        "can be redistributed under CC BY-SA 4.0. Use this "
                        "for personal builds only.")
    p.add_argument("--image-width", type=int, default=400,
                   help="Max image width in pixels (default: 400). "
                        "Tuned for Xteink X3/X4 in portrait orientation.")
    p.add_argument("--image-height", type=int, default=320,
                   help="Max image height in pixels (default: 320). "
                        "Sized so title+subtitle+image fit on a single page "
                        "before the summary body on 480x800/528x792 panels.")
    p.add_argument("--image-shades", type=int, default=16)
    p.add_argument("--source", choices=("api", "enterprise-dump"), default="api",
                   help="Content source. 'api' = REST summaries (default). "
                        "'enterprise-dump' = monthly HTML dump, recommended for very large counts.")
    p.add_argument("--rate", type=float, default=5.0,
                   help="API requests per second when --source=api (default: 5).")
    p.add_argument("--cache-dir", type=Path, default=Path(".cache"),
                   help="Directory for caches (summaries, images, dumps).")
    p.add_argument("--output", type=Path, required=True,
                   help="Output .epub path.")
    p.add_argument("--title",
                   help="Override book title. Default: 'randompedia — top N shuffled (LANG)', "
                        "where LANG is the language's English name (e.g. 'English').")
    p.add_argument("--keep-meta-articles", action="store_true",
                   help="Do NOT filter out list / timeline / disambiguation / "
                        "category articles. By default we drop them via a "
                        "Wikidata classification because their REST summary "
                        "is usually just meta-boilerplate.")
    p.add_argument("-v", "--verbose", action="count", default=0)
    return p.parse_args(argv)


def _configure_logging(verbosity: int) -> None:
    level = logging.WARNING if verbosity == 0 else logging.INFO if verbosity == 1 else logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _collect_summaries_api(
    ranked: list[RankedTitle], *, lang: str, cache_dir: Path, rate: float,
    include_fair_use: bool,
) -> list[ArticleSummary]:
    cache = SummaryCache(cache_dir / "summaries.sqlite")
    out: list[ArticleSummary] = []
    with SummaryFetcher(
        lang=lang, cache=cache, rate_per_second=rate,
        include_fair_use_images=include_fair_use,
    ) as f:
        for i, rt in enumerate(ranked, start=1):
            s = f.fetch(rt.title)
            if s is not None:
                out.append(s)
            if i % 200 == 0:
                log.info("fetched %d/%d summaries (%d kept)", i, len(ranked), len(out))
    return out


def _collect_summaries_dump(
    ranked: list[RankedTitle], *, lang: str, cache_dir: Path,
    include_fair_use: bool,
) -> list[ArticleSummary]:
    from .dump_source import summaries_from_dump
    keys = [rt.title for rt in ranked]
    got = summaries_from_dump(
        lang=lang, wanted_keys=keys, cache_dir=cache_dir,
        include_fair_use_images=include_fair_use,
    )
    # Preserve ranked ordering, drop misses.
    return [got[k] for k in keys if k in got]


def _drop_meta_articles(
    summaries: list[ArticleSummary], *, cache_dir: Path,
) -> list[ArticleSummary]:
    """Remove list/timeline/index/disambiguation articles via Wikidata."""
    qids = [s.wikibase_item for s in summaries if s.wikibase_item]
    if not qids:
        log.info("meta filter: no wikibase_items available; skipping")
        return summaries

    cache = MetaClassifierCache(cache_dir / "meta_articles.sqlite")
    classification = classify_articles(qids, cache=cache)

    kept: list[ArticleSummary] = []
    dropped = 0
    for s in summaries:
        if s.wikibase_item and classification.get(s.wikibase_item):
            dropped += 1
            log.debug("dropping meta article %s (%s)", s.key, s.wikibase_item)
            continue
        kept.append(s)
    log.info("meta filter: dropped %d/%d articles as list/timeline/index/etc.",
             dropped, len(summaries))
    return kept


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.verbose)

    log.info("ranking top %d articles for %s.wikipedia", args.count, args.lang)
    # Over-fetch to survive 404s + drops (~15%) plus, if we're filtering
    # meta-articles, another ~10% for list/timeline pages that rank in the
    # top-viewed set.
    overfetch_ratio = 1.30 if not args.keep_meta_articles else 1.15
    overfetch = int(args.count * overfetch_ratio) + 50
    ranked = rank_top_articles(lang=args.lang, count=overfetch, months=args.months)
    log.info("got %d ranked titles", len(ranked))

    if args.source == "api":
        summaries = _collect_summaries_api(
            ranked, lang=args.lang, cache_dir=args.cache_dir, rate=args.rate,
            include_fair_use=args.include_fair_use,
        )
    else:
        summaries = _collect_summaries_dump(
            ranked, lang=args.lang, cache_dir=args.cache_dir,
            include_fair_use=args.include_fair_use,
        )

    if not args.keep_meta_articles:
        summaries = _drop_meta_articles(summaries, cache_dir=args.cache_dir)

    summaries = summaries[: args.count]
    if len(summaries) < args.count:
        log.warning("only got %d/%d summaries; continuing", len(summaries), args.count)

    # Reproducible shuffle.
    rng = random.Random(f"{args.seed}|{args.lang}|{args.count}")
    rng.shuffle(summaries)

    lang_name = language_display_name(args.lang)
    title = args.title or f"randompedia — top {args.count} shuffled ({lang_name})"
    meta = BookMeta(
        title=title,
        identifier=stable_book_id("v1", args.lang, str(args.count), args.seed),
        lang=args.lang,
        seed=args.seed,
    )

    build_epub(
        articles=summaries,
        output_path=args.output,
        meta=meta,
        with_images=args.with_images,
        image_cache_dir=args.cache_dir / "images",
        image_spec=ImageSpec(
            max_width=args.image_width,
            max_height=args.image_height,
            shades=args.image_shades,
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
