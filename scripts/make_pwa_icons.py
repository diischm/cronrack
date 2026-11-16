"""Draw CronRack's app icons (PWA, iOS home screen, browser tab) as PNG files.

A manual tool, run once by a developer when the logo changes; the PNGs it writes are
committed. The server never runs it, and the app has no runtime dependency on it.

Needs Pillow, which is deliberately NOT in requirements.txt or requirements-dev.txt:

    pip install pillow           # into any local Python 3.12, not the project's venv
    python3 scripts/make_pwa_icons.py

Logo: three horizontal rounded bars stacked vertically, orange #f26b2e at 100% / 60% /
32% opacity, on the Glass ground #0b0c0f. Reference SVG (viewBox 0 0 120 96): three rects
120 x 24, rx 12, at y = 0, 36 and 72.

Everything is drawn at 4x and scaled down with LANCZOS, so the bar edges are smooth.

Output (server/static/cronrack/icons/):
    icon-192.png, icon-512.png   "any" icons: a rounded dark tile, logo 60% of the width
    icon-maskable-512.png        full-bleed dark square, plain RGB; logo 50% of the width,
                                 well inside the central 80% safe zone that Android keeps
                                 when it crops the icon into a circle or squircle
    apple-touch-icon.png         180 x 180, full-bleed, plain RGB (iOS rounds the corners)
    favicon-32.png               32 x 32 tile for browser tabs, logo 72% so it stays legible
"""

from pathlib import Path

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "server" / "static" / "cronrack" / "icons"

GROUND = (0x0B, 0x0C, 0x0F)
ORANGE = (0xF2, 0x6B, 0x2E)
BAR_OPACITY = (1.0, 0.6, 0.32)

# The reference SVG, in its own units.
LOGO_W, LOGO_H = 120, 96
BAR_H, BAR_RADIUS = 24, 12
BAR_Y = (0, 36, 72)

SUPERSAMPLE = 4
TILE_RADIUS = 0.22  # corner radius of the "any" tiles, as a fraction of the size


def draw_logo(canvas: Image.Image, logo_width: float) -> None:
    """Composite the three bars, centred, onto an RGBA canvas."""
    size = canvas.width
    scale = logo_width / LOGO_W
    left = (size - logo_width) / 2
    top = (size - LOGO_H * scale) / 2
    for y, opacity in zip(BAR_Y, BAR_OPACITY, strict=True):
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).rounded_rectangle(
            (left, top + y * scale, left + logo_width, top + (y + BAR_H) * scale),
            radius=BAR_RADIUS * scale,
            fill=(*ORANGE, round(255 * opacity)),
        )
        canvas.alpha_composite(layer)


def render(size: int, logo_fraction: float, *, full_bleed: bool) -> Image.Image:
    big = size * SUPERSAMPLE
    if full_bleed:
        canvas = Image.new("RGBA", (big, big), (*GROUND, 255))
    else:
        canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        ImageDraw.Draw(canvas).rounded_rectangle(
            (0, 0, big - 1, big - 1), radius=big * TILE_RADIUS, fill=(*GROUND, 255)
        )
    draw_logo(canvas, big * logo_fraction)
    image = canvas.resize((size, size), Image.Resampling.LANCZOS)
    # Full-bleed icons must have no transparency at all: save them as plain RGB.
    return image.convert("RGB") if full_bleed else image


ICONS = {
    "icon-192.png": (192, 0.60, False),
    "icon-512.png": (512, 0.60, False),
    "icon-maskable-512.png": (512, 0.50, True),
    "apple-touch-icon.png": (180, 0.60, True),
    "favicon-32.png": (32, 0.72, False),
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, (size, fraction, full_bleed) in ICONS.items():
        path = OUT / name
        render(size, fraction, full_bleed=full_bleed).save(path, optimize=True)
        print(f"wrote {path.relative_to(REPO)}")


if __name__ == "__main__":
    main()
