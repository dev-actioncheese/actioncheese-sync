# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate the ActionCheese Sync logo and macOS .icns.

The mark is a block of ActionCheese cheese with the waveform *cut out* of it:
two envelopes, one per device, mirrored about a slit through the middle. Same
shape above and below, because that is what a synced multicam timeline looks
like. The cuts are true holes -- they punch through to the dark backing, so the
yellow reads as a solid block of cheese rather than a panel with marks drawn on
it.

Behind them the block is pitted with darker-yellow holes, and the pitting runs
right under the waveform rather than politely avoiding it -- a hole that stops
short of a bar looks painted on, where one that disappears behind it and comes
out the other side reads as a hole in a solid thing.

The block is not a tidy rounded rectangle either. Holes that happened to fall
on an edge have taken bites out of it, so the silhouette is chewed rather than
machined. A block of cheese is cut from something bigger; it should look it.

No clapperboard: ActionCheese itself carries the clapper, and the two apps are
easier to tell apart in a Dock when only one of them does.

Legibility at 16 px drove the rest: few cuts, thick strokes, and below 32 px
the waveform drops away entirely, leaving just the slit. Every small waveform
variant tried at that size came out looking like two digits.

    python packaging/make_icon.py        # writes assets/ and packaging/icon.icns
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

SIZE = 1024
#: Supersampling factor. The cuts are punched into an alpha mask, and a mask
#: drawn at final size leaves visibly stepped edges on the rounded ends.
SS = 4

# Sampled from CamLink/landing/actioncheese-icon.png so the two apps sit
# together in a Dock without one looking like a knock-off of the other.
BACKING_TOP = (38, 43, 59)
BACKING_BOTTOM = (16, 18, 24)
CHEESE_TOP = (255, 219, 88)
CHEESE_BOTTOM = (255, 175, 19)
#: The pitting. Darker than the cheese at every point of the gradient, but not
#: so dark that it reads as a cut -- the cuts are the only black on the tile.
PIT = (233, 154, 10)

#: Bar heights as a fraction of the available reach. Irregular on purpose --
#: a regular pattern reads as a graphic equaliser rather than a recording.
ENVELOPE = [0.38, 1.00, 0.62, 0.86]

#: Three tiers of artwork, because a 16 px tile has about ten usable pixels
#: and a 32 px one has twice that. At or below TINY only the slit is cut; up
#: to SMALL a three-cut waveform still reads; above that, the full four.
ENVELOPE_SMALL = [0.55, 1.00, 0.70]
TINY_THRESHOLD = 20
SMALL_THRESHOLD = 40

#: Where the block sits inside the tile, and how square it is. A smaller radius
#: than the tile's own, so the cheese reads as a slab set into the dark rather
#: than as the tile itself.
BLOCK_INSET = 0.052
BLOCK_RADIUS = 0.115

#: Holes that fell on an edge, as (x, y, radius) fractions of the icon. Their
#: centres sit on or outside the block's border, so each takes a bite out of
#: the silhouette and shows the dark behind.
EDGE_BITES = [
    (0.500, 0.041, 0.062), (0.786, 0.048, 0.040),
    (0.960, 0.352, 0.055), (0.951, 0.690, 0.036),
    (0.640, 0.958, 0.058), (0.268, 0.952, 0.038),
    (0.044, 0.620, 0.060), (0.052, 0.268, 0.034),
]

