"""Tests for HTTP Retry-After header parsing.

When Wikimedia's REST API rate-limits us with a 429, it tells us how long
to wait via the Retry-After header. Ignoring the header and blasting
retries every second is what gets a build banned; honouring it usually
resolves the 429 after one wait."""
import datetime as dt
import re

from randompedia.summaries import _parse_retry_after


def test_seconds_form():
    assert _parse_retry_after("30") == 30.0
    assert _parse_retry_after(" 5 ") == 5.0
    assert _parse_retry_after("0") == 0.0


def test_fractional_seconds():
    assert _parse_retry_after("2.5") == 2.5


def test_http_date_form_returns_positive_seconds_when_future():
    """Retry-After can be an HTTP-date. Value should be in the future
    (or very close to now) and produce a non-negative delta."""
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=45)
    # RFC 7231 preferred format: IMF-fixdate.
    header = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
    delta = _parse_retry_after(header)
    # Give a wide window for test-execution jitter.
    assert 30 < delta < 60, f"expected ~45s, got {delta}"


def test_http_date_in_the_past_returns_zero_not_negative():
    """A past date must not produce a negative sleep — that would collapse
    to a busy loop."""
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=120)
    header = past.strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert _parse_retry_after(header) == 0.0


def test_garbage_falls_back_to_safe_default():
    """A malformed header must not throw and must not sleep for zero
    seconds (that would still be a busy loop). A past HTTP-date IS
    parseable and legitimately means "0 seconds" — that's tested
    separately above."""
    for garbage in ["not-a-number", "", "   ", "tomorrow", "🤷"]:
        result = _parse_retry_after(garbage)
        assert result >= 5.0, (
            f"garbage Retry-After ({garbage!r}) produced {result}s — "
            f"would loop too fast against a rate-limited server"
        )


def test_none_value_returns_default():
    """A missing header (None) must still produce a safe wait."""
    # The header dict lookup may return None; we should tolerate it.
    assert _parse_retry_after(None) >= 5.0  # type: ignore[arg-type]
