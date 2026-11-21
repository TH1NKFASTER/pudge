"""Synthetic scanned manga pages and a reference renderer for the page alignment tests
(pudge/manga_align.py). No tests here.

`page()` draws a page like a scan: white paper, black panel borders, art strokes, and
optionally a stroke that runs on over the gutter into the facing page (`across`), then
tilts, scales and shifts it by known amounts. `draw_spread()` shows two pages side by side
with 0 px between them as the readers do: both at the same height, each transformed around
its centre, cut off at the gutter line, with its backing behind it.
"""

from __future__ import annotations

import math
from typing import Any

from PIL import Image, ImageDraw

WIDTH, HEIGHT = 700, 1000
# Where the printed pages' frames are (the panel area), in pixels of one page.
TOP, BOTTOM, OUTER, INNER = 70, 930, 60, 40
# The panel that runs over both pages: its bottom border line, y in pixels.
ACROSS_BOTTOM = 330


def printed_spread(*, across: bool = True, seed: int = 0) -> tuple[Image.Image, Image.Image]:
    """Two facing printed pages (screen left, screen right; no scan yet): panels in each
    page's frame and, with `across`, a top panel that runs over both pages with art (strokes,
    a dark shape, a tone band) crossing the fold."""
    width, height = 2 * WIDTH, HEIGHT
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    fold = WIDTH
    tiers = [(TOP, ACROSS_BOTTOM), (ACROSS_BOTTOM + 30, 620 - 9 * seed), (650 - 9 * seed, BOTTOM)]
    for page in (0, 1):
        x0 = OUTER if page == 0 else fold + INNER
        x1 = fold - INNER if page == 0 else width - OUTER
        for index, (y0, y1) in enumerate(tiers):
            if across and index == 0:
                continue
            draw.rectangle((x0, y0, x1, y1), outline=0, width=4)
            if index == 1:
                middle = (x0 + x1) // 2 + 25 * ((seed + page) % 3) - 25
                draw.rectangle((middle, y0, middle + 24, y1), fill=255)
                draw.line((middle, y0, middle, y1), fill=0, width=4)
                draw.line((middle + 24, y0, middle + 24, y1), fill=0, width=4)
        # Art in the lower panels.
        for k in range(14):
            x = x0 + 30 + (k * 53 + seed * 17 + page * 29) % (x1 - x0 - 80)
            y = tiers[1][0] + 25 + (k * 37) % (tiers[1][1] - tiers[1][0] - 60)
            draw.line((x, y, x + 40, y + 18 + k % 5 * 7), fill=0, width=3)
        for k in range(5):
            y = tiers[2][0] + 40 + k * 45
            draw.ellipse((x0 + 50 + k * 90, y, x0 + 100 + k * 90, y + 40), outline=0, width=3)
    if across:
        y0, y1 = tiers[0]
        draw.rectangle((OUTER, y0, width - OUTER, y1), outline=0, width=4)
        # Art over the fold: strokes, a dark shape, hatching and a grey band.
        for k in range(9):
            y = y0 + 30 + k * 24
            draw.line((fold - 160 + 13 * k, y, fold + 130 - 9 * k, y + 14 + 3 * (k % 4)), fill=0, width=4)
        draw.polygon(
            [(fold - 90, y0 + 40), (fold + 70, y0 + 60), (fold + 40, y0 + 120), (fold - 60, y0 + 100)], fill=0
        )
        for k in range(12):
            x = fold - 120 + k * 20
            draw.line((x, y1 - 70, x + 30, y1 - 20), fill=0, width=2)
        draw.rectangle((fold - 200, y0 + 170, fold + 200, y0 + 182), fill=70)
        draw.line((fold - 250, y0 + 150, fold + 250, y0 + 150), fill=0, width=3)
    return image.crop((0, 0, WIDTH, HEIGHT)), image.crop((WIDTH, 0, width, HEIGHT))


def scan(
    image: Image.Image,
    *,
    angle: float = 0.0,
    scale: float = 1.0,
    shift: tuple[float, float] = (0.0, 0.0),
    gutter_margin: int = 0,
    side: str = "right",
) -> Image.Image:
    """The page as a scanner saw it: tilted `angle` degrees clockwise, scaled and moved (pixels)
    about its centre, with `gutter_margin` blank pixels added on the gutter side; same size."""
    width, height = image.size
    cx, cy = width / 2, height / 2
    a = math.radians(angle)
    cos, sin = math.cos(a), math.sin(a)
    # Output pixel q came from input p: q = c + scale * R(p - c) + shift (y down, clockwise).
    inv = 1 / scale
    tx, ty = shift
    coefficients = (
        inv * cos,
        inv * sin,
        cx - inv * (cos * (cx + tx) + sin * (cy + ty)),
        -inv * sin,
        inv * cos,
        cy - inv * (-sin * (cx + tx) + cos * (cy + ty)),
    )
    scanned = image.transform(
        (width, height), Image.Transform.AFFINE, coefficients, Image.Resampling.BICUBIC, fillcolor=255
    )
    if gutter_margin:
        out = Image.new("L", (width, height), 255)
        if side == "right":
            out.paste(scanned.crop((gutter_margin, 0, width, height)), (0, 0))
        else:
            out.paste(scanned.crop((0, 0, width - gutter_margin, height)), (gutter_margin, 0))
        scanned = out
    return scanned


