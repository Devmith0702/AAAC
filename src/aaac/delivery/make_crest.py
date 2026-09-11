"""Generate the crest image used by the `full` payload variant.

Provenance matters here. The `full` variant's byte weight is evidence for the C3
claim, so every byte in it has to be honest weight rather than filler. This
script is committed so the crest can be regenerated and inspected:

    $env:PYTHONPATH="src"
    python -m aaac.delivery.make_crest

The emblem is deliberately generic -- a geometric shield, laurel arcs, and an
open book. It carries no real institution's name, motto, or branding. This is a
mock exam portal for a research testbed, and dressing it in the Department's
actual crest would be impersonation, not realism.

The image is a genuine anti-aliased raster: it is drawn at 4x and downsampled,
which is what gives it real photographic-style byte weight. No noise, texture,
or padding is added to inflate its size -- if the PNG encodes small, that is its
honest size and the variant is simply lighter than expected.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).parent / "static" / "crest.png"

# 256px, not 512. A header crest displays at ~96-128 CSS px, so 256 covers a 2x
# retina panel with nothing to spare -- which is what a real portal ships. The
# first cut was 512px / 105 KB and pushed the `full` variant over its 450 KB
# budget on its own. Shrinking to a realistic display size is honest; truncating
# one of the real vendored files would not have been.
SIZE = 256
SS = 4  # supersampling factor for anti-aliasing

NAVY = (14, 43, 84)
NAVY_DEEP = (8, 24, 51)
GOLD = (198, 158, 62)
GOLD_LIGHT = (233, 202, 122)
CREAM = (245, 241, 230)


def _radial_background(d: ImageDraw.ImageDraw, n: int) -> None:
    """Smooth radial gradient. Real tonal variation, not flat fill."""
    cx = cy = n / 2
    steps = 160
    for i in range(steps, 0, -1):
        t = i / steps
        r = (n / 2) * t
        mix = 1.0 - t
        col = tuple(
            int(NAVY_DEEP[c] + (NAVY[c] - NAVY_DEEP[c]) * mix) for c in range(3)
        )
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col + (255,))


def _shield(n: int) -> list[tuple[float, float]]:
    """Classic shield outline: straight shoulders, curved point."""
    w, h = n * 0.46, n * 0.54
    cx, top = n / 2, n * 0.235
    pts = [(cx - w / 2, top), (cx + w / 2, top), (cx + w / 2, top + h * 0.42)]
    for i in range(41):
        t = i / 40
        x = cx + (w / 2) * (1 - t)
        y = top + h * 0.42 + (h * 0.58) * math.sin(t * math.pi / 2)
        pts.append((x, y))
    for i in range(41):
        t = i / 40
        x = cx - (w / 2) * t
        y = top + h * 0.42 + (h * 0.58) * math.cos(t * math.pi / 2)
        pts.append((x, y))
    return pts


def _laurel(d: ImageDraw.ImageDraw, n: int) -> None:
    """Two laurel arcs of tapering leaves flanking the shield."""
    cx, cy = n / 2, n * 0.53
    for side in (-1, 1):
        for i in range(14):
            t = i / 13
            ang = math.radians(128 - 96 * t)
            rad = n * 0.395
            x = cx + side * math.cos(ang) * rad
            y = cy - math.sin(ang) * rad * 0.94
            leaf = n * (0.052 - 0.022 * t)
            tilt = ang + side * 0.5
            dx, dy = math.cos(tilt) * leaf, -math.sin(tilt) * leaf
            shade = tuple(
                int(GOLD[c] + (GOLD_LIGHT[c] - GOLD[c]) * t) for c in range(3)
            )
            d.ellipse(
                [x - abs(dx) - leaf * 0.34, y - abs(dy) - leaf * 0.34,
                 x + abs(dx) + leaf * 0.34, y + abs(dy) + leaf * 0.34],
                fill=shade + (255,),
            )


def _book(d: ImageDraw.ImageDraw, n: int) -> None:
    """Open book across the shield face."""
    cx, cy = n / 2, n * 0.50
    hw, hh = n * 0.148, n * 0.098
    for side in (-1, 1):
        d.polygon(
            [
                (cx, cy - hh * 0.72),
                (cx + side * hw, cy - hh * 0.30),
                (cx + side * hw, cy + hh),
                (cx, cy + hh * 0.56),
            ],
            fill=CREAM + (255,),
        )
    d.line([(cx, cy - hh * 0.72), (cx, cy + hh * 0.56)], fill=NAVY_DEEP + (255,),
           width=max(2, n // 220))
    for side in (-1, 1):
        for k in range(4):
            y = cy - hh * 0.10 + k * hh * 0.24
            d.line(
                [(cx + side * hw * 0.16, y), (cx + side * hw * 0.82, y - hh * 0.10)],
                fill=(176, 168, 150, 255), width=max(1, n // 420),
            )


def _star(d: ImageDraw.ImageDraw, n: int) -> None:
    """Five-pointed star above the book."""
    cx, cy, r = n / 2, n * 0.325, n * 0.052
    pts = []
    for i in range(10):
        ang = math.radians(-90 + i * 36)
        rad = r if i % 2 == 0 else r * 0.42
        pts.append((cx + math.cos(ang) * rad, cy + math.sin(ang) * rad))
    d.polygon(pts, fill=GOLD_LIGHT + (255,))


def build() -> Image.Image:
    n = SIZE * SS
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    _radial_background(d, n)
    d.ellipse([n * 0.035, n * 0.035, n * 0.965, n * 0.965],
              outline=GOLD + (255,), width=max(3, n // 110))
    d.ellipse([n * 0.072, n * 0.072, n * 0.928, n * 0.928],
              outline=GOLD_LIGHT + (200,), width=max(2, n // 300))
    _laurel(d, n)

    shield = _shield(n)
    d.polygon(shield, fill=CREAM + (255,))
    d.line(shield + [shield[0]], fill=GOLD + (255,), width=max(3, n // 150))
    d.polygon(
        [(n / 2 - n * 0.23, n * 0.235), (n / 2 + n * 0.23, n * 0.235),
         (n / 2 + n * 0.23, n * 0.30), (n / 2 - n * 0.23, n * 0.30)],
        fill=NAVY + (255,),
    )
    _star(d, n)
    _book(d, n)

    return img.resize((SIZE, SIZE), Image.LANCZOS)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    build().save(OUT, format="PNG", optimize=True)
    print(f"wrote {OUT}  ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
