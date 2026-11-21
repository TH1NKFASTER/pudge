"""Straighten and align scanned manga pages shown two side by side.

Scanned volumes are scanned page by page: each page is a little rotated, cropped and scaled
differently, so in the two-page view the panel borders are not level and the art that goes on
over the gutter does not meet. For each page this finds, once (cached by the page's identity):

- its tilt, from the long straight panel borders (edge points projected at small angles: the
  angle that stacks them into the sharpest rows and columns);
- after straightening: the blank scan margin on each side, the top and bottom of the drawn
  frame when they are clear border lines, and a strip of the art along each side.

For two facing pages it then works out one transform per page (``pair_transforms``): straighten
it, give both frames the same height and vertical position (a small scale and shift), and when
the art crosses the gutter (it touches both inner edges) take the blank margin at the gutter
away and refine the vertical shift by matching the two inner strips. Only when confident (clear
border lines, a tilt of at most 2 degrees, a scale within 3 %, small shifts); otherwise the page
is left as it is. Nothing drawn is ever cut away: a page only moves over its own blank margin.

A transform is ``{"angle", "scale", "dx", "dy"}`` (plus ``"gutter"`` and ``"backing"`` in a
pair), applied to the page as drawn (the image and its OCR text boxes together), around its
centre: rotated by ``angle`` degrees clockwise, scaled by ``scale``, then moved by ``dx`` of its
width and ``dy`` of its height. In a pair both pages are first drawn at the same height, side by
side, the inner edges meeting at the gutter; each one is then cut off at the gutter line
(``gutter``: its side that faces the gutter) and ``backing`` (``[x0, y0, x1, y1]`` in fractions of
the page box, colour ``fill``) is painted behind it so a straightened edge leaves no dark wedge.
The Mac reader (CSS) and the iPhone/iPad (MangaSpreads.swift) apply exactly this; the shared
cases are tests/fixtures/manga_alignment.json.

numpy is optional (the "sync" extra brings it): without it pages are simply not aligned.
"""

from __future__ import annotations

import base64
import math
import zlib
from typing import Any

from PIL import Image

try:  # pragma: no cover - the import itself
    import numpy as np
except ImportError:  # pragma: no cover - a Mac without the sync extra
    np = None  # type: ignore[assignment]

# Bump when the analysis changes: cached pages of an older version are analysed again.
VERSION = 1

MAX_ANGLE = 2.0  # degrees: a page tilted more is probably drawn that way
MAX_SCALE = 0.03  # each page changes size by at most 3 %
MAX_SHIFT = 0.015  # each page moves up or down by at most 1.5 % of its height
MAX_TRIM = 0.06  # at most 6 % of a page's width is blank margin taken away at the gutter
MIN_ANGLE = 0.05  # degrees: a smaller tilt is not worth turning the page for
WORK_HEIGHT = 1400  # larger scans are analysed at this height
_WORK_PIXELS = 2_000_000  # and at most this many pixels (a very wide or very long image)
_MIN_SIDE = 64  # a smaller image is no page to straighten
_INK = 0.5  # darkness above this is ink


def available() -> bool:
    """Whether pages can be analysed here (numpy is installed)."""
    return np is not None


# ----------------------------------------------------------------------------- one page
def _darkness(image: Image.Image) -> tuple[Any, float, float] | None:
    """0 (paper) … 1 (ink) at the working size with the paper's and the ink's grey, or None for
    a blank or low-contrast page."""
    gray = image.convert("L")
    factor = min(1.0, WORK_HEIGHT / gray.height, math.sqrt(_WORK_PIXELS / (gray.width * gray.height)))
    if factor < 1.0:
        size = (max(1, round(gray.width * factor)), max(1, round(gray.height * factor)))
        gray = gray.resize(size, Image.Resampling.BOX)
    values = np.asarray(gray, dtype=np.float32)
    paper = float(np.percentile(values, 92))
    ink = float(np.percentile(values, 2))
    if paper - ink < 80:
        return None
    return np.clip((paper - values) / (paper - ink), 0.0, 1.0), paper, ink


