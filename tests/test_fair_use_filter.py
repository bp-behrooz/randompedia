"""Tests for the fair-use image URL filter."""
from randompedia.summaries import _is_freely_licensed_image_url


def test_accepts_commons_urls():
    urls = [
        "https://upload.wikimedia.org/wikipedia/commons/1/10/Foo.jpg",
        "http://upload.wikimedia.org/wikipedia/commons/thumb/a/b/Foo.jpg/400px-Foo.jpg",
        "HTTPS://Upload.Wikimedia.ORG/wikipedia/commons/x/y/Bar.png",  # case-insensitive
    ]
    for u in urls:
        assert _is_freely_licensed_image_url(u), u


def test_rejects_local_wikipedia_urls():
    # Fair-use images live under /wikipedia/en/, /wikipedia/de/, etc.
    urls = [
        "https://upload.wikimedia.org/wikipedia/en/a/a8/Movie_poster.jpg",
        "https://upload.wikimedia.org/wikipedia/de/x/y/AlbumCover.jpg",
        "https://upload.wikimedia.org/wikipedia/fr/1/2/BookCover.jpg",
    ]
    for u in urls:
        assert not _is_freely_licensed_image_url(u), u


def test_rejects_arbitrary_third_party_hosts():
    urls = [
        "https://example.com/some/image.jpg",
        "https://commons.wikimedia.org/wiki/File:Foo.jpg",  # wiki page, not upload
        "https://en.wikipedia.org/some/thing.png",
    ]
    for u in urls:
        assert not _is_freely_licensed_image_url(u), u


def test_rejects_empty_and_junk():
    assert not _is_freely_licensed_image_url("")
    assert not _is_freely_licensed_image_url("not a url")