#: Texture pits, as (x, y, radius) fractions of the icon. Scattered once by
#: the generator below and then frozen, so the icon is reproducible and can be
#: hand-corrected. They run under the waveform on purpose.
PITS = [
    (0.098, 0.150, 0.052), (0.213, 0.083, 0.030), (0.330, 0.132, 0.022),
    (0.072, 0.320, 0.034), (0.178, 0.268, 0.019), (0.612, 0.088, 0.026),
    (0.735, 0.140, 0.044), (0.880, 0.086, 0.024), (0.906, 0.238, 0.037),
    (0.822, 0.318, 0.020), (0.062, 0.612, 0.028), (0.152, 0.712, 0.046),
    (0.088, 0.858, 0.033), (0.258, 0.872, 0.023), (0.372, 0.908, 0.031),
    (0.640, 0.902, 0.027), (0.768, 0.842, 0.049), (0.905, 0.718, 0.031),
    (0.852, 0.918, 0.021), (0.930, 0.548, 0.024), (0.068, 0.468, 0.020),
    (0.508, 0.930, 0.018), (0.470, 0.074, 0.019), (0.055, 0.742, 0.018),
    (0.948, 0.408, 0.019), (0.298, 0.045, 0.014), (0.690, 0.960, 0.015),
]

#: At 32 px the fine pitting turns to noise, so a handful of larger pits stand
#: in for it. Deliberately lopsided: four tidy ones read as corner dots, which
#: is the thing the pitting replaced.
PITS_SMALL = [(0.118, 0.148, 0.068), (0.300, 0.075, 0.036),
              (0.880, 0.205, 0.058), (0.070, 0.560, 0.040),
              (0.160, 0.848, 0.052), (0.430, 0.925, 0.032),
              (0.812, 0.838, 0.070), (0.925, 0.612, 0.033)]

#: The bites that still read at 32 px: two, big, on opposite edges. Four
#: roughened the whole outline by a pixel each, which stops looking like
#: cheese and starts looking like a rendering fault. At 16 px there are none
#: at all -- see :func:`render`.
EDGE_BITES_SMALL = [(0.500, 0.030, 0.082), (0.500, 0.970, 0.078)]


def _vertical_gradient(size: int, top: tuple[int, int, int],
                       bottom: tuple[int, int, int]) -> Image.Image:
    strip = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        strip.putpixel((0, y), tuple(
            round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)
        ))
    return strip.resize((size, size))


def _rounded_mask(size: int, radius_ratio: float = 0.225) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=round(size * radius_ratio), fill=255)
    return mask


def _cut_waveform(draw: ImageDraw.ImageDraw, size: int, centre_y: float,
                  direction: int, envelope: list[float], width_ratio: float,
                  left: float, right: float, reach: float, gap: float) -> None:
    """Punch one device's bars out of the cheese, growing away from the slit."""
    pitch = (right - left) / len(envelope)
    width = pitch * width_ratio
    for index, level in enumerate(envelope):
        centre = left + pitch * (index + 0.5)
        height = max(width, reach * level)
        near = centre_y + direction * gap
        far = near + direction * height
        top, bottom = sorted((near, far))
        draw.rounded_rectangle((centre - width / 2, top, centre + width / 2, bottom),
                               radius=width / 2, fill=0)


