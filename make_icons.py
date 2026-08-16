"""Generate every app-icon PNG from one set of vector definitions.

Run from the repo root:  python3 make_icons.py

Why this exists as a script and not a folder of hand-drawn files: there are
four styles x five sizes x two treatments. Editing those by hand guarantees
they drift apart. Change a path here and re-run; everything regenerates
consistently.

The same paths are also emitted into static/planes.js so the MAP MARKER and
the APP ICON can never disagree about what a plane looks like.
"""
import json
import os

import cairosvg

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Icon ground and paint. Dark plane on a light ground reads better as a
# home-screen tile than the reverse; the MAP marker inverts this (white fill,
# dark outline) because it has to stay visible over arbitrary map tiles.
GROUND = "#dbe4f3"
PAINT = "#16243d"

# Each style: the viewBox size its paths are drawn in, the icon body (filled),
# and the marker body (a simplified silhouette that survives a 1px outline at
# 40px). Modern drops its engine nacelles on the map for exactly that reason.
STYLES = {
    "modern": {
        "label": "Modern",
        "vb": 64,
        "icon": (
            '<path d="M32 5c2.2 0 3.6 2.6 3.7 5.6l.5 38c.1 3.6-1.4 6.4-4.2 6.4'
            's-4.3-2.8-4.2-6.4l.5-38C28.4 7.6 29.8 5 32 5z"/>'
            '<path d="M32 18l29 19v4.2L32 34.5 3 41.2V37z"/>'
            '<path d="M32 45.5l14.5 7.8v2.4L32 51.4l-14.5 4.4v-2.4z"/>'
            '<rect x="17.8" y="31.5" width="5" height="11.5" rx="2.5"/>'
            '<rect x="41.2" y="31.5" width="5" height="11.5" rx="2.5"/>'
        ),
        "marker": (
            '<path d="M32 5c2.2 0 3.6 2.6 3.7 5.6l.5 38c.1 3.6-1.4 6.4-4.2 6.4'
            's-4.3-2.8-4.2-6.4l.5-38C28.4 7.6 29.8 5 32 5z"/>'
            '<path d="M32 18l29 19v4.2L32 34.5 3 41.2V37z"/>'
            '<path d="M32 45.5l14.5 7.8v2.4L32 51.4l-14.5 4.4v-2.4z"/>'
        ),
    },
    "sharp": {
        "label": "Sharp",
        "vb": 40,
        "icon": (
            '<path d="M20 2.5l1.6 10.5L35.5 25v2.6l-13.6-4.8.6 7.8 3.9 3.3v2'
            'L20 33.8 12.6 36v-2l3.9-3.3.6-7.8L3.5 27.6V25L17.4 13z"/>'
        ),
    },
    "rounded": {
        "label": "Rounded",
        "vb": 40,
        "icon": (
            '<path d="M20 4.5c2.1 0 3.2 2.2 3.35 5.2l.25 4.6 10.6 5.3c1.3.65 1.3 2.9 0 3.2'
            'l-10.85-2 .35 7.6 3.4 3c.9.8.5 2.6-.6 2.3L20 31.6l-6.5 1.6c-1.1.3-1.5-1.5-.6-2.3'
            'l3.4-3 .35-7.6-10.85 2c-1.3-.3-1.3-2.55 0-3.2l10.6-5.3.25-4.6C16.8 6.7 17.9 4.5 20 4.5z"/>'
        ),
    },
    "delta": {
        "label": "Delta",
        "vb": 40,
        "icon": '<path d="M20 3.5l14 28-14-6.2-14 6.2z"/>',
    },
}

for s in STYLES.values():
    s.setdefault("marker", s["icon"])


def icon_svg(style, px, maskable=False):
    """One square icon. `maskable` shrinks the plane into Android's safe zone.

    Android crops maskable icons to whatever shape the launcher wants, and
    only the inner 80% is guaranteed to survive. A plane sized for the normal
    icon loses its wingtips there, so it gets its own smaller scale.
    """
    st = STYLES[style]
    vb = st["vb"]
    fill = 0.52 if maskable else 0.72
    scale = px * fill / vb
    off = (px - vb * scale) / 2
    radius = 0 if maskable else px * 0.225
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{px}" height="{px}" '
        f'viewBox="0 0 {px} {px}">'
        f'<rect width="{px}" height="{px}" rx="{radius:.1f}" fill="{GROUND}"/>'
        f'<g transform="translate({off:.2f},{off:.2f}) scale({scale:.5f})" '
        f'fill="{PAINT}">{st["icon"]}</g></svg>'
    )


def write_png(svg, name, px):
    path = os.path.join(OUT, name)
    cairosvg.svg2png(bytestring=svg.encode(), write_to=path,
                     output_width=px, output_height=px)
    return path


written = []
for style in STYLES:
    for px in (192, 512):
        written.append(write_png(icon_svg(style, px), f"icon-{style}-{px}.png", px))
    written.append(write_png(icon_svg(style, 512, maskable=True),
                             f"icon-{style}-maskable-512.png", 512))
    written.append(write_png(icon_svg(style, 180), f"apple-touch-icon-{style}.png", 180))

# The default style also lands on the legacy filenames. Anything still pointing
# at the old names keeps working instead of 404ing into a blank icon.
DEFAULT = "modern"
for px, legacy in ((192, "icon-192.png"), (512, "icon-512.png")):
    written.append(write_png(icon_svg(DEFAULT, px), legacy, px))
written.append(write_png(icon_svg(DEFAULT, 512, maskable=True), "icon-maskable-512.png", 512))
written.append(write_png(icon_svg(DEFAULT, 180), "apple-touch-icon.png", 180))
for px in (16, 32):
    written.append(write_png(icon_svg(DEFAULT, px), f"favicon-{px}x{px}.png", px))

from PIL import Image  # noqa: E402

ico_src = Image.open(os.path.join(OUT, "favicon-32x32.png"))
ico_path = os.path.join(OUT, "favicon.ico")
ico_src.save(ico_path, sizes=[(16, 16), (32, 32), (48, 48)])
written.append(ico_path)

# One source of truth for the marker, consumed by viewer.html. Regenerated
# here so a path edit above cannot leave the map drawing the old shape.
markers = {k: {"label": v["label"], "vb": v["vb"], "body": v["marker"]}
           for k, v in STYLES.items()}
js = (
    "/* GENERATED by make_icons.py - do not edit by hand.\n"
    "   The map marker and the app icon are drawn from the same paths so they\n"
    "   can never disagree. Edit make_icons.py and re-run. */\n"
    "window.PLANE_STYLES = " + json.dumps(markers, indent=2) + ";\n"
)
js_path = os.path.join(OUT, "planes.js")
with open(js_path, "w") as fh:
    fh.write(js)
written.append(js_path)

print(f"wrote {len(written)} files to {OUT}")
