# randompedia

Generate EPUBs of the top Wikipedia articles, shuffled into a random order, and
optimized for low-powered e-ink readers such as
[xteink / crosspoint-reader](https://github.com/crosspoint-reader/crosspoint-reader)
running on an ESP32.

Each entry is a short summary (title + one or two paragraphs, same content
served by Wikipedia's mobile page-preview card). The `--with-images` variants
include the article's lead image, converted to a 4-bit grayscale, dithered PNG
so it looks good and stays small on e-ink hardware.

Four artifacts are produced per release:

- `randompedia-1k-text.epub`
- `randompedia-1k-images.epub`
- `randompedia-10k-text.epub`
- `randompedia-10k-images.epub`

## How it works

1. **Rank.** Query the Wikimedia Pageviews API for the top articles of each
   of the last 12 completed months and aggregate the scores. This gives a
   stable "top of the year" list rather than a spike-driven one.
2. **Fetch summaries.** For small runs (≤ ~10k), we call the REST
   `page/summary` endpoint with a rate limit and a persistent on-disk cache,
   so re-runs are almost free. For very large runs, pass
   `--source enterprise-dump` to stream the monthly Enterprise HTML dump
   instead — one big download, zero live API traffic.
3. **Shuffle.** Articles are shuffled with a fixed seed so runs are
   reproducible. Change `--seed` to get a different order.
4. **Optimize images.** Lead images are downscaled to ~400 px wide, converted
   to grayscale, and Floyd–Steinberg dithered to a 16-shade palette.
5. **Build EPUB.** One chapter per article, minimal CSS, no JavaScript.

## Building locally

You need **Python 3.11 or newer**. Pick whichever route you prefer.

### Option A — Docker (no Python needed on the host)

The repo ships a `Dockerfile` that builds a self-contained image with all
dependencies pinned. Nothing gets installed on your machine outside of
Docker's own storage.

```bash
# Build the image once (~250 MB, takes a minute or two).
docker build -t randompedia .

# 1k articles, text only. The generated .epub lands in ./out/ on the host.
mkdir -p out cache
docker run --rm \
  -v "$PWD/out:/out" \
  -v "$PWD/cache:/cache" \
  randompedia \
    --count 1000 --lang en \
    --cache-dir /cache \
    --output /out/randompedia-1k-text.epub

# Same, with dithered lead images.
docker run --rm -v "$PWD/out:/out" -v "$PWD/cache:/cache" randompedia \
  --count 1000 --lang en --with-images \
  --cache-dir /cache \
  --output /out/randompedia-1k-images.epub

# Personal build that also includes fair-use images (posters, album art…).
# Do not redistribute the resulting file.
docker run --rm -v "$PWD/out:/out" -v "$PWD/cache:/cache" randompedia \
  --count 1000 --lang en --with-images --include-fair-use \
  --cache-dir /cache \
  --output /out/randompedia-1k-personal.epub
```

The `/cache` volume holds the summary SQLite database and the processed
image PNGs; keeping it around across runs makes subsequent builds nearly
free.

### Option B — Python virtualenv

If you'd rather not use Docker:

```bash
python3.12 -m venv .venv          # or python3.11 / python3.13
source .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -e .

# 1k text-only, English, default seed
randompedia --count 1000 --lang en --output out/randompedia-1k-text.epub

# 1k with images
randompedia --count 1000 --lang en --with-images \
  --output out/randompedia-1k-images.epub

# 10k using the enterprise dump instead of the API
randompedia --count 10000 --source enterprise-dump \
  --output out/randompedia-10k-text.epub

# Personal build including fair-use images (not redistributable).
randompedia --count 1000 --with-images --include-fair-use \
  --output out/randompedia-1k-personal.epub

deactivate                        # when done
```

To run the test suite, install with the `dev` extra:

```bash
pip install -e '.[dev]'
pytest
```

## Reproducibility

The default seed is `randompedia-v1`. Passing the same `--count`, `--lang`,
`--seed`, and top-list month range will produce a byte-identical article
ordering.

## CI

`.github/workflows/build.yml` runs on the 3rd of every month (giving
Wikimedia time to publish the previous month's pageviews) and produces
the four EPUBs listed above, attaching them to a GitHub Release tagged
`YYYY-MM`.

The scheduled build only runs on the canonical repository
(`everplays/randompedia`). Forks that just sit there never spam
Wikimedia — the fork owner has to explicitly opt in by dispatching the
workflow themselves. The `test` job runs unconditionally on every push
and PR so forks still get CI feedback on code changes.

### Building your own personal variant

Want an English 5000-article edition, or images including fair-use
content, or a different shuffle? Fork the repository and dispatch the
workflow on your fork:

```bash
gh workflow run build-epubs --repo your-user/your-fork \
  -f seed=my-seed -f lang=en -f include_fair_use=true
```

or via the Actions tab: **build-epubs → Run workflow**.

Dispatch inputs:

| Input | Default | Notes |
|---|---|---|
| `seed` | `randompedia-v1` | Shuffle seed. Same seed + count + lang produces the same ordering. |
| `lang` | `en` | Wikipedia language edition. |
| `include_fair_use` | `false` | When true, the with-images builds include fair-use images (film posters, album covers, etc.). See the note below. |

**Fair-use output never lands on a GitHub Release**, on any repository.
When you dispatch with `include_fair_use=true`, the resulting file is
named `randompedia-<size>-images-fairuse.epub` and is available only as
a workflow-run artifact (30-day retention, requires GitHub auth to
download). This matches the licensing story — fair-use content is for
personal use, not redistribution — and applies uniformly to canonical
and forked repos alike.

## CrossPoint / FreeInkBook CSS compatibility

The EPUB uses a deliberately tiny CSS subset because the CrossPoint reader
(FreeInkBook engine) only supports:

- `font-size`, `font-weight`, `font-style`
- `text-align`, `text-indent`
- `margin-*` (no `padding`, no `border`)
- `display: none` (any other `display` value is ignored)
- Class selectors (`.foo`), element selectors (`p`), and `element.class` — no
  `#id`, no pseudo-classes, no descendant combinators
- Inline `style=""` attributes (same subset)

Everything else — `position`, `flex`, viewport units (`vh`/`vw`), `border`,
`padding`, `background`, `color`, `page-break-*`, `@media` — is silently
dropped by the reader. `<hr>` produces no visible line. If you contribute
CSS, stay inside the supported subset or add it as progressive
enhancement that other, more capable EPUB readers can use.

Reference: `freeink-sdk/libs/book/FreeInkBook/src/css/Css.cpp` in the
[crosspoint-reader](https://github.com/crosspoint-reader/crosspoint-reader)
firmware source.

## License

- **Source code** in this repository: MIT — see [`LICENSE`](LICENSE).
- **Generated EPUB files** are derivative works of Wikipedia and are
  therefore released under [CC BY-SA 4.0][cc-by-sa]. Each book includes a
  colophon with full attribution. Only images from Wikimedia Commons are
  included; non-free (fair-use) images that appear on individual
  Wikipedia projects are deliberately excluded so the output can be
  freely redistributed.

[cc-by-sa]: https://creativecommons.org/licenses/by-sa/4.0/