def _runs(mask: Any, length: int, axis: int) -> Any:
    """Pixels of `mask` inside a run of at least 80 % of `length` along `axis` (line-like)."""
    counts = np.cumsum(mask, axis=axis, dtype=np.int32)
    pad = [(0, 0), (0, 0)]
    pad[axis] = (1, 0)
    counts = np.pad(counts, pad)
    half = length // 2
    size = mask.shape[axis]
    low = np.clip(np.arange(size) - half, 0, size)
    high = np.clip(np.arange(size) + half + 1, 0, size)
    if axis == 1:
        window = counts[:, high] - counts[:, low]
    else:
        window = counts[high, :] - counts[low, :]
    return mask & (window >= int(length * 0.8))


_BLUR = (
    np.exp(-0.5 * np.arange(-3, 4) ** 2) / np.exp(-0.5 * np.arange(-3, 4) ** 2).sum()
    if np is not None
    else None
)


def _profile_score(coordinate: Any, offset: int, size: int) -> tuple[float, Any]:
    """How sharply the points stack into rows (sum of squares of their profile) and the profile.

    Each point is shared between its two nearest bins and the profile blurred by a pixel before
    squaring, so the score changes smoothly with the angle and no angle is favoured for
    putting whole-pixel positions on whole bins (at 0 degrees every point would)."""
    position = np.clip(coordinate + offset, 0, size - 2)
    low = np.floor(position).astype(np.int64)
    part = position - low
    bins = np.bincount(low, weights=1 - part, minlength=size) + np.bincount(
        low + 1, weights=part, minlength=size
    )
    blurred = np.convolve(bins, _BLUR, mode="same")
    return float(np.dot(blurred, blurred)), bins


def _tilt(dark: Any) -> tuple[float | None, dict[str, Any]]:
    """The content's tilt in degrees clockwise and what it was found from."""
    height, width = dark.shape
    run = max(9, round(min(width, height) * 0.02))
    edges_y = np.abs(np.diff(dark, axis=0)) > 0.4
    edges_x = np.abs(np.diff(dark, axis=1)) > 0.4
    ys_h, xs_h = np.nonzero(_runs(edges_y, run, axis=1))
    ys_v, xs_v = np.nonzero(_runs(edges_x, run, axis=0))
    info: dict[str, Any] = {"points": int(len(xs_h) + len(xs_v))}
    if len(xs_h) + len(xs_v) < 200:
        return None, info
    xs_h = xs_h - width / 2
    ys_v = ys_v - height / 2
    span = int(width * 0.06 + height * 0.06) + 4
    size_h = height + 2 * span
    size_v = width + 2 * span

    def score(angle: float) -> tuple[float, float]:
        slope = math.tan(math.radians(angle))
        horizontal = _profile_score(ys_h - xs_h * slope, span, size_h)[0] if len(xs_h) else 0.0
        vertical = _profile_score(xs_v + ys_v * slope, span, size_v)[0] if len(xs_v) else 0.0
        return horizontal, vertical

    coarse = np.arange(-3.0, 3.0001, 0.1)
    scores = np.array([score(float(angle)) for angle in coarse])
    total = scores.sum(axis=1)
    best = float(coarse[int(np.argmax(total))])
    fine = np.arange(best - 0.12, best + 0.12001, 0.01)
    fine_scores = np.array([score(float(angle)) for angle in fine])
    fine_total = fine_scores.sum(axis=1)
    angle = _peak(fine, fine_total)
    # Each direction on its own: two estimates that must agree when both have lines.
    angle_h = _peak(fine, fine_scores[:, 0])
    angle_v = _peak(fine, fine_scores[:, 1])
    # Long lines at the angle found: profile peaks of a quarter of the page or more.
    slope = math.tan(math.radians(angle))
    lines_h = (
        _long_lines(_profile_score(ys_h - xs_h * slope, span, size_h)[1], width * 0.25) if len(xs_h) else 0
    )
    lines_v = (
        _long_lines(_profile_score(xs_v + ys_v * slope, span, size_v)[1], height * 0.25) if len(xs_v) else 0
    )
    # Sharpness: the best angle against one a degree away (a page of lines is much sharper).
    away = [score(angle - 1.0), score(angle + 1.0)]
    sharp = float(fine_total.max()) / max(1.0, max(sum(item) for item in away))
    info.update(
        angle_h=round(angle_h, 3),
        angle_v=round(angle_v, 3),
        lines_h=lines_h,
        lines_v=lines_v,
        sharp=round(sharp, 3),
    )
    if lines_h + lines_v < 3 or sharp < 1.15:
        return None, info
    if lines_h >= 1 and lines_v >= 1 and abs(angle_h - angle_v) > 0.3:
        return None, info
    if abs(angle) > MAX_ANGLE:
        return None, info
    return angle, info


