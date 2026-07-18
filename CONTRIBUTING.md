# Contributing

## Running the tests

```bash
pip install -e '.[dev]'
pytest
```

## Building your own personal variant

Want an English 5000-article edition, images including fair-use content, a
different shuffle, or a different Wikipedia language edition? Fork the
repository and dispatch the workflow on your fork:

```bash
gh workflow run build.yml --repo your-user/your-fork \
  -f seed=my-seed -f lang=en -f include_fair_use=true
```

or via the Actions tab: **build-epubs → Run workflow**.

Dispatch inputs:

| Input | Default | Notes |
|---|---|---|
| `seed` | `randompedia-v1` | Shuffle seed. Same seed + count + lang produces the same ordering. |
| `lang` | `en` | Wikipedia language edition. |
| `include_fair_use` | `false` | When true, the with-images builds include fair-use images (film posters, album covers, etc.). See below. |

### Fair-use output

**Fair-use output never lands on a GitHub Release**, on any repository.
When you dispatch with `include_fair_use=true`, the resulting file is
named `randompedia-<size>-images-fairuse.epub` and is available only as
a workflow-run artifact (30-day retention, requires GitHub auth to
download). This matches the licensing story — fair-use content is for
personal use, not redistribution — and applies uniformly to canonical
and forked repos alike.
