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

# Icon ground and paint. The MAP marker inverts this (white fill, dark
# outline) because it has to stay visible over arbitrary map tiles.
GROUND = "#dbe4f3"
PAINT = "#16243d"

# ---------------------------------------------------------------------------
# ONE SILHOUETTE PER STYLE, AND IT IS A SINGLE CLOSED PATH. (1.7.0)
#
# Two rules, both learned the hard way.
#
# SINGLE PATH. "Modern" used to be three overlapping shapes — fuselage,
# wing, tailplane — plus two nacelle rects. That is fine filled flat on a
# tile, and it is a mess on the map: the marker strokes a dark outline
# round EVERY shape, so you saw outlines crossing through the middle of the
# aeroplane and the whole thing read as a smudge. One closed path strokes
# once, on its own edge, which is what a marker needs.
#
# NO SEPARATE MARKER BODY. Each style used to carry a second, simplified
# `marker` path so the outline survived at 40px. A single path survives
# fine, and having two meant the map and the home screen were drawing
# genuinely different aeroplanes.
#
# All drawn nose-up in a 64 viewBox so they are interchangeable.
STYLES = {
    "modern": {
        "label": "Modern",
        "vb": 64,
        "icon": (
            '<path d="M32 4c2.6 0 4.4 3.4 4.7 8.6l.5 7.8 23 17.4c.5.4.7 1 .7 1.7'
            'v3.3L37.4 36.4l.9 11.9 6.6 5.4c.5.4.6 1 .6 1.6v2L32 53.6l-13.5 3.7'
            'v-2c0-.6.1-1.2.6-1.6l6.6-5.4.9-11.9L3.1 42.8v-3.3c0-.7.2-1.3.7-1.7'
            'l23-17.4.5-7.8C27.6 7.4 29.4 4 32 4z"/>'
        ),
    },
    "sharp": {
        "label": "Sharp",
        "vb": 64,
        "icon": (
            '<path d="M32 4l2.6 16.8L60 40v4.2l-21.8-7.7 1 12.5 6.2 5.3v3.2'
            'L32 54l-13.4 3.5v-3.2l6.2-5.3 1-12.5L4 44.2V40l25.4-19.2z"/>'
        ),
    },
    "rounded": {
        "label": "Rounded",
        "vb": 64,
        "icon": (
            '<path d="M32 5.5c3.4 0 5.1 3.5 5.4 8.3l.4 7.4 17 8.5c2.1 1 2.1 4.6 0 5.1'
            'l-17.4-3.2.6 12.2 5.4 4.8c1.4 1.3.8 4.2-1 3.7L32 50.9l-10.4 1.9'
            'c-1.8.5-2.4-2.4-1-3.7l5.4-4.8.6-12.2-17.4 3.2c-2.1-.5-2.1-4.1 0-5.1'
            'l17-8.5.4-7.4C26.9 9 28.6 5.5 32 5.5z"/>'
        ),
    },
    "delta": {
        "label": "Delta",
        "vb": 64,
        "icon": '<path d="M32 5l22.5 45L32 40 9.5 50z"/>',
    },
}

for s in STYLES.values():
    # Same path on the map as on the home screen. Kept as a key so callers
    # that ask for `marker` keep working, but it is never a DIFFERENT shape.
    s["marker"] = s["icon"]