def _peak(angles: Any, scores: Any) -> float:
    """The angle of the best score (the middle of a flat top)."""
    best = float(np.max(scores))
    return float(np.mean(angles[scores >= best - abs(best) * 1e-9]))


def _long_lines(profile: Any, length: float) -> int:
    """Peaks of at least `length` (adjacent bins together), one per line."""
    if not len(profile):
        return 0
    merged = profile + np.concatenate(([0.0], profile[:-1])) + np.concatenate((profile[1:], [0.0]))
    count = 0
    index = 0
    size = len(merged)
    while index < size:
        if merged[index] >= length:
            count += 1
            index += 6  # one line has two edges a few pixels apart
        else:
            index += 1
    return count


def _open2(mask: Any) -> Any:
    """A 2×2 opening: dust specks go, lines and art stay."""
    eroded = mask[:-1, :-1] & mask[1:, :-1] & mask[:-1, 1:] & mask[1:, 1:]
    out = np.zeros_like(mask)
    out[:-1, :-1] |= eroded
    out[1:, :-1] |= eroded
    out[:-1, 1:] |= eroded
    out[1:, 1:] |= eroded
    return out


def _encode(values: Any) -> str:
    """A 0…1 profile as compressed bytes (the cache is JSON)."""
    data = np.clip(np.rint(np.asarray(values, dtype=np.float64) * 255), 0, 255).astype(np.uint8).tobytes()
    return base64.b64encode(zlib.compress(data, 6)).decode("ascii")


def _decode(text: str) -> Any:
    return np.frombuffer(zlib.decompress(base64.b64decode(text)), dtype=np.uint8).astype(np.float64) / 255.0


