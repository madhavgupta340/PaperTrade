#!/usr/bin/env python3
"""Generates the app icons (favicon, iOS home screen, Android/PWA) from the logo geometry.

The logo is three axis-aligned bars, so each pixel's coverage is computed exactly, which gives
crisp anti-aliased edges at any size with no image libraries. Run:  python3 scripts/make_icons.py
"""
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "static"

BG = (13, 13, 11)          # near-black, matches the dark theme
INK = (242, 241, 234)      # paper white
# The header logo draws its third bar in paper white at 35% opacity; on the dark background
# that is this solid grey, so the icon matches the logo in the app.
FADED = tuple(round(0.35 * i + 0.65 * b) for i, b in zip(INK, BG))

# Same geometry as the logo in the page header (static/index.html), in its 22x22 grid:
# (x, y, w, h, color). GROUP is the bounding box of the bars.
BARS = [(0, 12, 6, 10, INK), (8, 6, 6, 16, INK), (16, 0, 6, 22, FADED)]
GROUP = (0, 0, 22, 22)


def render(size: int, content: float) -> bytes:
    """RGBA pixels: dark square background, bars scaled so the group is `content` of the width."""
    scale = size * content / GROUP[2]
    off = (size - GROUP[2] * scale) / 2
    rects = [((x - GROUP[0]) * scale + off, (y - GROUP[1]) * scale + off,
              (x - GROUP[0] + w) * scale + off, (y - GROUP[1] + h) * scale + off, c) for x, y, w, h, c in BARS]
    rows = []
    for py in range(size):
        row = bytearray([0])  # PNG filter type 0
        for px in range(size):
            r, g, b = BG
            for x0, y0, x1, y1, color in rects:
                cx = max(0.0, min(px + 1, x1) - max(px, x0))
                cy = max(0.0, min(py + 1, y1) - max(py, y0))
                a = cx * cy  # exact pixel coverage of an axis-aligned rectangle
                if a:
                    r = r + (color[0] - r) * a
                    g = g + (color[1] - g) * a
                    b = b + (color[2] - b) * a
            row += bytes((round(r), round(g), round(b), 255))
        rows.append(bytes(row))
    return b"".join(rows)


def write_png(path: Path, size: int, content: float) -> None:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = render(size, content)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    print(f"wrote {path.relative_to(OUT.parent)} ({size}x{size}, {len(png) // 1024 or 1} KB)")


def write_svg(path: Path) -> None:
    hexc = lambda c: "#%02x%02x%02x" % c  # noqa: E731
    bars = "".join(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{hexc(c)}"/>' for x, y, w, h, c in BARS)
    path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 22 22">'
                    f'<rect width="22" height="22" rx="4.5" fill="{hexc(BG)}"/>'
                    f'<g transform="translate(11 11) scale(.6) translate(-11 -11)">{bars}</g></svg>\n')
    print(f"wrote {path.relative_to(OUT.parent)}")


if __name__ == "__main__":
    write_svg(OUT / "favicon.svg")                          # browser tab (sharp at any zoom)
    write_png(OUT / "icons" / "favicon-32.png", 32, 0.70)   # fallback for browsers without SVG favicons
    write_png(OUT / "icons" / "apple-touch-icon.png", 180, 0.56)  # iOS home screen (iOS rounds the corners)
    write_png(OUT / "icons" / "icon-192.png", 192, 0.56)    # Android / install
    write_png(OUT / "icons" / "icon-512.png", 512, 0.56)    # Android splash / install
    write_png(OUT / "icons" / "icon-maskable-512.png", 512, 0.44)  # fits the circular safe zone when masked
