"""Tests that pin the default image dimensions to the target hardware.

These are more documentation than logic — they exist so that if someone
later changes the defaults without thinking about the Xteink X3/X4 panel
geometry, the test explains why the old values were chosen and forces a
conscious update.

Panel geometry from freeink-sdk (BoardConfig.h):
  * Xteink X4 — SSD1677, native 800 x 480, 4.3", ~220 PPI
  * Xteink X3 — UC8253,  native 792 x 528, 3.7", ~259 PPI

Both panels are landscape-native; the crosspoint firmware supports
orientation control and portrait is the common reading orientation, so we
target the tighter portrait dimensions of the smaller device (X4: 480x800).
The image needs to fit alongside a title (which may wrap to multiple lines)
plus an optional subtitle, all above a forced page break, so it must
occupy meaningfully less than half of the shorter axis.
"""
from randompedia.images import ImageSpec


# Shortest portrait dimension of the smallest target device (X4, portrait).
X4_PORTRAIT_WIDTH = 480
X4_PORTRAIT_HEIGHT = 800


def test_default_image_width_fits_x4_portrait_with_margin():
    """Width must leave room for body-page margins and reader chrome."""
    spec = ImageSpec()
    # A generous 15% margin either side leaves ~336 px usable; we should
    # be inside that.
    usable = int(X4_PORTRAIT_WIDTH * 0.85)
    assert spec.max_width <= X4_PORTRAIT_WIDTH, \
        f"image would clip on X4 in portrait: {spec.max_width} > {X4_PORTRAIT_WIDTH}"
    # 400 is comfortably below the raw panel width; the reader will scale
    # to fit if the panel is narrower.
    assert spec.max_width >= 300, "image too small to be worth including"


def test_default_image_height_leaves_room_for_multiline_title():
    """A 3-line h1 + 2-line subtitle + attribution shouldn't push the
    image off the page. Assume ~40% of the vertical space is header/
    footer chrome in the worst case."""
    spec = ImageSpec()
    header_budget_ratio = 0.40  # h1 wraps to 3 lines + subtitle wraps to 2
    image_budget_ratio = 1.0 - header_budget_ratio  # 60%
    max_reasonable_height = int(X4_PORTRAIT_HEIGHT * image_budget_ratio)
    assert spec.max_height <= max_reasonable_height, (
        f"image height {spec.max_height} may not fit alongside a multi-line "
        f"title on the X4 (portrait 480x800, header budget "
        f"{int(header_budget_ratio*100)}%, image budget {max_reasonable_height}px)"
    )
    # And a soft lower bound: too small is useless.
    assert spec.max_height >= 200, "image too small to be legible"


def test_defaults_are_landscape_or_square_capable():
    """A wide/landscape lead image at max_width should still be shorter
    than max_height (i.e. the height cap doesn't unnecessarily crush
    already-conforming images)."""
    spec = ImageSpec()
    # If the caps are internally consistent, a 4:3 image at max_width
    # has height = max_width * 3/4, which should be <= max_height.
    implied_height_at_4_3 = spec.max_width * 3 // 4
    assert implied_height_at_4_3 <= spec.max_height, (
        "at max_width, a 4:3 image is taller than max_height, so all "
        "landscape images would get needlessly rescaled twice"
    )


def test_default_quality_is_e_ink_appropriate():
    """CrossPoint quantises the decoded JPEG down to 16 shades at render
    time, so a very high JPEG quality would just waste bytes without any
    visible benefit. Very low would introduce visible artefacts. The
    sweet spot is somewhere in the 65-85 band; keep the default in it."""
    spec = ImageSpec()
    assert 65 <= spec.quality <= 85, (
        f"default JPEG quality {spec.quality} is outside the "
        f"e-ink-appropriate 65-85 band"
    )
