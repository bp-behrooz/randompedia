"""Make sure the colophon's 'Generator' link actually points at the project,
not a placeholder."""
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from randompedia import PROJECT_URL
from randompedia.epub_build import BookMeta, build_epub, stable_book_id
from randompedia.summaries import ArticleSummary


def _sample_article() -> ArticleSummary:
    return ArticleSummary(
        title="Sample", key="Sample", lang="en", description=None,
        extract_html="<p>x</p>", extract_text="x",
        image_url=None, raw_image_url=None,
        url="https://en.wikipedia.org/wiki/Sample",
    )


def _meta() -> BookMeta:
    return BookMeta(title="t", identifier=stable_book_id("t", "en", "1", "s"),
                    lang="en", seed="s")


def _read_colophon(zf: zipfile.ZipFile) -> str:
    name = next(n for n in zf.namelist() if "colophon" in n and n.endswith(".xhtml"))
    return zf.read(name).decode("utf-8")


def test_project_url_is_a_real_project_url():
    """PROJECT_URL must be a proper URL that includes a project path, not a
    bare host (regression: colophon used to link to 'https://github.com/')."""
    parsed = urlparse(PROJECT_URL)
    assert parsed.scheme in ("http", "https"), PROJECT_URL
    assert parsed.netloc, PROJECT_URL
    # A bare host would have an empty or "/" path; a real repo URL has more.
    assert parsed.path.strip("/"), f"PROJECT_URL has no path: {PROJECT_URL!r}"
    # Two segments = owner/repo on GitHub.
    assert len(parsed.path.strip("/").split("/")) >= 2, PROJECT_URL


def test_colophon_generator_link_points_at_project(tmp_path: Path):
    out = tmp_path / "c.epub"
    build_epub(articles=[_sample_article()], output_path=out, meta=_meta(),
               with_images=False)
    with zipfile.ZipFile(out) as zf:
        text = _read_colophon(zf)
    # The link is present…
    assert f'href="{PROJECT_URL}"' in text
    # …and the placeholder is gone.
    assert 'href="https://github.com/"' not in text
    assert 'href="https://github.com/">randompedia</a>' not in text


def test_user_agent_includes_project_url():
    from randompedia import USER_AGENT
    assert PROJECT_URL in USER_AGENT
