# randompedia

Generate EPUBs of the top Wikipedia articles, shuffled into a random order, and
optimized for low-powered e-ink readers such as
[xteink / crosspoint-reader](https://github.com/crosspoint-reader/crosspoint-reader)
running on an ESP32.

## Download

Latest builds are attached to the most recent
[GitHub Release](https://github.com/everplays/randompedia/releases/latest)
(see [all releases](https://github.com/everplays/randompedia/releases) for
previous months):

- [randompedia-1k-text.epub](https://github.com/everplays/randompedia/releases/latest/download/randompedia-1k-text.epub)
- [randompedia-1k-images.epub](https://github.com/everplays/randompedia/releases/latest/download/randompedia-1k-images.epub)
- [randompedia-5k-text.epub](https://github.com/everplays/randompedia/releases/latest/download/randompedia-5k-text.epub)
- [randompedia-5k-images.epub](https://github.com/everplays/randompedia/releases/latest/download/randompedia-5k-images.epub)
- [randompedia-10k-text.epub](https://github.com/everplays/randompedia/releases/latest/download/randompedia-10k-text.epub)
- [randompedia-10k-images.epub](https://github.com/everplays/randompedia/releases/latest/download/randompedia-10k-images.epub)

## How it works

1. **Rank.** Query the Wikimedia Pageviews API for the top articles of each
   of the last 12 completed months and aggregate the scores. This gives a
   stable "top of the year" list rather than a spike-driven one.
2. **Fetch summaries.** For small runs (≤ ~10k), we call the REST
   `page/summary` endpoint with a rate limit and a persistent on-disk cache,
   so re-runs are almost free. For very large runs, pass
   `--source enterprise-dump` to stream the monthly Enterprise HTML dump
   instead — one big download, zero live API traffic.
3. **Shuffle.** Articles are shuffled with a fixed seed (default
   `randompedia-v1`) so runs are reproducible. Change `--seed` to get a
   different order.
4. **Optimize images.** Lead images are downscaled to at most 400x320,
   converted to grayscale, and encoded as baseline grayscale JPEG. The
   CrossPoint firmware does its own dither to the panel's 16 shades at
   draw time, so we deliberately ship a smooth grayscale image rather
   than pre-dithering.
5. **Build EPUB.** One chapter per article, minimal CSS, no JavaScript.

## Building locally

### Option A — Docker

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

# Same, with lead images.
docker run --rm -v "$PWD/out:/out" -v "$PWD/cache:/cache" randompedia \
  --count 1000 --lang en --with-images \
  --cache-dir /cache \
  --output /out/randompedia-1k-images.epub
```

The `/cache` volume holds the summary SQLite databases and the processed
image JPEGs; keeping it around across runs makes subsequent builds nearly
free.

### Option B — Python virtualenv

Requires **Python 3.11 or newer**.

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
```

## CI

`.github/workflows/build.yml` runs on the 3rd of every month and attaches
the six EPUBs to a GitHub Release tagged `YYYY-MM`. The scheduled build
only runs on the canonical repository (`everplays/randompedia`); forks
never hit Wikimedia unless their owner explicitly dispatches the workflow.

Contributor and fork-owner docs (running tests, dispatching personal
builds, the fair-use opt-in) live in [`CONTRIBUTING.md`](CONTRIBUTING.md).

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

Everything outside that subset is silently dropped. Reference:
`freeink-sdk/libs/book/FreeInkBook/src/css/Css.cpp` in the
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