def render(size: int = SIZE, *, small: bool = False,
           tiny: bool = False) -> Image.Image:
    """Render the mark.

    ``small`` selects the three-cut waveform, ``tiny`` drops it altogether.
    """
    small = small or tiny
    s = size * SS
    outline = _rounded_mask(s)

    # The dark backing: what every cut and every bite reveals.
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    image.paste(_vertical_gradient(s, BACKING_TOP, BACKING_BOTTOM), (0, 0), outline)

    # The block: a slab set into the tile, with the waveform and the edge bites
    # taken out of its alpha so the backing shows through both.
    inset = s * BLOCK_INSET
    cheese_mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(cheese_mask).rounded_rectangle(
        (inset, inset, s - inset - 1, s - inset - 1),
        radius=round(s * BLOCK_RADIUS), fill=255)
    cut = ImageDraw.Draw(cheese_mask)
    if not tiny:
        for bx, by, br in (EDGE_BITES_SMALL if small else EDGE_BITES):
            cut.ellipse((s * (bx - br), s * (by - br),
                         s * (bx + br), s * (by + br)), fill=0)

    middle = s * 0.5
    inset = s * (0.165 if small else 0.245)
    left, right = inset, s - inset
    slit = s * (0.082 if tiny else 0.058 if small else 0.042)
    gap = slit * (0.95 if small else 1.10)

    if not tiny:
        envelope = ENVELOPE_SMALL if small else ENVELOPE
        reach = s * (0.215 if small else 0.245)
        ratio = 0.34 if small else 0.40
        _cut_waveform(cut, s, middle, -1, envelope, ratio, left, right, reach, gap)
        _cut_waveform(cut, s, middle, +1, envelope, ratio, left, right, reach, gap)

    # The slit itself -- the sync point, and the only mark small sizes keep.
    cut.rounded_rectangle((left - s * 0.075, middle - slit / 2,
                           right + s * 0.075, middle + slit / 2),
                          radius=slit / 2, fill=0)

    # The pitting, painted on the face before it is masked, so a pit is clipped
    # by the block's edge and by the waveform alike -- which is what makes one
    # read as passing behind a bar rather than stopping at it.
    face = _vertical_gradient(s, CHEESE_TOP, CHEESE_BOTTOM).convert("RGBA")
    if not tiny:
        pit = ImageDraw.Draw(face)
        for px, py, pr in (PITS_SMALL if small else PITS):
            pit.ellipse((s * (px - pr), s * (py - pr),
                         s * (px + pr), s * (py + pr)), fill=PIT)

    cheese = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    cheese.paste(face, (0, 0), cheese_mask)
    image.alpha_composite(cheese)
    return image.resize((size, size), Image.LANCZOS)


def icon_for(pixels: int) -> Image.Image:
    """Artwork for one pixel size, drawn at that size rather than downscaled."""
    tiny = pixels <= TINY_THRESHOLD
    small = pixels <= SMALL_THRESHOLD
    return render(max(pixels, 256 if small else 512), small=small, tiny=tiny) \
        .resize((pixels, pixels), Image.LANCZOS)


def write_icns(destination: Path) -> bool:
    """Build a .icns via iconutil; returns False when unavailable."""
    if not shutil.which("iconutil"):
        return False
    iconset = destination.parent / "icon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True)
    for base in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            pixels = base * scale
            suffix = "" if scale == 1 else "@2x"
            icon_for(pixels).save(iconset / f"icon_{base}x{base}{suffix}.png")
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(destination)],
                   check=True)
    shutil.rmtree(iconset)
    return True


def write_ico(destination: Path) -> None:
    """Windows wants every size in one .ico file.

    Each size is drawn at its own size rather than downscaled from the master,
    the same as the .icns, so the simplified small artwork is what Windows
    shows in the taskbar and the title bar.
    """
    sizes = (16, 24, 32, 48, 64, 128, 256)
    images = [icon_for(size) for size in sizes]
    images[-1].save(destination, format="ICO",
                    sizes=[(size, size) for size in sizes],
                    append_images=images[:-1])


def main() -> int:
    ASSETS.mkdir(exist_ok=True)
    master = render()
    master.save(ASSETS / "logo.png")
    for size in (512, 256, 128, 64, 32, 16):
        icon_for(size).save(ASSETS / f"logo-{size}.png")

    # A wordmark for the README: icon on the left, name to the right.
    banner = Image.new("RGBA", (1200, 320), (0, 0, 0, 0))
    banner.paste(master.resize((256, 256), Image.LANCZOS), (32, 32))
    banner.save(ASSETS / "wordmark.png")

    ico = ROOT / "packaging" / "icon.ico"
    write_ico(ico)
    print(f"wrote {ico.relative_to(ROOT)}")

    icns = ROOT / "packaging" / "icon.icns"
    if write_icns(icns):
        print(f"wrote {icns.relative_to(ROOT)}")
    else:
        print("iconutil not found; skipped .icns (the app will use a default icon)")
    print(f"wrote {(ASSETS / 'logo.png').relative_to(ROOT)} "
          f"and {len(list(ASSETS.glob('logo-*.png')))} sizes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