def analyze(image: Image.Image) -> dict[str, Any]:
    """What alignment needs to know about one page (JSON-ready, cached by the caller).

    ``angle`` is None when the tilt is not clear (the page is then never rotated); the margins,
    frame edges and strips are measured on the straightened page, in fractions of its size."""
    result: dict[str, Any] = {"version": VERSION, "width": int(image.width), "height": int(image.height)}
    if np is None or min(image.size) < _MIN_SIDE:
        result["angle"] = None
        return result
    levels = _darkness(image)
    if levels is None:
        result["angle"] = None
        return result
    dark, paper, ink_level = levels
    angle, info = _tilt(dark)
    result["angle"] = None if angle is None else round(angle, 3)
    result["found"] = info
    height, width = dark.shape
    rotated = dark
    if angle is not None and abs(angle) >= MIN_ANGLE:
        # Straightened on a larger canvas: nothing is lost at the corners.
        picture = Image.fromarray(dark, mode="F").rotate(
            angle, resample=Image.Resampling.BILINEAR, expand=True
        )
        rotated = np.asarray(picture, dtype=np.float32)
    canvas_h, canvas_w = rotated.shape
    x0 = (canvas_w - width) / 2.0
    y0 = (canvas_h - height) / 2.0
    ink = _open2(rotated > _INK)
    counts = ink.sum(axis=0)
    columns = np.nonzero(counts >= 2)[0]
    rows = np.nonzero(ink.sum(axis=1) >= 2)[0]
    if not len(columns) or not len(rows):
        result["angle"] = None
        return result
    left, right = int(columns[0]), int(columns[-1])
    # A column with a speck or the very end of a line (a few pixels of ink) beyond the art is
    # paper too, but never more than a few columns of it.
    solid = np.nonzero(counts >= max(4, round(height * 0.003)))[0]
    if len(solid):
        give = max(2, round(width * 0.004))
        left = min(int(solid[0]), left + give)
        right = max(int(solid[-1]), right - give)
    top, bottom = int(rows[0]), int(rows[-1])
    # Blank margin between the content and the page's edge (negative: straightened content
    # reaches past where the edge was).
    result["margins"] = {
        "left": round((left - x0) / width, 5),
        "right": round((x0 + width - 1 - right) / width, 5),
        "top": round((top - y0) / height, 5),
        "bottom": round((y0 + height - 1 - bottom) / height, 5),
    }
    # The frame: a clear border line along the top (bottom) of the content with blank paper
    # beyond it. Pages whose art runs off the edge (bleed) have none there.
    lines = _runs(ink, max(9, round(width * 0.02)), axis=1)
    content_width = max(1, right - left + 1)

    def frame_line(row: int) -> bool:
        band = lines[max(0, row - 1) : row + 4, left : right + 1]
        return bool(band.any(axis=0).sum() >= content_width * 0.4)

    frame: dict[str, float] = {}
    if top - y0 >= 2 and frame_line(top):
        frame["top"] = round((top - y0) / height, 5)
    if y0 + height - 1 - bottom >= 2 and frame_line(bottom - 3):
        frame["bottom"] = round((bottom - y0) / height, 5)
    result["frame"] = frame
    # Along each side, just inside the content: where horizontal lines and edges reach the side
    # (what has to meet at the gutter) and the rows where content touches the side without a
    # border line along it (art that goes on over the gutter).
    band = max(6, round(width * 0.03))
    edges = np.zeros_like(ink)
    edges[1:, :] = _runs(np.abs(np.diff(rotated, axis=0)) > 0.4, max(9, round(width * 0.012)), axis=1)
    row_start = int(round(y0))
    box_rows = slice(max(0, row_start), max(0, row_start) + height)
    reach = max(4, round(width * 0.025))
    thin = max(6, round(width * 0.008))
    strips: dict[str, dict[str, str]] = {}
    touch: dict[str, float] = {}
    for side in ("left", "right"):
        if side == "left":
            columns_band = slice(left, left + band)
            outward = ink[box_rows, left : left + band]
        else:
            columns_band = slice(max(0, right - band + 1), right + 1)
            outward = ink[box_rows, max(0, right - band + 1) : right + 1][:, ::-1]
        lines_profile = edges[box_rows, columns_band].mean(axis=1)
        # Per row: how far in from the side the ink starts and how long it runs from there.
        count = outward.shape[1]
        first = np.where(outward.any(axis=1), outward.argmax(axis=1), count)
        run = np.zeros(outward.shape[0], dtype=np.int32)
        going = np.ones(outward.shape[0], dtype=bool)
        for column in range(count):
            started = first <= column
            going &= ~started | outward[:, column]
            run += started & going
        touching = first < reach
        # A border line along the side: thin ink at the side with paper inside it, row after
        # row for a long stretch. Art cut off by the scan edge is not thin.
        thin_rows = touching & (run <= thin) & (first + run + 3 <= count)
        stretch = max(30, round(height * 0.05))
        border = _runs(thin_rows[:, None], stretch, axis=0)[:, 0]
        # The whole line with its ends (where a panel's top or bottom border meets it).
        grow = round(stretch * 0.35)
        border = np.convolve(border.astype(np.float64), np.ones(2 * grow + 1), mode="same") > 0
        open_rows = touching & ~border
        strips[side] = {"lines": _encode(lines_profile), "open": _encode(open_rows.astype(np.float32))}
        touch[side] = round(float(open_rows.mean()), 4)
    result["strips"] = strips
    result["touch"] = touch
    # The grey along each side of the content (painted behind a straightened edge, where it
    # leaves a sliver at the gutter).
    greys: dict[str, int] = {}
    for side, columns_edge in (
        ("left", slice(left, left + 3)),
        ("right", slice(max(0, right - 2), right + 1)),
    ):
        value = float(np.median(rotated[box_rows, columns_edge]))
        greys[side] = int(round(min(255.0, max(0.0, paper - value * (paper - ink_level)))))
    result["edges"] = greys
    # And along each side of the page as scanned (its own margin there).
    sides: dict[str, int] = {}
    for side, columns_side in (("left", slice(0, 3)), ("right", slice(width - 3, width))):
        value = float(np.median(dark[:, columns_side]))
        sides[side] = int(round(min(255.0, max(0.0, paper - value * (paper - ink_level)))))
    result["sides"] = sides
    return result