def draw_spread(
    left: Image.Image,
    right: Image.Image,
    transforms: list[dict[str, Any] | None],
    *,
    height: int = 800,
    background: int = 8,
    margin: int = 0,
) -> Image.Image:
    """Two pages as the readers draw them (screen left, screen right; `transforms` in the same
    order): the same height, the gutter between them, each page transformed (pudge.manga_align
    semantics), cut at the gutter, its backing behind it. `margin` pixels of room around."""
    sizes = [(max(1, round(height * page.width / page.height)), height) for page in (left, right)]
    canvas = Image.new(
        "RGB", (sizes[0][0] + sizes[1][0] + 2 * margin, height + 2 * margin), (background,) * 3
    )
    origins = [(margin, margin), (margin + sizes[0][0], margin)]
    for index, (page, transform) in enumerate(zip((left, right), transforms)):
        width, h = sizes[index]
        base = page.convert("RGB").resize((width, h), Image.Resampling.LANCZOS)
        ox, oy = origins[index]
        transform = transform or {}
        angle = math.radians(float(transform.get("angle", 0)))
        scale = float(transform.get("scale", 1))
        dx, dy = float(transform.get("dx", 0)) * width, float(transform.get("dy", 0)) * h
        cx, cy = width / 2, h / 2
        cos, sin = math.cos(angle), math.sin(angle)
        # Box point q from page point p: q = c + scale * R(p - c) + d; PIL wants p from q.
        inv = 1 / scale
        coefficients = (
            inv * cos,
            inv * sin,
            cx - inv * (cos * (cx + dx) + sin * (cy + dy)),
            -inv * sin,
            inv * cos,
            cy - inv * (-sin * (cx + dx) + cos * (cy + dy)),
        )
        # The box of the page with room around it (a transformed page may poke out a little).
        room = margin
        out_size = (width + 2 * room, h + 2 * room)
        shifted = list(coefficients)
        # Output pixel (u, v) of the roomy box is box point (u - room, v - room).
        shifted[2] = coefficients[2] - room * (coefficients[0] + coefficients[1])
        shifted[5] = coefficients[5] - room * (coefficients[3] + coefficients[4])
        picture = base.transform(out_size, Image.Transform.AFFINE, tuple(shifted), Image.Resampling.BICUBIC)
        mask = Image.new("L", (width, h), 255).transform(
            out_size, Image.Transform.AFFINE, tuple(shifted), Image.Resampling.BILINEAR
        )
        gutter = transform.get("gutter")
        if gutter:
            # Cut at the gutter line (the box's inner edge).
            draw = ImageDraw.Draw(mask)
            if gutter == "right":
                draw.rectangle((room + width, 0, out_size[0], out_size[1]), fill=0)
            elif room > 0:
                draw.rectangle((0, 0, room - 1, out_size[1]), fill=0)
        layer = Image.new("RGB", out_size, (0, 0, 0))
        backing_mask = Image.new("L", out_size, 0)
        if transform.get("backing") and transform["backing"][2] > transform["backing"][0]:
            x0, y0, x1, y1 = transform["backing"]
            level = int(str(transform.get("fill", "#ffffff"))[1:3], 16)
            box = (
                math.floor(room + x0 * width),
                math.floor(room + y0 * h),
                max(math.floor(room + x0 * width), math.ceil(room + x1 * width) - 1),
                max(math.floor(room + y0 * h), math.ceil(room + y1 * h) - 1),
            )
            ImageDraw.Draw(layer).rectangle(box, fill=(level,) * 3)
            ImageDraw.Draw(backing_mask).rectangle(box, fill=255)
        canvas.paste(layer, (ox - room, oy - room), backing_mask)
        canvas.paste(picture, (ox - room, oy - room), mask)
    return canvas


def gutter_lag(image: Image.Image, gutter: int, rows: range, *, band: int = 6, search: int = 30) -> int:
    """How far (pixels, + when the right side is lower) the art just right of the gutter is
    moved against the art just left of it, over `rows`: 0 when it meets."""
    import numpy as np

    values = np.asarray(image.convert("L"), dtype=np.float64)
    left = values[:, gutter - band - 1 : gutter - 1].mean(axis=1)
    right = values[:, gutter + 1 : gutter + band + 1].mean(axis=1)
    rows_list = list(rows)
    best, lag = -2.0, 0
    for shift in range(-search, search + 1):
        a = left[rows_list]
        b = right[[min(len(right) - 1, max(0, row + shift)) for row in rows_list]]
        a, b = a - a.mean(), b - b.mean()
        norm = float(np.sqrt((a * a).sum() * (b * b).sum()))
        score = float((a * b).sum()) / norm if norm else -1.0
        if score > best:
            best, lag = score, shift
    return lag


def blank_columns_at_gutter(image: Image.Image, gutter: int, rows: range) -> int:
    """Paper-only columns next to the gutter (either side) over `rows`: the gap the art has."""
    import numpy as np

    values = np.asarray(image.convert("L"), dtype=np.float64)[list(rows)]
    count = 0
    for step in (-1, 1):
        column = gutter if step > 0 else gutter - 1
        while 0 <= column < values.shape[1] and values[:, column].min() > 200:
            count += 1
            column += step
    return count


def ink_inside(page: Image.Image, transform: dict[str, Any] | None, *, tolerance: float = 1.5) -> bool:
    """Every ink pixel of `page` is drawn on its side of the gutter (nothing drawn is cut away)."""
    import numpy as np

    from pudge import manga_align

    if not transform or not transform.get("gutter"):
        return True
    values = np.asarray(page.convert("L"), dtype=np.float64)
    ys, xs = np.nonzero(values < 128)
    width, height = page.size
    aspect = width / height
    for x, y in zip(xs[::7], ys[::7]):
        drawn_x, _ = manga_align.apply(transform, (x + 0.5) / width, (y + 0.5) / height, aspect)
        if transform["gutter"] == "right" and drawn_x > 1 + tolerance / width:
            return False
        if transform["gutter"] == "left" and drawn_x < -tolerance / width:
            return False
    return True