def icon_svg(style, px, maskable=False):
    """One square icon. `maskable` shrinks the plane into Android's safe zone.

    Android crops maskable icons to whatever shape the launcher wants, and
    only the inner 80% is guaranteed to survive. A plane sized for the normal
    icon loses its wingtips there, so it gets its own smaller scale.
    """
    st = STYLES[style]
    vb = st["vb"]
    u = px / 64.0                      # everything below is in 64-unit space

    # MASKABLE ICONS GET NO ARTWORK. Android crops them to whatever shape
    # the launcher wants and only the inner 80% is guaranteed to survive,
    # so an arc drawn to the edges would be sliced at an arbitrary radius.
    # A plain ground and a smaller plane is the honest answer there.
    if maskable:
        scale = px * 0.52 / vb
        off = (px - vb * scale) / 2
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{px}" height="{px}" '
            f'viewBox="0 0 {px} {px}">{_defs()}'
            f'<rect width="{px}" height="{px}" fill="url(#sky)"/>'
            f'<g transform="translate({off:.2f},{off:.2f}) scale({scale:.5f})" '
            f'fill="#ffffff">{st["icon"]}</g></svg>'
        )

    # The plane sits ON the arc, at its apex (32, 28 in 64-space), banked so
    # it follows the curve. Nose-up in the middle of a square looks like a
    # logo; on the arc it looks like a flight in progress, which is what the
    # app is for.
    scale = px * 0.42 / vb
    radius = px * 0.225
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{px}" height="{px}" '
        f'viewBox="0 0 {px} {px}">{_defs()}'
        f'<rect width="{px}" height="{px}" rx="{radius:.1f}" fill="url(#sky)"/>'
        f'{_backdrop(px)}'
        f'<g transform="translate({32*u:.2f} {28*u:.2f}) rotate(55) '
        f'scale({scale:.5f}) translate({-vb/2:.1f} {-vb/2:.1f})" '
        f'fill="#ffffff">{st["icon"]}</g></svg>'
    )


def _defs():
    """The night sky the whole thing sits on."""
    return (
        '<defs><linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#12213c"/>'
        '<stop offset="1" stop-color="#0a1526"/></linearGradient></defs>'
    )


def _backdrop(px):
    """The great-circle arc, the earth's limb, and a few stars.

    THE ARC IS SPLIT, and the split is the point: solid behind the plane,
    dashed ahead of it. That is the same flown/remaining reading as the
    route strip on the tracker card, so the icon says what the app does
    rather than just being a plane in a box.

    Everything is a fraction of px rather than a fixed unit, so the 16px
    favicon is the SAME PICTURE as the 512px tile and not the same picture
    with a giant arc across it. Below 64px the fine detail is dropped
    entirely — a 3-unit dash pattern and a 1-unit star are mush at favicon
    size, and mush reads as a smeared icon rather than as detail.
    """
    u = px / 64.0
    fine = px >= 64
    limb = (f'<circle cx="{32*u:.1f}" cy="{72*u:.1f}" r="{34*u:.1f}" '
            f'fill="#1d3a63" opacity="0.55"/>')
    flown = (f'<path d="M{-4*u:.1f} {46*u:.1f} Q{14*u:.1f} {22.5*u:.1f} '
             f'{32*u:.1f} {28*u:.1f}" fill="none" stroke="#7fb0ea" '
             f'stroke-width="{1.8*u:.2f}" opacity="0.9" stroke-linecap="round"/>')
    if not fine:
        # One continuous arc at favicon size. Same shape, no dashes.
        ahead = (f'<path d="M{32*u:.1f} {28*u:.1f} Q{50*u:.1f} {35.5*u:.1f} '
                 f'{68*u:.1f} {46*u:.1f}" fill="none" stroke="#5a8fd6" '
                 f'stroke-width="{1.6*u:.2f}" opacity="0.7"/>')
        return limb + flown + ahead
    ahead = (f'<path d="M{32*u:.1f} {28*u:.1f} Q{50*u:.1f} {35.5*u:.1f} '
             f'{68*u:.1f} {46*u:.1f}" fill="none" stroke="#5a8fd6" '
             f'stroke-width="{1.6*u:.2f}" stroke-dasharray="{3*u:.1f} {3*u:.1f}" '
             f'opacity="0.7"/>')
    stars = (
        f'<circle cx="{47*u:.1f}" cy="{14*u:.1f}" r="{1.3*u:.2f}" fill="#fff" opacity="0.5"/>'
        f'<circle cx="{53*u:.1f}" cy="{22*u:.1f}" r="{0.9*u:.2f}" fill="#fff" opacity="0.35"/>'
        f'<circle cx="{13*u:.1f}" cy="{16*u:.1f}" r="{1.0*u:.2f}" fill="#fff" opacity="0.4"/>'
    )
    return limb + flown + ahead + stars


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