# --------------------------------------------------------------------------- transforms
def identity() -> dict[str, float]:
    return {"angle": 0.0, "scale": 1.0, "dx": 0.0, "dy": 0.0}


def is_identity(transform: dict[str, Any] | None) -> bool:
    if not transform:
        return True
    return (
        abs(float(transform.get("angle", 0))) < 1e-4
        and abs(float(transform.get("scale", 1)) - 1) < 1e-5
        and abs(float(transform.get("dx", 0))) < 1e-5
        and abs(float(transform.get("dy", 0))) < 1e-5
    )


def single_transform(page: dict[str, Any] | None) -> dict[str, Any] | None:
    """A page shown alone: straightened only (None: left as it is)."""
    angle = (page or {}).get("angle")
    if angle is None or abs(float(angle)) < MIN_ANGLE:
        return None
    return {"angle": round(-float(angle), 3), "scale": 1.0, "dx": 0.0, "dy": 0.0}


def _ncc(a: Any, b: Any) -> float:
    a = a - a.mean()
    b = b - b.mean()
    norm = math.sqrt(float(np.dot(a, a)) * float(np.dot(b, b)))
    return float(np.dot(a, b)) / norm if norm > 1e-9 else 0.0


def _smooth(values: Any, sigma: float) -> Any:
    radius = int(3 * sigma) + 1
    kernel = np.exp(-0.5 * (np.arange(-radius, radius + 1) / sigma) ** 2)
    return np.convolve(values, kernel / kernel.sum(), mode="same")


def _resample(values: Any, count: int) -> Any:
    source = (np.arange(len(values)) + 0.5) / len(values)
    return np.interp((np.arange(count) + 0.5) / count, source, values)


def _matcher(left: dict[str, Any], right: dict[str, Any], rows: int) -> Any:
    """evaluate(scale, shift) -> (score, overlap) at `rows` samples per side (see _strip_match)."""
    left_lines, right_lines = _resample(left["lines"], rows), _resample(right["lines"], rows)
    left_open, right_open = _resample(left["open"], rows) > 0.5, _resample(right["open"], rows)
    positions = (np.arange(rows) + 0.5) / rows
    # Where the art crosses (either side, a little wider so the lines that end it count).
    grow = np.ones(2 * max(2, round(rows * 0.03)) + 1)
    left_region = np.convolve(left_open.astype(np.float64), grow, mode="same") > 0
    right_region = (np.convolve((right_open > 0.5).astype(np.float64), grow, mode="same") > 0).astype(
        np.float64
    )

    def evaluate(relative: float, offset: float) -> tuple[float, float]:
        mapped = relative * (positions - 0.5) + 0.5 + offset
        inside = (mapped >= 0) & (mapped <= 1)
        if inside.mean() < 0.8:
            return -1.0, 0.0
        region = (left_region | (np.interp(mapped, positions, right_region) > 0.5)) & inside
        if region.sum() < rows * 0.05:
            return -1.0, 0.0
        both = left_open & (np.interp(mapped, positions, right_open) > 0.5) & inside
        value = _ncc(left_lines[region], np.interp(mapped[region], positions, right_lines))
        # A slight preference for no change at all.
        return value - abs(relative - 1) - 2 * abs(offset), float(both.mean())

    return evaluate


