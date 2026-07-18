# Minimal image for building randompedia EPUBs.
#
# Usage:
#   docker build -t randompedia .
#   docker run --rm -v "$PWD/out:/out" -v "$PWD/.cache:/cache" randompedia \
#     --count 1000 --with-images --cache-dir /cache --output /out/book.epub
#
# The container's default entrypoint is the `randompedia` CLI, so any flags
# after the image name are passed straight through. Mount /out to collect
# the resulting .epub on your host, and /cache to persist the summary +
# image caches between runs.

FROM python:3.12-slim

# Only build-time deps we actually need. lxml wheels are prebuilt on PyPI for
# python:3.12-slim, so no compiler is needed.
WORKDIR /app

# Install first with the pyproject metadata alone so image layers cache well
# on code-only changes.
COPY pyproject.toml README.md LICENSE ./
COPY randompedia/ ./randompedia/
RUN pip install --no-cache-dir .

# Runtime working directory. Users mount /out and /cache from the host.
WORKDIR /work
VOLUME ["/out", "/cache"]

ENTRYPOINT ["randompedia"]
CMD ["--help"]