def _strip_match(
    left: dict[str, str], right: dict[str, str], *, scale: float, shift: float, window: tuple[float, float]
) -> tuple[float, float, float, float] | None:
    """How the art meets at the gutter: the relative scale and shift (a point at height y of the
    left page is at scale * (y - 0.5) + 0.5 + shift on the right page, fractions of the height)
    that put the lines and edges reaching the two inner sides at the same heights, searched
    `window` (scale, shift) around (`scale`, `shift`).

    (scale, shift, score, overlap) or None when there is nothing to match: the score is the
    correlation of the two sides' line profiles where the art crosses, the overlap the part of
    the height where content runs over both sides without a border line."""
    sides = []
    for strip in (left, right):
        lines, opened = _decode(strip["lines"]), _decode(strip["open"])
        if len(lines) < 50 or len(opened) != len(lines):
            return None
        sides.append({"lines": _smooth(lines, 1.5), "open": opened})
    rows = max(len(sides[0]["lines"]), len(sides[1]["lines"]))
    # Coarse, at half the rows: every scale and shift of the window. The best shift must stand
    # out from shifts further away (a line that happens to sit at a similar height elsewhere is
    # not enough) unless it hardly moves anything, and must not lie on the window's edge (the
    # true one may be beyond it).
    coarse = _matcher(sides[0], sides[1], max(50, rows // 2))
    scale_span, shift_span = window
    scales = np.arange(scale - scale_span, scale + scale_span + 1e-9, 0.01)
    shifts = np.arange(shift - shift_span, shift + shift_span + 1e-9, 2.0 / rows)
    table = np.full((len(scales), len(shifts)), -1.0)
    for i, relative in enumerate(scales):
        for j, offset in enumerate(shifts):
            table[i, j] = coarse(float(relative), float(offset))[0]
    i, j = np.unravel_index(int(np.argmax(table)), table.shape)
    if table[i, j] <= 0 or j in (0, len(shifts) - 1) or (len(scales) > 1 and i in (0, len(scales) - 1)):
        return None
    by_shift = table.max(axis=0)
    far = np.abs(shifts - shifts[j]) > 0.01
    second = float(by_shift[far].max()) if far.any() else -1.0
    small = abs(float(shifts[j]) - shift) <= 0.005 and abs(float(scales[i]) - scale) <= 0.0125
    if float(table[i, j]) - second < 0.1 and not small:
        return None
    # Fine, at every row: around the best.
    fine = _matcher(sides[0], sides[1], rows)
    best: tuple[float, float, float, float] | None = None
    for relative in np.arange(scales[i] - 0.01, scales[i] + 0.01 + 1e-9, 0.0025):
        for offset in np.arange(shifts[j] - 3.0 / rows, shifts[j] + 3.0 / rows + 1e-9, 0.25 / rows):
            value, overlap = fine(float(relative), float(offset))
            if best is None or value > best[2]:
                best = (float(relative), float(offset), value, overlap)
    return best


def pair_transforms(
    first: dict[str, Any] | None, second: dict[str, Any] | None, *, right_to_left: bool
) -> list[dict[str, Any] | None]:
    """The transforms of two facing pages (in page order; None: left as it is).

    `right_to_left` (manga): the first page is on the right, the second on the left."""
    first = first or {}
    second = second or {}
    screen_left, screen_right = (second, first) if right_to_left else (first, second)
    pages = [screen_left, screen_right]
    if np is None or any(page.get("version") != VERSION or "margins" not in page for page in pages):
        return [single_transform(first), single_transform(second)]
    angles = [float(page["angle"]) if page.get("angle") is not None else None for page in pages]
    aspects = [float(page["width"]) / float(page["height"]) for page in pages]
    # Vertical: the frames' heights and positions (both pages' frames clear).
    scales = [1.0, 1.0]
    shifts = [0.0, 0.0]
    relative_scale, relative_shift = 1.0, 0.0
    frames = [page.get("frame") or {} for page in pages]
    known = False
    if all("top" in frame and "bottom" in frame for frame in frames):
        heights = [frame["bottom"] - frame["top"] for frame in frames]
        if min(heights) > 0.5:
            relative_scale = heights[1] / heights[0]
            centres = [(frame["top"] + frame["bottom"]) / 2 for frame in frames]
            # A left page's point at y lands at relative*(y-0.5)+0.5+shift on the right page.
            relative_shift = centres[1] - (relative_scale * (centres[0] - 0.5) + 0.5)
            known = True
    elif all("top" in frame for frame in frames):
        relative_shift = frames[1]["top"] - frames[0]["top"]
        known = True
    elif all("bottom" in frame for frame in frames):
        relative_shift = frames[1]["bottom"] - frames[0]["bottom"]
        known = True
    if known and (abs(relative_scale - 1) > 2 * MAX_SCALE or abs(relative_shift) > 2 * MAX_SHIFT):
        relative_scale, relative_shift, known = 1.0, 0.0, False
    # Art across the gutter: content runs over both inner sides (no border line along them).
    inner = ["right", "left"]  # the screen-left page's right side meets the right page's left side
    touch = [float((page.get("touch") or {}).get(side, 0)) for page, side in zip(pages, inner)]
    crossing = False
    if min(touch) >= 0.08 and all(side in (page.get("strips") or {}) for page, side in zip(pages, inner)):
        window = (0.01, 0.01) if known else (2 * MAX_SCALE, 2 * MAX_SHIFT)
        match = _strip_match(
            pages[0]["strips"][inner[0]],
            pages[1]["strips"][inner[1]],
            scale=relative_scale,
            shift=relative_shift,
            window=window,
        )
        if match is not None and match[2] >= 0.5 and match[3] >= 0.05:
            relative_scale, relative_shift = match[0], match[1]
            crossing = known = True
    if known:
        # Split the difference: each page goes half way (the larger one shrinks, the other grows).
        root = math.sqrt(relative_scale)
        scales = [root, 1 / root]
        # left: y -> root*(y-.5)+.5+e0 ; right: y' -> (1/root)*(y'-.5)+.5+e1, with
        # right(left_point) == left(left_point): e0 = e1 + relative_shift / root.
        half = relative_shift / root / 2
        shifts = [half, -half]
    # Horizontal: the blank margin at the gutter goes away when the art crosses it.
    margins = [float(page["margins"][side]) for page, side in zip(pages, inner)]
    trims = [min(MAX_TRIM, max(0.0, value)) if crossing else 0.0 for value in margins]
    # A straightened page whose content reaches past its inner edge moves back so none is cut.
    for index in range(2):
        if margins[index] < 0:
            trims[index] = margins[index]
    transforms: list[dict[str, Any]] = []
    for index, page in enumerate(pages):
        tilt = angles[index]
        angle = -tilt if tilt is not None and abs(tilt) >= MIN_ANGLE else 0.0
        scale = scales[index]
        # The content edge at the gutter is at (1 - trim) of the width (left page) or at trim
        # (right page); scaled about the centre and moved, it lands on the gutter line.
        if index == 0:
            dx = 1 - (0.5 + scale * (0.5 - trims[0]))
        else:
            dx = -(0.5 + scale * (trims[1] - 0.5))
        transforms.append({"angle": round(angle, 3), "scale": scale, "dx": dx, "dy": shifts[index]})
    transforms = _keep_inside(transforms, aspects)
    out: list[dict[str, Any] | None] = []
    for index, transform in enumerate(transforms):
        transform = {key: round(float(value), 5) for key, value in transform.items()}
        if is_identity(transform):
            out.append(None)
            continue
        transform["gutter"] = inner[index]
        transform["backing"] = [round(value, 5) for value in backing(transform, aspects[index], inner[index])]
        # The colour of what ends up at the gutter: the art there when its margin went, else
        # the page's own edge.
        colours = pages[index].get("edges" if trims[index] > 0 else "sides") or {}
        edge = max(0, min(255, int(colours.get(inner[index], 255))))
        transform["fill"] = f"#{edge:02x}{edge:02x}{edge:02x}"
        out.append(transform)
    return [out[1], out[0]] if right_to_left else out


def _keep_inside(transforms: list[dict[str, Any]], aspects: list[float]) -> list[dict[str, Any]]:
    """Both pages a little smaller together (around the gutter) when a shift takes one past the
    top or bottom of where the pages are drawn (no scroll bar, nothing out of sight)."""
    over = 0.0
    for transform in transforms:
        scale, dy = float(transform["scale"]), float(transform["dy"])
        over = max(over, -(0.5 + dy - scale / 2), (0.5 + dy + scale / 2) - 1)
    if over <= 1e-6:
        return transforms
    factor = 1 / (1 + 2 * over)
    out = []
    for index, transform in enumerate(transforms):
        aspect = aspects[index]
        # Centre of the page in pair coordinates (height 1, the gutter at x = 0).
        centre_x = -aspect / 2 if index == 0 else aspect / 2
        moved_x = centre_x + float(transform["dx"]) * aspect
        moved_y = 0.5 + float(transform["dy"])
        new_x = factor * moved_x
        new_y = factor * (moved_y - 0.5) + 0.5
        out.append(
            {
                "angle": transform["angle"],
                "scale": float(transform["scale"]) * factor,
                "dx": (new_x - centre_x) / aspect,
                "dy": new_y - 0.5,
            }
        )
    return out


# ------------------------------------------------------------------------------- geometry
def apply(transform: dict[str, Any] | None, x: float, y: float, aspect: float) -> tuple[float, float]:
    """Where a point of the page (fractions of its width and height) is drawn, in the same
    fractions of the page's box. `aspect` is width / height of the box."""
    if not transform:
        return x, y
    angle = math.radians(float(transform.get("angle", 0)))
    scale = float(transform.get("scale", 1))
    # In height units, so the rotation is a true rotation of the picture.
    px, py = (x - 0.5) * aspect, y - 0.5
    cos, sin = math.cos(angle), math.sin(angle)
    rx, ry = scale * (px * cos - py * sin), scale * (px * sin + py * cos)
    return 0.5 + rx / aspect + float(transform.get("dx", 0)), 0.5 + ry + float(transform.get("dy", 0))


def invert(transform: dict[str, Any] | None, x: float, y: float, aspect: float) -> tuple[float, float]:
    """The page point drawn at (x, y) of the box (`apply` backwards: taps and OCR boxes)."""
    if not transform:
        return x, y
    angle = math.radians(float(transform.get("angle", 0)))
    scale = float(transform.get("scale", 1)) or 1.0
    px = (x - 0.5 - float(transform.get("dx", 0))) * aspect
    py = y - 0.5 - float(transform.get("dy", 0))
    cos, sin = math.cos(angle), math.sin(angle)
    rx, ry = (px * cos + py * sin) / scale, (-px * sin + py * cos) / scale
    return 0.5 + rx / aspect, 0.5 + ry


def backing(transform: dict[str, Any], aspect: float, gutter: str) -> tuple[float, float, float, float]:
    """The band along the gutter a straightened page may leave uncovered, in fractions of its
    box: from the gutter line to the farthest its inner edge comes away from it, as high as the
    page's inner edge reaches."""
    edge_x = 1.0 if gutter == "right" else 0.0
    top = apply(transform, edge_x, 0.0, aspect)
    bottom = apply(transform, edge_x, 1.0, aspect)
    if gutter == "right":
        reach = max(0.0, 1.0 - min(top[0], bottom[0]))
        x0, x1 = 1.0 - reach, 1.0
    else:
        reach = max(0.0, max(top[0], bottom[0]))
        x0, x1 = 0.0, reach
    return x0, min(top[1], bottom[1]), x1, max(top[1], bottom[1])
