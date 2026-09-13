from __future__ import annotations

import io
import hashlib
import importlib.metadata
import difflib
from functools import lru_cache
import re
import statistics
import unicodedata
import json
import math
import sys
import zipfile
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps


def _number(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def _rectangle_vertical_candidate(region: dict[str, object]) -> bool:
    """Return True for the thin VNDetectTextRectangles slices seen on vertical manga.

    Apple Vision's geometry-only request can report a horizontal slice through a
    vertical text column.  MangaOCR then receives a crop only a few glyphs high,
    which truncates the dialogue and makes the selectable overlay land in the
    wrong place.  The heuristic is intentionally restricted to low-confidence
    rectangle-only detections; normal recognised captions keep their geometry.
    """

    detector = str(region.get("detector") or "")
    if "vision-rectangles" not in detector:
        return False
    confidence = _number(region.get("confidence"), 0.0)
    raw_text = str(region.get("raw_text") or "").strip()
    width = max(0.0, _number(region.get("width")))
    height = max(0.0, _number(region.get("height")))
    if width <= 0.012 or height <= 0.004 or width > 0.30 or height > 0.075:
        return False
    if confidence > 0.36 and raw_text:
        return False
    return width >= height * 1.45


def _should_expand_rectangle_regions(regions: list[dict[str, object]]) -> bool:
    if len(regions) < 2:
        return False
    candidates = sum(_rectangle_vertical_candidate(region) for region in regions)
    # Require multiple matching slices and a meaningful share of the page so a
    # single horizontal SFX/caption is never turned into a tall manga column.
    return candidates >= 2 and candidates >= max(2, math.ceil(len(regions) * 0.34))


def _expand_vertical_rectangle(region: dict[str, object]) -> dict[str, object]:
    item = dict(region)
    x = max(0.0, min(1.0, _number(region.get("x"))))
    y = max(0.0, min(1.0, _number(region.get("y"))))
    width = max(0.0, min(1.0 - x, _number(region.get("width"))))
    height = max(0.0, min(1.0 - y, _number(region.get("height"))))
    original = {
        "x": round(x, 6),
        "y": round(y, 6),
        "width": round(width, 6),
        "height": round(height, 6),
    }

    center_y = y + height / 2.0
    target_height = min(0.28, max(0.13, height * 5.5, width * 1.55))
    target_height = min(1.0, target_height)
    new_y = max(0.0, min(1.0 - target_height, center_y - target_height / 2.0))

    # Vision rectangle x bounds are usually much better than y bounds.  Add a
    # small side margin only; large frontend padding is deliberately avoided.
    side = max(0.004, min(0.014, width * 0.08))
    new_x = max(0.0, x - side)
    new_width = min(1.0 - new_x, width + side * 2.0)

    item.update(
        {
            "x": round(new_x, 6),
            "y": round(new_y, 6),
            "width": round(new_width, 6),
            "height": round(target_height, 6),
            "orientation": "vertical",
            "orientation_reason": "expanded-thin-vision-rectangle",
            "source": "expanded-vision-rectangle",
            "detector_geometry": original,
        }
    )
    # Existing segments describe the stale thin rectangle.  Keeping them would
    # make frontend token hit-testing use coordinates that no longer match the
    # crop.  Fresh character-box geometry is preserved because it does not enter
    # this expansion path in the first place.
    item.pop("segments", None)
    return item



def _coherent_horizontal_multiline_exact_observation(region: dict[str, object]) -> bool:
    """Return True for a compact observed multi-row horizontal caption.

    Character/name cards often have two or three short horizontal rows inside
    one Vision observation.  A low-confidence rectangle can otherwise look
    "vertical enough" to the generic clipped-seed recovery and lose its exact
    row geometry.  Require exact glyph boxes, whitespace-separated detector
    rows, and row-by-row textual agreement before protecting the observation.
    """
    if str(region.get("orientation") or "") != "horizontal":
        return False
    exact = [
        dict(segment)
        for segment in region.get("segments") or []
        if isinstance(segment, dict)
        and str(segment.get("source") or "").startswith("vision-accurate-range")
        and float(segment.get("width") or 0.0) > 0
        and float(segment.get("height") or 0.0) > 0
        and str(segment.get("text") or "").strip()
    ]
    if len(exact) < 5:
        return False

    detector_rows = [
        _normalize_line_surface(part)
        for part in re.split(r"\s+", str(region.get("raw_text") or region.get("text") or "").strip())
        if _normalize_line_surface(part)
    ]
    if not (2 <= len(detector_rows) <= 4):
        return False

    groups: list[list[dict[str, object]]] = []
    for segment in sorted(exact, key=_segment_center_y, reverse=True):
        center = _segment_center_y(segment)
        if groups:
            group_height = statistics.median(
                float(item.get("height") or 0.0) for item in groups[-1]
            )
            current_height = float(segment.get("height") or 0.0)
            tolerance = max(min(group_height, current_height) * 0.80, 0.008)
            if abs(_segment_center_y(groups[-1][0]) - center) <= tolerance:
                groups[-1].append(segment)
                continue
        groups.append([segment])
    if len(groups) != len(detector_rows):
        return False

    similarities: list[float] = []
    for detector_text, group in zip(detector_rows, groups):
        surface = "".join(
            _normalize_line_surface(item.get("text"))
            for item in sorted(group, key=lambda value: float(value.get("x") or 0.0))
        )
        if not surface:
            return False
        similarities.append(
            difflib.SequenceMatcher(a=surface, b=detector_text, autojunk=False).ratio()
        )
    return min(similarities) >= 0.55 and statistics.mean(similarities) >= 0.70


def _vertical_seed_candidate(region: dict[str, object]) -> bool:
    """Return True for a small Japanese fragment likely clipped from vertical manga text.

    The backend used to mark the supplied One Piece ``ざい`` fragment horizontal
    because Vision's merged box was wider than tall; the vertical correction only
    happened *after* MangaOCR had already been skipped. Rectangle-backed,
    low-confidence Japanese fragments are therefore accepted as vertical seeds
    even when the provisional orientation says horizontal.
    """
    if str(region.get("source") or "") in {"expanded-vision-rectangle", "dark-block-proposal"}:
        return False
    if _coherent_horizontal_multiline_exact_observation(region):
        return False
    text = "".join(ch for ch in str(region.get("raw_text") or region.get("text") or "") if not ch.isspace())
    japanese = sum("\u3040" <= ch <= "\u30ff" or "\u3400" <= ch <= "\u9fff" or ch in "々〆ヶ" for ch in text)
    if japanese < 1 or len(text) > 12:
        return False
    orientation = str(region.get("orientation") or "")
    detector = str(region.get("detector") or "")
    confidence = _number(region.get("confidence"), 0.0)
    japanese_ratio = japanese / max(1, len(text))
    rectangle_seed = (
        "vision-rectangles" in detector
        and confidence <= 0.60
        and japanese_ratio >= 0.50
        # A single very shallow rectangle such as a horizontal caption must
        # not be promoted to vertical merely because it contains Japanese.
        # The failing One Piece seed (ざい) is wide but still has meaningful
        # vertical extent (h/w ~= .52), while the horizontal regression is .25.
        and _number(region.get("height"), 0.0)
        >= _number(region.get("width"), 0.0) * 0.35
    )
    if orientation != "vertical" and not rectangle_seed:
        return False
    x = max(0.0, min(1.0, _number(region.get("x"))))
    y = max(0.0, min(1.0, _number(region.get("y"))))
    width = max(0.0, min(1.0 - x, _number(region.get("width"))))
    height = max(0.0, min(1.0 - y, _number(region.get("height"))))
    # Chapter/title bars at the very top are usually intentional short regions.
    if y + height >= 0.90:
        return False
    if width <= 0.008 or height <= 0.008 or width > 0.24 or height > 0.24:
        return False
    return width * height <= 0.032 and (width <= 0.20 or height <= 0.20)

def _expand_vertical_seed(region: dict[str, object]) -> dict[str, object]:
    item = dict(region)
    x = max(0.0, min(1.0, _number(region.get("x"))))
    y = max(0.0, min(1.0, _number(region.get("y"))))
    width = max(0.0, min(1.0 - x, _number(region.get("width"))))
    height = max(0.0, min(1.0 - y, _number(region.get("height"))))
    original = {
        "x": round(x, 6),
        "y": round(y, 6),
        "width": round(width, 6),
        "height": round(height, 6),
    }
    center_x = x + width / 2.0
    center_y = y + height / 2.0
    target_width = min(0.30, max(0.14, width * 1.65))
    target_height = min(0.30, max(0.15, height * 1.90))
    new_x = max(0.0, min(1.0 - target_width, center_x - target_width / 2.0))
    new_y = max(0.0, min(1.0 - target_height, center_y - target_height / 2.0))
    item.update({
        "x": round(new_x, 6),
        "y": round(new_y, 6),
        "width": round(target_width, 6),
        "height": round(target_height, 6),
        "orientation": "vertical",
        "orientation_reason": "expanded-short-japanese-seed",
        "source": "expanded-vertical-seed",
        "detector_geometry": original,
    })
    item.pop("segments", None)
    return item


def _region_area(region: dict[str, object]) -> float:
    return max(0.0, _number(region.get("width"))) * max(0.0, _number(region.get("height")))


def _region_coverage(container: dict[str, object], inner: dict[str, object]) -> float:
    """Fraction of ``inner`` covered by ``container`` in Vision coordinates."""
    ax1, ay1 = _number(container.get("x")), _number(container.get("y"))
    ax2 = ax1 + max(0.0, _number(container.get("width")))
    ay2 = ay1 + max(0.0, _number(container.get("height")))
    bx1, by1 = _number(inner.get("x")), _number(inner.get("y"))
    bx2 = bx1 + max(0.0, _number(inner.get("width")))
    by2 = by1 + max(0.0, _number(inner.get("height")))
    intersection = max(0.0, min(ax2, bx2) - max(ax1, bx1)) * max(0.0, min(ay2, by2) - max(ay1, by1))
    return intersection / max(_region_area(inner), 1e-9)


def _dark_text_block_regions(image: Image.Image) -> list[dict[str, object]]:
    """Propose solid dark narration boxes that Apple Vision commonly misses.

    This deliberately looks for *large dark connected areas*, not arbitrary
    glyphs. On the supplied page it finds the two black narration boxes at the
    top, ``世は``, and the lower-left narration panel while ignoring Roger's
    hair/clothes because those components are not rectangular/dense enough.
    """
    gray = image.convert("L")
    width, height = gray.size
    if width < 32 or height < 32:
        gray.close()
        return []
    target_width = 96
    target_height = max(48, min(220, round(height * target_width / max(1, width))))
    small = gray.resize((target_width, target_height), Image.Resampling.BOX)
    gray.close()
    try:
        pixels = list(small.getdata())
    finally:
        small.close()
    w, h = target_width, target_height
    dark = [int(value) <= 72 for value in pixels]
    seen = bytearray(w * h)
    proposals: list[dict[str, object]] = []
    for start in range(w * h):
        if not dark[start] or seen[start]:
            continue
        stack = [start]
        seen[start] = 1
        xs: list[int] = []
        ys: list[int] = []
        while stack:
            index = stack.pop()
            yy, xx = divmod(index, w)
            xs.append(xx)
            ys.append(yy)
            for nx, ny in ((xx - 1, yy), (xx + 1, yy), (xx, yy - 1), (xx, yy + 1)):
                if not (0 <= nx < w and 0 <= ny < h):
                    continue
                nxt = ny * w + nx
                if dark[nxt] and not seen[nxt]:
                    seen[nxt] = 1
                    stack.append(nxt)
        if len(xs) < 28:
            continue
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        box_w, box_h = x1 - x0 + 1, y1 - y0 + 1
        if box_w < 6 or box_h < 6:
            continue
        fill = len(xs) / max(1, box_w * box_h)
        norm_w = box_w / w
        norm_h = box_h / h
        area = norm_w * norm_h
        if fill < 0.68 or area < 0.006 or area > 0.13:
            continue
        if norm_w > 0.36 or norm_h > 0.36:
            continue
        # One coarse-cell margin catches white edge glyphs without swallowing a
        # neighbouring bubble.
        x0 = max(0, x0 - 1)
        x1 = min(w - 1, x1 + 1)
        y0 = max(0, y0 - 1)
        y1 = min(h - 1, y1 + 1)
        left = x0 / w
        right = (x1 + 1) / w
        top = y0 / h
        bottom = (y1 + 1) / h
        proposal = {
            "text": "",
            "raw_text": "",
            "orientation": "vertical",
            "orientation_reason": "dark-narration-block",
            "x": round(left, 6),
            "y": round(max(0.0, 1.0 - bottom), 6),
            "width": round(right - left, 6),
            "height": round(bottom - top, 6),
            "confidence": round(min(0.75, 0.35 + fill * 0.35), 4),
            "detector": "pil-dark-block-v1",
            "source": "dark-block-proposal",
            "dark_fill": round(fill, 4),
        }
        proposals.append(proposal)
    proposals.sort(key=lambda row: (-_number(row.get("y")), -_number(row.get("x"))))
    return proposals[:12]


def _augment_dark_block_regions(image: Image.Image, regions: list[dict[str, object]]) -> list[dict[str, object]]:
    rows = [dict(region) for region in regions]
    for proposal in _dark_text_block_regions(image):
        # If Vision already owns almost all of this dark box, keep its richer
        # geometry/text. Otherwise replace tiny clipped seeds contained inside
        # the proposal and let MangaOCR recognize the complete black panel.
        if any(_region_coverage(existing, proposal) >= 0.78 for existing in rows):
            continue
        kept: list[dict[str, object]] = []
        for existing in rows:
            covered = _region_coverage(proposal, existing)
            compact = "".join(ch for ch in str(existing.get("text") or "") if not ch.isspace())
            short_seed = len(compact) <= 12 or _number(existing.get("confidence"), 0.0) <= 0.60
            if covered >= 0.72 and _region_area(proposal) >= _region_area(existing) * 1.35 and short_seed:
                continue
            kept.append(existing)
        kept.append(proposal)
        rows = kept
    return rows


def _prepare_regions_for_ocr(
    regions: list[dict[str, object]],
    image: Image.Image | None = None,
) -> list[dict[str, object]]:
    rows = [dict(region) for region in regions]
    if image is not None:
        rows = _augment_dark_block_regions(image, rows)
    rectangle_expansion = _should_expand_rectangle_regions(rows)
    prepared: list[dict[str, object]] = []
    for region in rows:
        if rectangle_expansion and _rectangle_vertical_candidate(region):
            prepared.append(_expand_vertical_rectangle(region))
        elif _vertical_seed_candidate(region):
            prepared.append(_expand_vertical_seed(region))
        else:
            prepared.append(dict(region))
    return prepared




# pudge-manga-recovery-phase2.2-line-promotion-v1
_LAYOUT_LINE_SOURCE = "manga-layout-line-v1"
_LAYOUT_DETECTOR = "manga-ink-components-v1"
_RAW_LAYOUT_DETECTOR = "manga-raw-components-v1"
_CONTEXT_LAYOUT_DETECTOR = "manga-context-gap-components-v1"
_LAYOUT_CLUSTER_DETECTOR = "manga-layout-cluster-v1"
_LAYOUT_PIPELINE = "manga-layout-dual-pass-v1"
_SYNTHETIC_SEGMENT_SOURCES = {"ink-grid-v1", "ink-columns-v2", "dark-columns-v1", "layout-line-proportional-v1"}
_PIPELINE_GENERATION = "pudge-manga-regions-v59-layout-token-geometry"
_PIPELINE_WORKER = "pudge-manga-recovery-phase3.4-first20-recall-v59"
# pudge-manga-recovery-phase2.7-dark-column-dedupe-v23
# pudge-manga-recovery-phase2.8-dark-suffix-v24
# pudge-manga-recovery-phase2.9-ruby-echo-suppression-v25
_PIPELINE_FRONTEND = "pudge-manga-recovery-hitbox-contract-v1"
_PIPELINE_ARTIFACT_SCHEMA = "pudge-manga-ocr-v3"
_PIPELINE_NORMALIZATION = "surface-offsets-v1"
_OCR_CROP_POLICY = "vertical-failure-retry-pad-v2"


def _pipeline_fingerprint(image: Image.Image, model: object) -> dict[str, object]:
    try:
        model_version = importlib.metadata.version("manga-ocr")
    except importlib.metadata.PackageNotFoundError:
        model_version = "unknown"
    digest = hashlib.sha256()
    digest.update(f"{image.mode}:{image.size[0]}x{image.size[1]}\n".encode("ascii"))
    digest.update(image.tobytes())
    return {
        "generation": _PIPELINE_GENERATION,
        "worker": _PIPELINE_WORKER,
        "frontend": _PIPELINE_FRONTEND,
        "artifact_schema": _PIPELINE_ARTIFACT_SCHEMA,
        "detector": _LAYOUT_PIPELINE,
        "recognizer": f"{type(model).__module__}.{type(model).__qualname__}",
        "recognizer_version": model_version,
        "normalization": _PIPELINE_NORMALIZATION,
        "layout_config": "dual-pass+context-gap+ruby-reject+semantic-noise+layout-token-geometry+dark-column-consensus-v3+dark-column-local-rule-v1+dark-column-glyph-center-v1+dark-column-leading-edge-refine-v1+horizontal-wide-segment-recovery-v1+small-kana-consensus-v1+layout-line-ink-v2+full-region-line-relabel-v1+vertical-leading-ink-v1+title-detector-consensus-v1+short-kanji-pair-v1+vertical-slot-ink-tighten-v1+vertical-x-context-v1+horizontal-narrow-expand-v1+weak-synthetic-coverage-v1+chapter-prefix-components-v2+leading-frame-reject-v1+strong-raw-recall-v1+single-kanji-layout-v1+synthetic-texture-reject-v1+pre-split-kanji-pair-v1+vertical-edge-context-v1+layout-cluster-context-v1+latin-detector-preserve-v1+kanji-pair-context-v2+layout-cluster-height-split-v2+layout-xy-retry-v1+horizontal-pair-lower-band-v1+cluster-square-pad-v1+cluster-member-consensus-v2+adjacent-short-raw-v1+adaptive-horizontal-line-groups-v1+latin-wide-skip-v1+layout-square-retry-v1+layout-geometry-guard-v1+cluster-member-geometry-v1+ruby-latin-letter-v2+kanji-baseline-mask-v1+wide-vertical-layout-donor-v1+cluster-dynamic-split-v1+weak-square-retry-guard-v1+cluster-consensus-only-v1+raw-gap-cluster-v1+raw-wide-donor-v1+ruby-component-crop-v1+repair-attempt-diagnostics-v1+vertical-detector-surface-v1+kanji-pair-evidence-v1+cluster-member-ensemble-v1+postmerge-wide-split-v1+ruby-isolated-components-v1+leading-prefix-center-support-v1+small-standalone-baseline-v1+partial-weak-raw-component-support-v3+leading-prefix-width-guard-v1+tall-merged-direct-square-consensus-v1+bold-adjacent-raw-line-v1+adjacent-tall-prefix-trim-v1+p09-raw-cluster-consensus-v1+bounded-local-ensemble-v1+caption-whole-region-consensus-v1+caption-suffix-anchor-cleanup-v1+dark-column-small-kana-polarity-consensus-v1+clipped-horizontal-sfx-right-context-v1+clipped-horizontal-sfx-exact-crop-v1+short-latin-case-consensus-v1+chapter-quote-segment-geometry-v1+chapter-quote-existing-text-geometry-v1+horizontal-trailing-punctuation-ink-v1+nfkc-segment-surface-relabel-v1+short-fullwidth-digit-ink-geometry-v1+short-raw-empty-rectangle-art-reject-v1+short-raw-empty-rectangle-art-output-guard-v1+short-raw-empty-rectangle-service-finalize-v1+raw-verified-segment-relabel-v1+repeated-kana-ink-split-v1",
        "ocr_crop_policy": _OCR_CROP_POLICY,
        "image_sha256": digest.hexdigest(),
        "image_size": [int(image.size[0]), int(image.size[1])],
    }


def _binary_components(mask: Image.Image) -> list[tuple[int, int, int, int, int]]:
    """Connected components for an 8-connected L-mode binary image.

    This intentionally avoids OpenCV/scipy so foreground manga recovery does not
    add another heavyweight runtime dependency.  Rows are represented as runs and
    unioned with overlapping runs from the previous row.
    """
    width, height = mask.size
    if width <= 0 or height <= 0:
        return []
    data = mask.tobytes()
    parents: list[int] = []
    ranks: list[int] = []
    runs: list[tuple[int, int, int, int]] = []

    def new_label() -> int:
        label = len(parents)
        parents.append(label)
        ranks.append(0)
        return label

    def find(label: int) -> int:
        while parents[label] != label:
            parents[label] = parents[parents[label]]
            label = parents[label]
        return label

    def union(left: int, right: int) -> int:
        left = find(left)
        right = find(right)
        if left == right:
            return left
        if ranks[left] < ranks[right]:
            left, right = right, left
        parents[right] = left
        if ranks[left] == ranks[right]:
            ranks[left] += 1
        return left

    previous: list[tuple[int, int, int]] = []
    for row in range(height):
        offset = row * width
        current: list[tuple[int, int, int]] = []
        column = 0
        previous_index = 0
        while column < width:
            while column < width and data[offset + column] == 0:
                column += 1
            if column >= width:
                break
            start = column
            while column + 1 < width and data[offset + column + 1] != 0:
                column += 1
            end = column
            column += 1
            while previous_index < len(previous) and previous[previous_index][1] < start - 1:
                previous_index += 1
            matches: list[int] = []
            scan = previous_index
            while scan < len(previous) and previous[scan][0] <= end + 1:
                matches.append(previous[scan][2])
                scan += 1
            label = matches[0] if matches else new_label()
            for other in matches[1:]:
                label = union(label, other)
            current.append((start, end, label))
            runs.append((row, start, end, label))
        previous = current

    aggregates: dict[int, list[int]] = {}
    for row, start, end, label in runs:
        root = find(label)
        count = end - start + 1
        aggregate = aggregates.get(root)
        if aggregate is None:
            aggregates[root] = [start, row, end, row, count]
        else:
            aggregate[0] = min(aggregate[0], start)
            aggregate[1] = min(aggregate[1], row)
            aggregate[2] = max(aggregate[2], end)
            aggregate[3] = max(aggregate[3], row)
            aggregate[4] += count
    return [
        (left, top, right - left + 1, bottom - top + 1, count)
        for left, top, right, bottom, count in aggregates.values()
    ]


def _layout_component_candidates(image: Image.Image) -> list[dict[str, float]]:
    """Find glyph-like dark components without inferring characters from OCR text."""
    gray = image.convert("L")
    try:
        width, height = gray.size
        # Keep glyph resolution stable on large scans while avoiding a quadratic
        # detector cost.  The five recovery pages are 760px wide and stay native.
        scale = min(1.0, 760.0 / max(1, width))
        if scale < 0.999:
            scan = gray.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.BOX)
        else:
            scan = gray.copy()
    finally:
        gray.close()
    try:
        # Black text and its antialiased edge are enough for layout; screen-tone
        # dots mostly remain tiny disconnected components and are filtered below.
        binary = scan.point(lambda value: 255 if value < 125 else 0)
        try:
            dilated = binary.filter(ImageFilter.MaxFilter(3))
        finally:
            binary.close()
        try:
            components = _binary_components(dilated)
        finally:
            dilated.close()
    finally:
        scan.close()

    candidates: list[dict[str, float]] = []
    inverse = 1.0 / max(scale, 1e-9)
    for raw_x, raw_y, raw_width, raw_height, area in components:
        box_area = raw_width * raw_height
        density = area / max(1, box_area)
        normal_glyph = (
            4 <= raw_width <= 55
            and 4 <= raw_height <= 150
            and box_area <= 4500
        )
        tall_text_run = (
            12 <= raw_width <= 45
            and 150 < raw_height <= 320
            and box_area <= 10000
            and density < 0.90
        )
        if not (normal_glyph or tall_text_run) or area < 18:
            continue
        if density < 0.11 or density > 0.94:
            continue
        if normal_glyph and not (
            (5 <= raw_width <= 45 and 5 <= raw_height <= 90)
            or (raw_height >= 45 and raw_width <= 35)
        ):
            continue
        candidates.append(
            {
                "x": raw_x * inverse,
                "y": raw_y * inverse,
                "width": raw_width * inverse,
                "height": raw_height * inverse,
                "cx": (raw_x + raw_width / 2.0) * inverse,
                "density": density,
            }
        )
    return candidates


def _layout_vertical_lines(image: Image.Image) -> list[dict[str, object]]:
    """Cluster observed ink components into real vertical text-line boxes."""
    page_width, page_height = image.size
    if page_width <= 0 or page_height <= 0:
        return []
    gray = image.convert("L")
    components = sorted(_layout_component_candidates(image), key=lambda item: float(item["cx"]))
    x_tolerance = max(6.0, page_width * 0.0112)  # ~=8.5px on 760px recovery pages.
    x_groups: list[dict[str, object]] = []
    for component in components:
        best: dict[str, object] | None = None
        best_distance = float("inf")
        for group in x_groups[-6:]:
            distance = abs(float(component["cx"]) - float(group["mean_x"]))
            if distance <= x_tolerance and distance < best_distance:
                best = group
                best_distance = distance
        if best is None:
            best = {"items": [], "mean_x": float(component["cx"])}
            x_groups.append(best)
        items = best["items"]
        assert isinstance(items, list)
        items.append(component)
        best["mean_x"] = sum(float(item["cx"]) for item in items) / len(items)

    lines: list[dict[str, object]] = []
    max_gap = max(18.0, page_width * 0.0316)  # ~=24px on 760px.
    for group in x_groups:
        raw_items = group.get("items")
        if not isinstance(raw_items, list):
            continue
        items = sorted(raw_items, key=lambda item: float(item["y"]))
        run: list[dict[str, float]] = []

        def emit(active: list[dict[str, float]]) -> None:
            if not active:
                return
            left = min(float(item["x"]) for item in active)
            top = min(float(item["y"]) for item in active)
            right = max(float(item["x"]) + float(item["width"]) for item in active)
            bottom = max(float(item["y"]) + float(item["height"]) for item in active)
            width = right - left
            height = bottom - top
            if height < max(30.0, page_height * 0.025) or width > page_width * 0.077:
                return
            if height < width * 1.30:
                return
            # Short 1-2 component runs are overwhelmingly panel art / speed lines.
            # One exception is a *merged* vertical text blob: dilation can join a
            # short 4-5 glyph manga column (e.g. 邪魔するぞ) or a large single
            # kanji with ruby (e.g. 銃) into one tall component. Keep only that
            # strict shape and let MangaOCR/short-text validation decide later.
            single_merged_component = False
            if len(active) < 3 and height / max(float(page_height), 1.0) < 0.10:
                if 1 <= len(active) <= 2:
                    component_densities = [float(component.get("density") or 0.0) for component in active]
                    ink_area = sum(
                        float(component.get("width") or 0.0)
                        * float(component.get("height") or 0.0)
                        * float(component.get("density") or 0.0)
                        for component in active
                    )
                    box_density = ink_area / max(1.0, width * height)
                    single_merged_component = (
                        height >= max(48.0, width * 1.40)
                        and width <= page_width * 0.060
                        and all(0.10 <= density <= 0.90 for density in component_densities)
                        and box_density >= 0.10
                    )
                if not single_merged_component:
                    return
            center_spread = max(float(item["cx"]) for item in active) - min(float(item["cx"]) for item in active)
            if center_spread > max(12.0, page_width * 0.016):
                return
            coverage = sum(float(item["height"]) for item in active) / max(height, 1.0)
            if coverage < 0.22:
                return
            score = len(active) + min(5.0, height / 30.0) + min(1.5, coverage)
            # A couple of pixels of image-derived padding prevent clipped strokes;
            # _crop_region adds its own OCR-only margin later.
            pad_x = max(1.0, page_width * 0.0015)
            pad_y = max(1.0, page_height * 0.0010)
            left2 = max(0.0, left - pad_x)
            top2 = max(0.0, top - pad_y)
            right2 = min(float(page_width), right + pad_x)
            bottom2 = min(float(page_height), bottom + pad_y)
            lane = gray.crop((int(left2), int(top2), max(int(left2) + 1, int(right2)), max(int(top2) + 1, int(bottom2))))
            histogram = lane.histogram()
            pixels = max(1, sum(histogram))
            black_ratio = sum(histogram[:100]) / pixels
            white_ratio = sum(histogram[220:]) / pixels
            midtone_ratio = max(0.0, 1.0 - black_ratio - white_ratio)
            # Clouds, speed lines and screentone can form vertical component runs.
            # Real black-on-white manga glyph lanes have enough solid ink and
            # little midtone texture even before recognition.
            if black_ratio < 0.055 or midtone_ratio > 0.22:
                return
            if single_merged_component and (black_ratio > 0.32 or midtone_ratio > 0.17):
                return
            norm_x = left2 / page_width
            norm_width = (right2 - left2) / page_width
            norm_height = (bottom2 - top2) / page_height
            norm_y = 1.0 - bottom2 / page_height
            lines.append(
                {
                    "text": "",
                    "raw_text": "",
                    "orientation": "vertical",
                    "orientation_reason": "observed-ink-components",
                    "x": round(norm_x, 6),
                    "y": round(norm_y, 6),
                    "width": round(norm_width, 6),
                    "height": round(norm_height, 6),
                    "confidence": round(max(0.45, min(0.92, 0.40 + score * 0.045)), 4),
                    "detector": _LAYOUT_DETECTOR,
                    "source": _LAYOUT_LINE_SOURCE,
                    "geometry_source": _LAYOUT_DETECTOR,
                    "geometry_status": "observed",
                    "provenance": {
                        "proposal_kind": "vertical_text_line",
                        "component_count": len(active),
                        "component_coverage": round(coverage, 4),
                        "layout_score": round(score, 4),
                        "black_ratio": round(black_ratio, 4),
                        "white_ratio": round(white_ratio, 4),
                        "midtone_ratio": round(midtone_ratio, 4),
                        "single_merged_component": bool(single_merged_component),
                        "detector_bbox_px": [
                            round(left2, 2), round(top2, 2), round(right2, 2), round(bottom2, 2)
                        ],
                    },
                }
            )

        for component in items:
            if run:
                previous = run[-1]
                gap = float(component["y"]) - (float(previous["y"]) + float(previous["height"]))
                if gap > max_gap:
                    emit(run)
                    run = []
            run.append(component)
        emit(run)

    # Dedupe nearly-identical lanes without merging neighbouring manga columns.
    lines.sort(
        key=lambda item: (
            -float((item.get("provenance") or {}).get("layout_score") or 0.0),
            -float(item.get("height") or 0.0),
        )
    )
    kept: list[dict[str, object]] = []
    for line in lines:
        cx = _number(line.get("x")) + _number(line.get("width")) / 2.0
        y1 = _number(line.get("y"))
        y2 = y1 + _number(line.get("height"))
        duplicate = False
        for existing in kept:
            ecx = _number(existing.get("x")) + _number(existing.get("width")) / 2.0
            ey1 = _number(existing.get("y"))
            ey2 = ey1 + _number(existing.get("height"))
            overlap = max(0.0, min(y2, ey2) - max(y1, ey1))
            shorter = min(_number(line.get("height")), _number(existing.get("height")))
            if abs(cx - ecx) <= 6.0 / page_width and overlap >= shorter * 0.45:
                duplicate = True
                break
        if not duplicate:
            kept.append(line)
    # MangaOCR hallucinates on non-text crops. Bound extra model calls and let
    # recognition validation below discard residual art candidates.
    return kept[:36]



def _raw_layout_component_candidates(image: Image.Image) -> list[dict[str, float]]:
    """Glyph-like components from the original threshold image, without dilation.

    The primary Phase2 detector intentionally dilates strokes to survive broken
    glyphs.  On a few manga bubbles that merges text into panel borders/art.  This
    second pass keeps the original connected components and is only allowed to
    contribute when spatial evidence below corroborates the line.
    """
    gray = image.convert("L")
    try:
        width, height = gray.size
        scale = min(1.0, 760.0 / max(1, width))
        if scale < 0.999:
            scan = gray.resize(
                (max(1, round(width * scale)), max(1, round(height * scale))),
                Image.Resampling.BOX,
            )
        else:
            scan = gray.copy()
    finally:
        gray.close()
    try:
        binary = scan.point(lambda value: 255 if value < 125 else 0)
        try:
            components = _binary_components(binary)
        finally:
            binary.close()
    finally:
        scan.close()

    inverse = 1.0 / max(scale, 1e-9)
    output: list[dict[str, float]] = []
    for raw_x, raw_y, raw_width, raw_height, area in components:
        box_area = raw_width * raw_height
        density = area / max(1, box_area)
        if area < 14 or density < 0.10 or density > 0.92:
            continue
        if not (3 <= raw_width <= 38 and 4 <= raw_height <= 48 and box_area <= 1400):
            continue
        if raw_width < 5 and raw_height < 8:
            continue
        output.append(
            {
                "x": raw_x * inverse,
                "y": raw_y * inverse,
                "width": raw_width * inverse,
                "height": raw_height * inverse,
                "cx": (raw_x + raw_width / 2.0) * inverse,
                "density": density,
            }
        )
    return output


def _raw_vertical_lines(image: Image.Image) -> list[dict[str, object]]:
    """Strict undilated vertical line hypotheses; not accepted without support."""
    page_width, page_height = image.size
    if page_width <= 0 or page_height <= 0:
        return []
    components = sorted(_raw_layout_component_candidates(image), key=lambda item: float(item["cx"]))
    x_tolerance = max(6.0, page_width * 0.0112)
    groups: list[dict[str, object]] = []
    for component in components:
        best: dict[str, object] | None = None
        best_distance = float("inf")
        for group in groups[-10:]:
            distance = abs(float(component["cx"]) - float(group["mean_x"]))
            if distance <= x_tolerance and distance < best_distance:
                best = group
                best_distance = distance
        if best is None:
            best = {"items": [], "mean_x": float(component["cx"])}
            groups.append(best)
        items = best["items"]
        assert isinstance(items, list)
        items.append(component)
        best["mean_x"] = sum(float(item["cx"]) for item in items) / len(items)

    gray = image.convert("L")
    lines: list[dict[str, object]] = []
    max_gap = max(12.0, page_width * 0.025)
    try:
        for group in groups:
            raw_items = group.get("items")
            if not isinstance(raw_items, list):
                continue
            items = sorted(raw_items, key=lambda item: float(item["y"]))
            run: list[dict[str, float]] = []

            def emit(active: list[dict[str, float]]) -> None:
                if len(active) < 2:
                    return
                left = min(float(item["x"]) for item in active)
                top = min(float(item["y"]) for item in active)
                right = max(float(item["x"]) + float(item["width"]) for item in active)
                bottom = max(float(item["y"]) + float(item["height"]) for item in active)
                width = right - left
                height = bottom - top
                if height < 24.0 or width > 45.0 or height < width * 1.05:
                    return
                spread = max(float(item["cx"]) for item in active) - min(float(item["cx"]) for item in active)
                if spread > 14.0:
                    return
                coverage = sum(float(item["height"]) for item in active) / max(height, 1.0)
                if coverage < 0.55:
                    return
                lane = gray.crop((int(left), int(top), max(int(left) + 1, int(right + 1)), max(int(top) + 1, int(bottom + 1))))
                try:
                    histogram = lane.histogram()
                finally:
                    lane.close()
                pixels = max(1, sum(histogram))
                black_ratio = sum(histogram[:100]) / pixels
                white_ratio = sum(histogram[220:]) / pixels
                midtone_ratio = max(0.0, 1.0 - black_ratio - white_ratio)
                if black_ratio < 0.07 or midtone_ratio > 0.18:
                    return
                pad_x = max(1.0, page_width * 0.0012)
                pad_y = max(1.0, page_height * 0.0008)
                left2 = max(0.0, left - pad_x)
                top2 = max(0.0, top - pad_y)
                right2 = min(float(page_width), right + pad_x)
                bottom2 = min(float(page_height), bottom + pad_y)
                score = len(active) + min(6.0, height / 35.0) + min(1.5, coverage)
                lines.append(
                    {
                        "text": "",
                        "raw_text": "",
                        "orientation": "vertical",
                        "orientation_reason": "observed-undilated-ink-components",
                        "x": round(left2 / page_width, 6),
                        "y": round(1.0 - bottom2 / page_height, 6),
                        "width": round((right2 - left2) / page_width, 6),
                        "height": round((bottom2 - top2) / page_height, 6),
                        "confidence": round(max(0.42, min(0.90, 0.38 + score * 0.045)), 4),
                        "detector": _RAW_LAYOUT_DETECTOR,
                        "source": _LAYOUT_LINE_SOURCE,
                        "geometry_source": _RAW_LAYOUT_DETECTOR,
                        "geometry_status": "observed",
                        "provenance": {
                            "proposal_kind": "vertical_text_line_raw",
                            "component_count": len(active),
                            "component_coverage": round(coverage, 4),
                            "layout_score": round(score, 4),
                            "black_ratio": round(black_ratio, 4),
                            "white_ratio": round(white_ratio, 4),
                            "midtone_ratio": round(midtone_ratio, 4),
                            "detector_bbox_px": [
                                round(left2, 2), round(top2, 2), round(right2, 2), round(bottom2, 2)
                            ],
                        },
                    }
                )

            for component in items:
                if run:
                    previous = run[-1]
                    gap = float(component["y"]) - (float(previous["y"]) + float(previous["height"]))
                    if gap > max_gap:
                        emit(run)
                        run = []
                run.append(component)
            emit(run)
    finally:
        gray.close()

    lines.sort(
        key=lambda item: (
            -float((item.get("provenance") or {}).get("layout_score") or 0.0),
            -float(item.get("height") or 0.0),
        )
    )
    kept: list[dict[str, object]] = []
    for line in lines:
        cx = _number(line.get("x")) + _number(line.get("width")) / 2.0
        y1 = _number(line.get("y"))
        y2 = y1 + _number(line.get("height"))
        duplicate = False
        for existing in kept:
            ecx = _number(existing.get("x")) + _number(existing.get("width")) / 2.0
            ey1 = _number(existing.get("y"))
            ey2 = ey1 + _number(existing.get("height"))
            overlap = max(0.0, min(y2, ey2) - max(y1, ey1))
            shorter = min(_number(line.get("height")), _number(existing.get("height")))
            if abs(cx - ecx) <= 7.0 / page_width and overlap >= shorter * 0.50:
                duplicate = True
                break
        if not duplicate:
            kept.append(line)
    return kept[:48]


def _pixel_bbox(region: dict[str, object], page_width: int, page_height: int) -> tuple[float, float, float, float]:
    x = max(0.0, min(1.0, _number(region.get("x"))))
    y = max(0.0, min(1.0, _number(region.get("y"))))
    width = max(0.0, min(1.0 - x, _number(region.get("width"))))
    height = max(0.0, min(1.0 - y, _number(region.get("height"))))
    return (
        x * page_width,
        (1.0 - y - height) * page_height,
        (x + width) * page_width,
        (1.0 - y) * page_height,
    )


def _pixel_cover(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> float:
    intersection = max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0, min(left[3], right[3]) - max(left[1], right[1])
    )
    left_area = max(1.0, (left[2] - left[0]) * (left[3] - left[1]))
    right_area = max(1.0, (right[2] - right[0]) * (right[3] - right[1]))
    return max(intersection / left_area, intersection / right_area)


def _pixel_vertical_overlap(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> float:
    overlap = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    return overlap / max(1.0, min(left[3] - left[1], right[3] - right[1]))


def _raw_layout_support(
    candidate: dict[str, object],
    regions: list[dict[str, object]],
    primary: list[dict[str, object]],
    page_width: int,
    page_height: int,
) -> dict[str, object] | None:
    box = _pixel_bbox(candidate, page_width, page_height)
    center = (box[0] + box[2]) / 2.0
    if center < 20.0 or center > page_width - 20.0:
        return None
    provenance = candidate.get("provenance")
    component_count = int((provenance or {}).get("component_count") or 0) if isinstance(provenance, dict) else 0
    component_coverage = float((provenance or {}).get("component_coverage") or 0.0) if isinstance(provenance, dict) else 0.0
    candidate_black_ratio = float((provenance or {}).get("black_ratio") or 0.0) if isinstance(provenance, dict) else 0.0
    candidate_white_ratio = float((provenance or {}).get("white_ratio") or 0.0) if isinstance(provenance, dict) else 0.0
    candidate_midtone_ratio = float((provenance or {}).get("midtone_ratio") or 1.0) if isinstance(provenance, dict) else 1.0
    candidate_height = box[3] - box[1]
    candidate_width = box[2] - box[0]

    # First, don't re-OCR the same lane merely because the raw bbox is tighter.
    # v43 exception: if dilation collapses a lane to <=2 blobs while the
    # independent undilated pass sees a near-identical stack of >=10 components,
    # prefer the raw geometry. This is deliberately much stricter than ordinary
    # same-lane corroboration and does not alter the primary proposal itself.
    primary_boxes = [_pixel_bbox(item, page_width, page_height) for item in primary]
    for primary_item, primary_box in zip(primary, primary_boxes):
        primary_center = (primary_box[0] + primary_box[2]) / 2.0
        duplicate_cover = _pixel_cover(box, primary_box) >= 0.58
        duplicate_track = (
            abs(center - primary_center) <= 10.0
            and _pixel_vertical_overlap(box, primary_box) >= 0.60
        )
        if not (duplicate_cover or duplicate_track):
            continue
        primary_provenance = primary_item.get("provenance")
        primary_count = (
            int(primary_provenance.get("component_count") or 0)
            if isinstance(primary_provenance, dict)
            else 0
        )
        primary_coverage = (
            float(primary_provenance.get("component_coverage") or 0.0)
            if isinstance(primary_provenance, dict)
            else 0.0
        )
        if (
            component_count >= 10
            and component_coverage >= 1.00
            and primary_count <= 2
            and primary_coverage >= 0.90
            and abs(center - primary_center) <= 4.0
            and _pixel_vertical_overlap(box, primary_box) >= 0.82
            and _region_coverage(candidate, primary_item) >= 0.82
            and _region_coverage(primary_item, candidate) >= 0.82
        ):
            return {
                "support_kind": "same-lane-raw-count-dominant-v1",
                "support_component_count": component_count,
                "support_component_coverage": round(component_coverage, 4),
                "support_primary_component_count": primary_count,
                "support_primary_component_coverage": round(primary_coverage, 4),
            }

        # A one-component primary fragment can be only the middle/tail of a much
        # taller raw lane. Let the candidate continue to the independent
        # adjacent-primary support check below; without that second peer it will
        # still be rejected.
        clipped_high_count_candidate = (
            primary_count <= 1
            and component_count >= 8
            and component_coverage >= 0.90
            and candidate_height >= max(96.0, candidate_width * 3.20)
            and candidate_height >= (primary_box[3] - primary_box[1]) * 2.20
            and candidate_width <= page_width * 0.050
            and 0.36 <= candidate_black_ratio <= 0.42
            and candidate_white_ratio >= 0.45
            and candidate_midtone_ratio <= 0.14
        )
        if clipped_high_count_candidate:
            continue
        return None

    # Strongest evidence: a weak/collapsed Vision region crosses this observed raw
    # line. Geometry comes from pixels; detector text is only retained as provenance.
    best_weak: tuple[float, dict[str, object]] | None = None
    for region in regions:
        if _region_geometry_trust(region) != "weak":
            continue
        overlap = _pixel_cover(box, _pixel_bbox(region, page_width, page_height))
        threshold = 0.45 if component_count < 3 else 0.15
        if overlap >= threshold and (best_weak is None or overlap > best_weak[0]):
            best_weak = (overlap, region)
    if best_weak is not None:
        overlap, region = best_weak
        return {
            "support_kind": "weak-region",
            "support_overlap": round(overlap, 4),
            "support_order": int(_number(region.get("order"), -1)),
            "support_text": str(region.get("text") or region.get("raw_text") or "").strip(),
        }

    # A very thin accurate Vision observation can itself be the wrong orientation
    # (case05 酒酒). Use it only as corroboration for a tall pixel-derived lane.
    best_tiny: tuple[float, dict[str, object]] | None = None
    for region in regions:
        region_box = _pixel_bbox(region, page_width, page_height)
        region_height = region_box[3] - region_box[1]
        if region_height > 28.0 or _japanese_character_count(region.get("text")) < 2:
            continue
        overlap = _pixel_cover(box, region_box)
        if overlap >= 0.28 and (best_tiny is None or overlap > best_tiny[0]):
            best_tiny = (overlap, region)
    if best_tiny is not None:
        overlap, region = best_tiny
        return {
            "support_kind": "tiny-geometry-corroboration",
            "support_overlap": round(overlap, 4),
            "support_order": int(_number(region.get("order"), -1)),
            "support_text": str(region.get("text") or region.get("raw_text") or "").strip(),
        }

    # v43: a very tall, high-component raw lane can sit just above the
    # global black-density ceiling. Evaluate this before spacing heuristics so
    # its stronger independent evidence is preserved in provenance. The global
    # standalone ceiling itself remains unchanged.
    if isinstance(provenance, dict):
        coverage = float(provenance.get("component_coverage") or 0.0)
        black_ratio = float(provenance.get("black_ratio") or 0.0)
        white_ratio = float(provenance.get("white_ratio") or 0.0)
        midtone_ratio = float(provenance.get("midtone_ratio") or 1.0)
        height = box[3] - box[1]
        width = box[2] - box[0]
        high_count_adjacent_shape = (
            component_count >= 8
            and coverage >= 0.90
            and height >= max(96.0, width * 3.20)
            and width <= page_width * 0.050
            and 0.36 <= black_ratio <= 0.42
            and white_ratio >= 0.45
            and midtone_ratio <= 0.14
        )
        if high_count_adjacent_shape:
            nearest_primary: tuple[float, float, int, float] | None = None
            for primary_item, primary_box in zip(primary, primary_boxes):
                primary_provenance = primary_item.get("provenance")
                if not isinstance(primary_provenance, dict):
                    continue
                primary_count = int(primary_provenance.get("component_count") or 0)
                primary_coverage = float(primary_provenance.get("component_coverage") or 0.0)
                if primary_count < 4 or primary_coverage < 0.85:
                    continue
                primary_center = (primary_box[0] + primary_box[2]) / 2.0
                distance = abs(center - primary_center)
                if not (24.0 <= distance <= 58.0):
                    continue
                vertical_overlap = _pixel_vertical_overlap(box, primary_box)
                if vertical_overlap < 0.60:
                    continue
                score = (distance, -vertical_overlap)
                if nearest_primary is None or score < (nearest_primary[0], -nearest_primary[1]):
                    nearest_primary = (distance, vertical_overlap, primary_count, primary_coverage)
            if nearest_primary is not None:
                return {
                    "support_kind": "high-count-adjacent-primary-raw-v1",
                    "support_component_count": component_count,
                    "support_component_coverage": round(coverage, 4),
                    "support_spacing_px": round(nearest_primary[0], 2),
                    "support_vertical_overlap": round(nearest_primary[1], 4),
                    "support_primary_component_count": nearest_primary[2],
                    "support_primary_component_coverage": round(nearest_primary[3], 4),
                    "support_black_ratio": round(black_ratio, 4),
                }

    # Missing columns also show up as a clean hole between already-observed lanes,
    # or one line beyond a stable pair. This proposes where to inspect; the final
    # bbox still comes entirely from raw connected pixels.
    peers: list[tuple[float, tuple[float, float, float, float]]] = []
    for primary_box in primary_boxes:
        if _pixel_vertical_overlap(box, primary_box) >= 0.45:
            peers.append(((primary_box[0] + primary_box[2]) / 2.0, primary_box))
    peers.sort(key=lambda item: item[0])
    left = [item for item in peers if item[0] < center - 10.0]
    right = [item for item in peers if item[0] > center + 10.0]
    if left and right:
        left_center = left[-1][0]
        right_center = right[0][0]
        gap = right_center - left_center
        if 22.0 <= gap <= 100.0 and min(center - left_center, right_center - center) >= 10.0:
            return {
                "support_kind": "observed-gap",
                "support_centers_px": [round(left_center, 2), round(center, 2), round(right_center, 2)],
            }
    if 20.0 <= center <= page_width - 20.0:
        for first, second in zip(peers, peers[1:]):
            spacing = second[0] - first[0]
            if not 20.0 <= spacing <= 45.0:
                continue
            if abs(center - (first[0] - spacing)) <= 10.0 or abs(center - (second[0] + spacing)) <= 10.0:
                return {
                    "support_kind": "observed-spacing-extrapolation",
                    "support_centers_px": [round(first[0], 2), round(second[0], 2), round(center, 2)],
                }

    # Recovery v12: a clean raw connected-component lane can stand on its own.
    # The previous code required Vision/peer corroboration for every raw lane,
    # which systematically dropped isolated speech bubbles such as 邪魔するぞ
    # and the two top-page columns on p018. Keep this deliberately strict;
    # recognition validation still rejects non-Japanese/art hallucinations.
    if isinstance(provenance, dict):
        component_count = int(provenance.get("component_count") or 0)
        coverage = float(provenance.get("component_coverage") or 0.0)
        black_ratio = float(provenance.get("black_ratio") or 0.0)
        midtone_ratio = float(provenance.get("midtone_ratio") or 1.0)
        height = box[3] - box[1]
        width = box[2] - box[0]
        strong_four_plus = (
            component_count >= 4
            and coverage >= 0.72
            and height >= max(48.0, width * 2.0)
            and width <= page_width * 0.052
            and 0.075 <= black_ratio <= 0.38
            and midtone_ratio <= 0.19
        )
        strong_three = (
            component_count == 3
            and coverage >= 0.85
            and height >= max(42.0, width * 2.20)
            and width <= page_width * 0.045
            and 0.09 <= black_ratio <= 0.32
            and midtone_ratio <= 0.16
        )
        if strong_four_plus or strong_three:
            return {
                "support_kind": "strong-raw-line",
                "support_component_count": component_count,
                "support_component_coverage": round(coverage, 4),
            }

        # v41: bold raw companion beside a tall dense primary lane.
        #
        # Bold manga glyphs can make an undilated raw lane much darker than the
        # global standalone ceiling (0.38). Do not raise that ceiling globally:
        # artwork/SFX frequently occupies the same density range. Admit the raw
        # lane only when an independently detected tall dense primary speech lane
        # sits one normal column pitch away and overlaps it strongly in Y.
        white_ratio = float(provenance.get("white_ratio") or 0.0)
        bold_raw_shape = (
            component_count >= 4
            and coverage >= 0.85
            and height >= max(72.0, width * 2.20)
            and height <= 180.0
            and width <= page_width * 0.055
            and 0.38 < black_ratio <= 0.50
            and white_ratio >= 0.40
            and midtone_ratio <= 0.14
        )
        if bold_raw_shape:
            nearest_dense: tuple[float, float] | None = None
            for primary_item, primary_box in zip(primary, primary_boxes):
                if not _tall_dense_merged_layout_candidate(primary_item):
                    continue
                primary_center = (primary_box[0] + primary_box[2]) / 2.0
                distance = abs(center - primary_center)
                if not (28.0 <= distance <= 52.0):
                    continue
                vertical_overlap = _pixel_vertical_overlap(box, primary_box)
                if vertical_overlap < 0.70:
                    continue
                if nearest_dense is None or distance < nearest_dense[0]:
                    nearest_dense = (distance, vertical_overlap)
            if nearest_dense is not None:
                return {
                    "support_kind": "bold-adjacent-raw-line-v1",
                    "support_component_count": component_count,
                    "support_component_coverage": round(coverage, 4),
                    "support_spacing_px": round(nearest_dense[0], 2),
                    "support_vertical_overlap": round(nearest_dense[1], 4),
                    "support_black_ratio": round(black_ratio, 4),
                }

        # v15: very short 2-glyph companion columns (e.g. ぜェ beside 邪魔する)
        # are legitimate dialogue but too short for the global standalone rule.
        # Accept them only when a strong primary lane sits one normal column pitch
        # away and overlaps vertically; furigana normally sits much closer (<18 px).
        if component_count == 2:
            height = box[3] - box[1]
            width = box[2] - box[0]
            nearest: tuple[float, tuple[float, float, float, float]] | None = None
            for primary_box in primary_boxes:
                primary_center = (primary_box[0] + primary_box[2]) / 2.0
                distance = abs(center - primary_center)
                if not (18.0 <= distance <= 46.0):
                    continue
                if _pixel_vertical_overlap(box, primary_box) < 0.45:
                    continue
                if nearest is None or distance < nearest[0]:
                    nearest = (distance, primary_box)
            if (
                nearest is not None
                and coverage >= 0.80
                and height >= max(24.0, width * 1.05)
                and height <= 62.0
                and width <= page_width * 0.045
                and 0.10 <= black_ratio <= 0.35
                and midtone_ratio <= 0.18
            ):
                return {
                    "support_kind": "adjacent-short-raw-line",
                    "support_component_count": component_count,
                    "support_component_coverage": round(coverage, 4),
                    "support_spacing_px": round(nearest[0], 2),
                }
    return None


def _supplemental_raw_layout_lines(
    image: Image.Image,
    regions: list[dict[str, object]],
    primary: list[dict[str, object]],
) -> list[dict[str, object]]:
    page_width, page_height = image.size
    output: list[dict[str, object]] = []
    for candidate in _raw_vertical_lines(image):
        support = _raw_layout_support(candidate, regions, primary, page_width, page_height)
        if support is None:
            continue
        item = dict(candidate)
        provenance = dict(item.get("provenance") or {})
        provenance.update(support)
        item["provenance"] = provenance
        output.append(item)
    output.sort(
        key=lambda item: (
            -float((item.get("provenance") or {}).get("support_overlap") or 0.0),
            -float((item.get("provenance") or {}).get("layout_score") or 0.0),
        )
    )
    return output[:12]



def _contextual_missing_layout_lines(
    image: Image.Image,
    regions: list[dict[str, object]],
    peers: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Recover short columns only where existing manga lanes predict a missing slot.

    This is intentionally contextual.  Globally accepting 1-2 connected blobs
    recreates the old artwork hallucinations; inside an expanded Vision text
    rectangle, however, two neighbouring observed lanes can reveal a clean
    missing middle lane or one extrapolated edge lane.
    """
    page_width, page_height = image.size
    if page_width <= 0 or page_height <= 0:
        return []
    components = _layout_component_candidates(image)
    peer_boxes = [(peer, _pixel_bbox(peer, page_width, page_height)) for peer in peers]
    proposals: list[dict[str, object]] = []

    def covered_by_peer(box: tuple[float, float, float, float], local_boxes: list[tuple[float, float, float, float]]) -> bool:
        return any(_pixel_cover(box, peer_box) >= 0.65 for peer_box in local_boxes)

    for region in regions:
        if str(region.get("source") or "") not in {"expanded-vision-rectangle", "expanded-vertical-seed"}:
            continue
        if str(region.get("orientation") or "") != "vertical":
            continue
        region_box = _pixel_bbox(region, page_width, page_height)
        local: list[tuple[float, tuple[float, float, float, float]]] = []
        for _peer, peer_box in peer_boxes:
            center = (peer_box[0] + peer_box[2]) / 2.0
            if not (region_box[0] - 5.0 <= center <= region_box[2] + 5.0):
                continue
            if _pixel_vertical_overlap(peer_box, region_box) < 0.40:
                continue
            if _pixel_cover(peer_box, region_box) < 0.45:
                continue
            local.append((center, peer_box))
        if len(local) < 2:
            continue
        local.sort(key=lambda item: item[0])
        centers = [center for center, _box in local]
        local_boxes = [box for _center, box in local]

        expected: list[tuple[float, str, float]] = []
        gaps = [right - left for left, right in zip(centers, centers[1:])]
        stable = [gap for gap in gaps if 20.0 <= gap <= 45.0]
        spacing = float(statistics.median(stable)) if stable else 0.0

        # One clean hole between observed neighbours.
        for left_center, right_center in zip(centers, centers[1:]):
            gap = right_center - left_center
            if 46.0 <= gap <= 95.0:
                expected.append(((left_center + right_center) / 2.0, "observed-gap", gap / 2.0))

        # One missing edge column beyond a stable pair.
        if spacing:
            for center, support_kind in (
                (centers[0] - spacing, "observed-spacing-extrapolation"),
                (centers[-1] + spacing, "observed-spacing-extrapolation"),
            ):
                if region_box[0] + 8.0 <= center <= region_box[2] - 8.0:
                    expected.append((center, support_kind, spacing))

        for expected_center, support_kind, support_spacing in expected:
            tolerance = max(9.0, min(15.0, support_spacing * 0.38))
            candidates: list[dict[str, float]] = []
            for component in components:
                cx = float(component["cx"])
                top = float(component["y"])
                bottom = top + float(component["height"])
                if abs(cx - expected_center) > tolerance:
                    continue
                if bottom < region_box[1] + 3.0 or top > region_box[3] - 3.0:
                    continue
                component_box = (
                    float(component["x"]),
                    top,
                    float(component["x"]) + float(component["width"]),
                    bottom,
                )
                if covered_by_peer(component_box, local_boxes):
                    continue
                candidates.append(component)
            if not candidates:
                continue

            candidates.sort(key=lambda item: float(item["y"]))
            clusters: list[list[dict[str, float]]] = []
            run: list[dict[str, float]] = []
            for component in candidates:
                if run:
                    previous = run[-1]
                    gap = float(component["y"]) - (
                        float(previous["y"]) + float(previous["height"])
                    )
                    if gap > 30.0:
                        clusters.append(run)
                        run = []
                run.append(component)
            if run:
                clusters.append(run)
            if not clusters:
                continue
            active = max(
                clusters,
                key=lambda rows: sum(float(item["width"]) * float(item["height"]) for item in rows),
            )
            left = min(float(item["x"]) for item in active)
            top = min(float(item["y"]) for item in active)
            right = max(float(item["x"]) + float(item["width"]) for item in active)
            bottom = max(float(item["y"]) + float(item["height"]) for item in active)
            width = right - left
            height = bottom - top
            if height < 28.0 or width > 50.0 or height < width * 1.15:
                continue

            pad_x = max(1.0, page_width * 0.0012)
            pad_y = max(1.0, page_height * 0.0008)
            left2 = max(region_box[0], left - pad_x)
            top2 = max(region_box[1], top - pad_y)
            right2 = min(region_box[2], right + pad_x)
            bottom2 = min(region_box[3], bottom + pad_y)
            if right2 <= left2 or bottom2 <= top2:
                continue

            lane = image.convert("L").crop(
                (int(left2), int(top2), max(int(left2) + 1, int(right2)), max(int(top2) + 1, int(bottom2)))
            )
            try:
                histogram = lane.histogram()
            finally:
                lane.close()
            pixels = max(1, sum(histogram))
            black_ratio = sum(histogram[:100]) / pixels
            white_ratio = sum(histogram[220:]) / pixels
            midtone_ratio = max(0.0, 1.0 - black_ratio - white_ratio)
            if black_ratio < 0.055 or midtone_ratio > 0.20:
                continue

            proposal = {
                "text": "",
                "raw_text": "",
                "orientation": "vertical",
                "orientation_reason": "contextual-missing-column",
                "x": round(left2 / page_width, 6),
                "y": round(1.0 - bottom2 / page_height, 6),
                "width": round((right2 - left2) / page_width, 6),
                "height": round((bottom2 - top2) / page_height, 6),
                "confidence": 0.62,
                "detector": _CONTEXT_LAYOUT_DETECTOR,
                "source": _LAYOUT_LINE_SOURCE,
                "geometry_source": _CONTEXT_LAYOUT_DETECTOR,
                "geometry_status": "observed",
                "provenance": {
                    "proposal_kind": "contextual_missing_vertical_text_line",
                    "component_count": len(active),
                    "black_ratio": round(black_ratio, 4),
                    "white_ratio": round(white_ratio, 4),
                    "midtone_ratio": round(midtone_ratio, 4),
                    "support_kind": support_kind,
                    "support_expected_center_px": round(expected_center, 2),
                    "support_spacing_px": round(support_spacing, 2),
                    "detector_bbox_px": [
                        round(left2, 2),
                        round(top2, 2),
                        round(right2, 2),
                        round(bottom2, 2),
                    ],
                },
            }
            proposal_box = _pixel_bbox(proposal, page_width, page_height)
            if any(_pixel_cover(proposal_box, _pixel_bbox(existing, page_width, page_height)) >= 0.58 for existing in [*peers, *proposals]):
                continue
            proposals.append(proposal)

    proposals.sort(
        key=lambda item: (
            -float((item.get("provenance") or {}).get("black_ratio") or 0.0),
            -float(item.get("height") or 0.0),
        )
    )
    return proposals[:8]





def _augment_cluster_members_with_raw_gaps(
    image: Image.Image,
    proposals: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Add only raw lanes that fill a geometrically obvious gap in a cluster.

    A raw detector can see a real middle column that the dilated detector merged
    away (p018).  Do not promote raw lines globally here: cluster OCR may use a
    raw lane only when two already accepted neighbours bracket it on the same
    vertical band.  This makes the extra lane geometry useful without reopening
    the old artwork-hallucination path.
    """
    page_width, _page_height = image.size
    if len(proposals) < 2 or page_width <= 0:
        return [dict(item) for item in proposals]
    output = [dict(item) for item in proposals]
    for raw in _raw_vertical_lines(image):
        if str(raw.get("orientation") or "") != "vertical":
            continue
        raw_center = _number(raw.get("x")) + _number(raw.get("width")) / 2.0
        duplicate = False
        for existing in output:
            center = _number(existing.get("x")) + _number(existing.get("width")) / 2.0
            if abs(center - raw_center) <= 8.0 / page_width and _layout_vertical_overlap(existing, raw) >= 0.55:
                duplicate = True
                break
        if duplicate:
            continue
        left: list[tuple[float, dict[str, object]]] = []
        right: list[tuple[float, dict[str, object]]] = []
        for peer in proposals:
            if _layout_vertical_overlap(peer, raw) < 0.45:
                continue
            center = _number(peer.get("x")) + _number(peer.get("width")) / 2.0
            distance_px = abs(center - raw_center) * page_width
            if not 9.0 <= distance_px <= 52.0:
                continue
            if center < raw_center:
                left.append((center, peer))
            elif center > raw_center:
                right.append((center, peer))
        if not left or not right:
            continue
        left_center, _ = max(left, key=lambda item: item[0])
        right_center, _ = min(right, key=lambda item: item[0])
        bracket_px = (right_center - left_center) * page_width
        if not 28.0 <= bracket_px <= 88.0:
            continue
        midpoint = (left_center + right_center) / 2.0
        if abs(raw_center - midpoint) * page_width > max(10.0, bracket_px * 0.28):
            continue
        provenance = dict(raw.get("provenance") or {})
        provenance["support_kind"] = "cluster-observed-gap"
        provenance["support_centers_px"] = [
            round(left_center * page_width, 2),
            round(raw_center * page_width, 2),
            round(right_center * page_width, 2),
        ]
        item = dict(raw)
        item["provenance"] = provenance
        output.append(item)
    return output


def _wide_vertical_geometry_candidates(image: Image.Image) -> list[dict[str, object]]:
    """Return observed lanes for geometry-only splitting of trusted wide OCR.

    Unlike OCR proposals, these candidates never generate text by themselves.
    They are consumed only inside an already-recognized wide Japanese region,
    so raw undilated lanes are safe and valuable even when they lacked enough
    standalone evidence to enter normal OCR recovery.
    """
    primary = [dict(item) for item in _layout_vertical_lines(image)]
    raw = [dict(item) for item in _raw_vertical_lines(image)]
    output: list[dict[str, object]] = []
    for item in [*primary, *raw]:
        center = _number(item.get("x")) + _number(item.get("width")) / 2.0
        duplicate_index: int | None = None
        for index, existing in enumerate(output):
            existing_center = _number(existing.get("x")) + _number(existing.get("width")) / 2.0
            if abs(center - existing_center) <= 0.010 and _layout_vertical_overlap(item, existing) >= 0.55:
                duplicate_index = index
                break
        if duplicate_index is None:
            output.append(item)
            continue
        old = output[duplicate_index]
        old_prov = old.get("provenance") if isinstance(old.get("provenance"), dict) else {}
        new_prov = item.get("provenance") if isinstance(item.get("provenance"), dict) else {}
        old_score = float(old_prov.get("component_coverage") or 0.0) + 0.08 * int(old_prov.get("component_count") or 0)
        new_score = float(new_prov.get("component_coverage") or 0.0) + 0.08 * int(new_prov.get("component_count") or 0)
        if new_score > old_score:
            output[duplicate_index] = item
    return output

def _layout_cluster_proposals(
    image: Image.Image,
    proposals: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Build recognition-only crops from adjacent observed vertical lanes.

    v13 grouped lanes by x order only. If two bubbles shared similar x positions,
    one lane from the lower bubble could split the upper bubble cluster and vice
    versa. v14 uses a geometry graph: an edge exists only when two lanes are
    horizontally adjacent *and* overlap vertically. The member boxes are kept in
    provenance so cluster OCR can later be split back by observed lane heights.
    """
    lanes: list[dict[str, object]] = []
    for original in proposals:
        if str(original.get("source") or "") != _LAYOUT_LINE_SOURCE:
            continue
        if str(original.get("orientation") or "") != "vertical":
            continue
        if _number(original.get("width")) <= 0.006 or _number(original.get("height")) <= 0.025:
            continue
        lane = dict(original)
        # Geometry-only edge recovery is safe here: it follows ink on the same
        # x track and does not depend on the first OCR hypothesis being correct.
        leading = _vertical_leading_ink_geometry(image, lane)
        if leading is not None:
            lane = leading
        trailing = _vertical_trailing_ink_geometry(image, lane)
        if trailing is not None:
            lane = trailing
        lanes.append(lane)

    if len(lanes) < 2:
        return []

    adjacency: list[set[int]] = [set() for _ in lanes]
    for left_index in range(len(lanes)):
        left = lanes[left_index]
        left_center = _number(left.get("x")) + _number(left.get("width")) / 2.0
        for right_index in range(left_index + 1, len(lanes)):
            right = lanes[right_index]
            right_center = _number(right.get("x")) + _number(right.get("width")) / 2.0
            gap = abs(right_center - left_center)
            if not (0.012 <= gap <= 0.066):
                continue
            if _layout_vertical_overlap(left, right) < 0.30:
                continue
            adjacency[left_index].add(right_index)
            adjacency[right_index].add(left_index)

    components: list[list[dict[str, object]]] = []
    seen: set[int] = set()
    for root in range(len(lanes)):
        if root in seen:
            continue
        stack = [root]
        seen.add(root)
        indexes: list[int] = []
        while stack:
            current = stack.pop()
            indexes.append(current)
            for neighbour in adjacency[current]:
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        if len(indexes) >= 2:
            component = [lanes[index] for index in indexes]
            component.sort(key=lambda item: _number(item.get("x")) + _number(item.get("width")) / 2.0)
            components.append(component)

    output: list[dict[str, object]] = []
    for component in components:
        # Connected components are already bubble-local in y. Keep crops small;
        # split unusually large dialogue groups into x-neighbour chunks.
        chunks = [component[index : index + 6] for index in range(0, len(component), 6)]
        for chunk in chunks:
            if len(chunk) < 2:
                continue
            x1 = min(_number(item.get("x")) for item in chunk)
            x2 = max(_number(item.get("x")) + _number(item.get("width")) for item in chunk)
            y1 = min(_number(item.get("y")) for item in chunk)
            y2 = max(_number(item.get("y")) + _number(item.get("height")) for item in chunk)
            span_width = x2 - x1
            span_height = y2 - y1
            if span_width <= 0.015 or span_width > 0.24 or span_height < 0.035:
                continue
            pad_x = min(0.010, max(0.003, span_width * 0.06))
            pad_y = min(0.012, max(0.004, span_height * 0.06))
            member_boxes = []
            for item in chunk:
                member_provenance = item.get("provenance")
                member_boxes.append(
                    {
                        "x": round(_number(item.get("x")), 6),
                        "y": round(_number(item.get("y")), 6),
                        "width": round(_number(item.get("width")), 6),
                        "height": round(_number(item.get("height")), 6),
                        "detector": str(item.get("detector") or ""),
                        "source": str(item.get("source") or ""),
                        "component_count": (
                            int(member_provenance.get("component_count") or 0)
                            if isinstance(member_provenance, dict)
                            else 0
                        ),
                        "component_coverage": (
                            float(member_provenance.get("component_coverage") or 0.0)
                            if isinstance(member_provenance, dict)
                            else 0.0
                        ),
                    }
                )
            output.append(
                {
                    "text": "",
                    "raw_text": "",
                    "orientation": "vertical",
                    "orientation_reason": "adjacent-layout-context-cluster-v2",
                    "x": round(max(0.0, x1 - pad_x), 6),
                    "y": round(max(0.0, y1 - pad_y), 6),
                    "width": round(min(1.0, x2 + pad_x) - max(0.0, x1 - pad_x), 6),
                    "height": round(min(1.0, y2 + pad_y) - max(0.0, y1 - pad_y), 6),
                    "confidence": 0.68,
                    "detector": _LAYOUT_CLUSTER_DETECTOR,
                    "source": "layout-cluster-v2",
                    "geometry_source": _LAYOUT_CLUSTER_DETECTOR,
                    "geometry_status": "approximate",
                    "provenance": {
                        "proposal_kind": "layout_context_cluster_v2",
                        "member_count": len(chunk),
                        "member_boxes": member_boxes,
                    },
                }
            )
    return output[:14]


def _allocate_cluster_characters(
    compact: str,
    members: list[dict[str, object]],
) -> list[str]:
    """Split cluster OCR right-to-left proportional to observed lane heights."""
    if not compact or len(members) < 2 or len(compact) < len(members):
        return []
    ordered = sorted(
        members,
        key=lambda item: _number(item.get("x")) + _number(item.get("width")) / 2.0,
        reverse=True,
    )
    heights = [max(1e-6, _number(member.get("height"))) for member in ordered]
    total_height = sum(heights)
    if total_height <= 0:
        return []
    # One character per lane first, then largest-remainder allocation. This is
    # stable for mixed 3/5/3 vertical bubbles where height tracks glyph count.
    remaining = len(compact) - len(ordered)
    ideals = [height / total_height * remaining for height in heights]
    extras = [int(math.floor(value)) for value in ideals]
    while sum(extras) < remaining:
        index = max(range(len(extras)), key=lambda i: ideals[i] - extras[i])
        extras[index] += 1
    counts = [1 + extra for extra in extras]
    if any(count > 14 for count in counts):
        return []
    chunks: list[str] = []
    cursor = 0
    for count in counts:
        chunks.append(compact[cursor : cursor + count])
        cursor += count
    return chunks if cursor == len(compact) else []


_VERTICAL_BOUNDARY_ENDINGS = (
    "は", "が", "を", "に", "で", "も", "と", "へ", "の", "か", "ね", "よ",
    "ぞ", "ぜ", "さ", "って", "たら", "なら", "から", "まで", "けど", "ので",
)
_VERTICAL_SMALL_KANA = set("っッゃゅょャュョぁぃぅぇぉァィゥェォゎヮ")


def _vertical_chunk_boundary_cost(chunk: str, *, final: bool = False) -> float:
    if not chunk:
        return 9.0
    cost = 0.0
    if len(chunk) == 1 and not final:
        cost += 1.5
    if chunk[-1] in _VERTICAL_SMALL_KANA:
        cost += 2.0
    if not final and any(chunk.endswith(ending) for ending in _VERTICAL_BOUNDARY_ENDINGS):
        cost -= 1.35
    if not final and chunk[-1] in "、。！？!?":
        cost -= 0.7
    return cost


def _layout_member_expected_glyphs(member: dict[str, object]) -> float:
    provenance = member.get("provenance")
    component_count = int(member.get("component_count") or 0)
    if component_count <= 0 and isinstance(provenance, dict):
        component_count = int(provenance.get("component_count") or 0)
    width = max(1e-6, _number(member.get("width")))
    height = max(1e-6, _number(member.get("height")))
    merged = bool(isinstance(provenance, dict) and provenance.get("single_merged_component"))
    if merged or component_count <= 1:
        return max(1.0, min(12.0, height / max(width * 0.72, 1e-6)))
    aspect_estimate = height / max(width * 0.72, 1e-6)
    return max(float(component_count), min(float(component_count + 3), aspect_estimate))


def _split_text_across_layout_members(
    value: object,
    members: list[dict[str, object]],
) -> list[str]:
    """Split right-to-left multi-column OCR using geometry + Japanese boundaries.

    This is used only when the text is already recognized over a wider bubble
    crop.  It never invents characters; it assigns the existing string to
    observed pixel lanes.  Component count is a soft estimate because one kana
    can split into multiple blobs and adjacent glyphs can merge.
    """
    compact = _compact_surface(value)
    if len(members) < 2 or len(members) > 4 or len(compact) < len(members) * 2 or len(compact) > 40:
        return []
    ordered = sorted(
        [dict(member) for member in members],
        key=lambda item: _number(item.get("x")) + _number(item.get("width")) / 2.0,
        reverse=True,
    )
    expected = [_layout_member_expected_glyphs(member) for member in ordered]
    count = len(ordered)
    # DP over exact character boundaries.  Keep at least two chars in a lane
    # unless the geometry itself is clearly one-glyph sized.
    states: dict[tuple[int, int], tuple[float, list[str]]] = {(0, 0): (0.0, [])}
    for lane_index in range(count):
        next_states: dict[tuple[int, int], tuple[float, list[str]]] = {}
        lanes_left = count - lane_index - 1
        for (_old_lane, cursor), (score, chunks) in states.items():
            minimum = 1 if expected[lane_index] < 1.8 else 2
            max_end = len(compact) - lanes_left
            for end in range(cursor + minimum, max_end + 1):
                chunk = compact[cursor:end]
                length_cost = abs(len(chunk) - expected[lane_index]) * 0.72
                # Strongly discourage physically absurd allocation.
                width = max(1e-6, _number(ordered[lane_index].get("width")))
                height = max(1e-6, _number(ordered[lane_index].get("height")))
                pitch = height / max(width * len(chunk), 1e-6)
                if pitch < 0.28 or pitch > 3.2:
                    length_cost += 5.0
                boundary_cost = _vertical_chunk_boundary_cost(chunk, final=(lane_index == count - 1))
                candidate = (score + length_cost + boundary_cost, [*chunks, chunk])
                key = (lane_index + 1, end)
                if key not in next_states or candidate[0] < next_states[key][0]:
                    next_states[key] = candidate
        states = next_states
        if not states:
            return []
    final = states.get((count, len(compact)))
    if final is None:
        return []
    score, chunks = final
    if score > max(5.0, count * 2.2):
        return []
    return chunks


def _promote_wide_vertical_text_to_layout_lanes(
    image: Image.Image,
    regions: list[dict[str, object]],
    layout_proposals: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Replace a collapsed multi-column Vision region with observed text lanes.

    Vision often recognizes the *combined* bubble text correctly while exposing
    no usable per-character geometry (p018: 「まだ栓もあけてない」).  When two
    to four strong pixel lanes sit inside that region, preserve the recognized
    text and move only its geometry onto those lanes.
    """
    output: list[dict[str, object]] = []
    for region in regions:
        compact = _compact_surface(region.get("text"))
        orientation = str(region.get("orientation") or "")
        # MangaService normalizes low-confidence VNDetectTextRectangles regions
        # to vertical *after* the worker returns.  v20 therefore skipped its
        # final lane split for p018 even though the exact same pixels exposed
        # two good layout lanes. Mirror that narrow normalization rule here so
        # the worker can enforce the geometry invariant before serialization.
        detector = str(region.get("detector") or "")
        confidence = float(region.get("confidence") or 0.0)
        japanese_count = _japanese_character_count(compact)
        rectangle_vertical = (
            orientation != "vertical"
            and "vision-rectangles" in detector
            and confidence <= 0.35
            and japanese_count >= 2
            and japanese_count / max(1, len(compact)) >= 0.55
        )
        if (orientation != "vertical" and not rectangle_vertical) or str(region.get("source") or "") in {
            _LAYOUT_LINE_SOURCE, "dark-block-proposal", "layout-cluster-v1", "layout-cluster-v2"
        }:
            output.append(region)
            continue
        if not (4 <= len(compact) <= 32) or _japanese_character_count(compact) < max(2, int(len(compact) * 0.55)):
            output.append(region)
            continue
        rx = _number(region.get("x")); rw = _number(region.get("width"))
        candidate_members: list[dict[str, object]] = []
        for proposal in layout_proposals:
            if str(proposal.get("orientation") or "") != "vertical":
                continue
            center = _number(proposal.get("x")) + _number(proposal.get("width")) / 2.0
            if center < rx - 0.006 or center > rx + rw + 0.006:
                continue
            if _layout_vertical_overlap(region, proposal) < 0.38:
                continue
            provenance = proposal.get("provenance")
            if isinstance(provenance, dict):
                coverage = float(provenance.get("component_coverage") or 0.0)
                count = int(provenance.get("component_count") or 0)
                if count >= 2 and coverage < 0.62:
                    continue
            candidate_members.append(dict(proposal))
        # De-duplicate nearly identical x tracks.
        candidate_members.sort(key=lambda item: _number(item.get("x")))
        members: list[dict[str, object]] = []
        for member in candidate_members:
            center = _number(member.get("x")) + _number(member.get("width")) / 2.0
            if members:
                prev = _number(members[-1].get("x")) + _number(members[-1].get("width")) / 2.0
                if abs(center - prev) < 0.012:
                    if _number(member.get("height")) > _number(members[-1].get("height")):
                        members[-1] = member
                    continue
            members.append(member)
        if not (2 <= len(members) <= 4):
            output.append(region)
            continue
        median_width = sorted(_number(member.get("width")) for member in members)[len(members)//2]
        if rw < median_width * 1.45 or rw > median_width * 6.5:
            output.append(region)
            continue
        if rectangle_vertical:
            # A low-confidence Vision rectangle is only useful as a text donor
            # when its OCR covers most of the glyph capacity observed in the
            # candidate lanes.  On p017 a truncated/interleaved wide crop
            # (``ーーが．．．別にらしに``) covered only ~73% of the two
            # observed lanes and v21 fabricated a plausible-looking second
            # column from that incomplete string.  p018's good collapsed crop
            # (``まだ栓もあけてない``) covers the two lanes completely.
            expected_glyphs = sum(_layout_member_expected_glyphs(member) for member in members)
            if expected_glyphs >= 4.0 and len(compact) < math.ceil(expected_glyphs * 0.80):
                output.append(region)
                continue
        chunks = _split_text_across_layout_members(compact, members)
        ordered = sorted(members, key=lambda item: _number(item.get("x")) + _number(item.get("width"))/2.0, reverse=True)
        if len(chunks) != len(ordered):
            output.append(region)
            continue
        for member, chunk in zip(ordered, chunks):
            donor = dict(region)
            donor.update({key: member[key] for key in ("x", "y", "width", "height")})
            donor["orientation"] = "vertical"
            donor["orientation_reason"] = "worker-wide-vertical-layout-donor"
            donor["text"] = chunk
            donor["raw_text"] = chunk
            donor["source"] = _LAYOUT_LINE_SOURCE
            donor["detector"] = "wide-vertical-text-donor-v1"
            donor["recognition_selection"] = "wide-vertical-layout-donor-v1"
            donor["selected_hypothesis_id"] = "wide-vertical-layout-donor-v1"
            donor["hypotheses"] = [{"id":"wide-vertical-layout-donor-v1","text":chunk,"source":"recognized-wide-region","selected":True}]
            provenance = dict(member.get("provenance") or {})
            provenance["cluster_context_donor"] = True
            provenance["wide_vertical_text_donor"] = True
            donor["provenance"] = provenance
            segments = _layout_line_character_segments(donor, chunk, image=image)
            if segments:
                donor["segments"] = _tighten_vertical_slot_ink_segments(image, segments)
                donor["geometry_source"] = "wide-vertical-layout-donor-v1"
                donor["geometry_status"] = "approximate"
                donor["word_geometry"] = "proportional-single-column-v1"
            output.append(donor)
    return output



def _post_cluster_member_geometry_plausible(
    region: dict[str, object],
    value: object,
) -> bool:
    """Allow a narrowly clipped lane only when a whole-cluster read corroborates it.

    The normal layout guard intentionally rejects text whose vertical pitch is
    below 0.38 glyph-widths.  Real manga columns can sit just below that bound
    when the component detector clips leading/trailing ink (p017/p018).  This
    relaxed guard is never used on its own: callers must also prove that the
    complete right-to-left member stream agrees with an independently OCRed
    multi-column cluster.
    """
    if _layout_retry_acceptable(region, value):
        return True
    compact = _compact_surface(value)
    glyphs = _layout_candidate_glyph_count(compact)
    if glyphs < 2 or glyphs > 14:
        return False
    japanese = _japanese_character_count(compact)
    semantic = [
        character
        for character in compact
        if character.isalnum() or "\u3040" <= character <= "\u9fff"
    ]
    if japanese < 2 or japanese / max(1, len(semantic)) < 0.55:
        return False
    width = max(1e-6, _number(region.get("width")))
    height = max(1e-6, _number(region.get("height")))
    pitch_ratio = height / max(1e-6, width * glyphs)
    if not (0.30 <= pitch_ratio <= 2.80):
        return False
    provenance = region.get("provenance")
    component_count = int(region.get("component_count") or 0)
    if component_count <= 0 and isinstance(provenance, dict):
        component_count = int(provenance.get("component_count") or 0)
    if component_count < 2:
        return False
    lower = max(1, int(math.floor(component_count * 0.55)))
    upper = max(component_count + 2, int(math.ceil(component_count * 1.75)))
    return lower <= glyphs <= upper


def _post_cluster_member_geometry_plausible_under_exact_consensus(
    region: dict[str, object],
    value: object,
) -> bool:
    """Relax component-count bounds only under an exact multi-crop consensus.

    Connected-component detection can merge several touching glyphs into one blob
    (notably bold vertical dialogue).  In that case ``component_count`` severely
    underestimates the number of characters even though both the per-lane crop and
    the independent whole-cluster crop agree exactly.  Callers must establish that
    exact stream agreement before using this helper.
    """
    compact = _compact_surface(value)
    glyphs = _layout_candidate_glyph_count(compact)
    if glyphs < 2 or glyphs > 14:
        return False
    japanese = _japanese_character_count(compact)
    semantic = [
        character
        for character in compact
        if character.isalnum() or "\u3040" <= character <= "\u9fff"
    ]
    if japanese < 2 or japanese / max(1, len(semantic)) < 0.55:
        return False

    width = max(1e-6, _number(region.get("width")))
    height = max(1e-6, _number(region.get("height")))
    pitch_ratio = height / max(1e-6, width * glyphs)
    if not (0.20 <= pitch_ratio <= 3.20):
        return False

    provenance = region.get("provenance")
    component_count = int(region.get("component_count") or 0)
    coverage = float(region.get("component_coverage") or 0.0)
    if isinstance(provenance, dict):
        component_count = component_count or int(provenance.get("component_count") or 0)
        coverage = coverage or float(provenance.get("component_coverage") or 0.0)
    return component_count >= 2 and coverage >= 0.90 and glyphs <= component_count * 5


def _recognize_post_cluster_surface(
    model: object,
    image: Image.Image,
    region: dict[str, object],
) -> str:
    """OCR an observed crop without square padding or neighbour context."""
    crop = _crop_region(image, region)
    try:
        return _compact_surface(model(crop))  # type: ignore[operator]
    finally:
        crop.close()


def _post_cluster_owner(
    regions: list[dict[str, object]],
    member: dict[str, object],
) -> dict[str, object] | None:
    member_center = _number(member.get("x")) + _number(member.get("width")) / 2.0
    best: tuple[float, dict[str, object]] | None = None
    for region in regions:
        if str(region.get("orientation") or "") != "vertical":
            continue
        compact = _compact_surface(region.get("text"))
        if not compact:
            continue
        center = _number(region.get("x")) + _number(region.get("width")) / 2.0
        # Same-lane ownership must be tighter than normal merge tolerance.
        # Adjacent manga columns are often only ~0.03 page-width apart and a
        # wide clipped lane must not claim its neighbour (p017).
        tolerance = max(
            0.010,
            min(_number(region.get("width")), _number(member.get("width"))) * 0.55,
        )
        if abs(center - member_center) > tolerance:
            continue
        overlap = _layout_vertical_overlap(region, member)
        if overlap < 0.42:
            continue
        member_coverage = _region_coverage(region, member)
        score = overlap + 0.25 * member_coverage - abs(center - member_center) * 4.0
        if best is None or score > best[0]:
            best = (score, region)
    return best[1] if best is not None else None


def _post_cluster_semantic_surface(value: object) -> str:
    """Strip decorative punctuation while keeping Japanese/Latin/digits for agreement."""
    compact = _compact_surface(value)
    return "".join(
        character
        for character in compact
        if character.isalnum() or "\u3040" <= character <= "\u9fff"
    )


def _post_cluster_semantically_bounded_exact_member(
    cluster_text: object,
    member_text: object,
    *,
    before_text: object,
    after_text: object,
) -> bool:
    """Prove one lane exactly between two independent neighbouring owners.

    Punctuation can differ between whole-cluster and single-lane MangaOCR, so
    boundary proof uses semantic surfaces only. Both neighbouring owner surfaces
    must occur exactly once and in reading order, and the semantic text between
    them must equal the candidate lane exactly.
    """
    cluster = _post_cluster_semantic_surface(cluster_text)
    member = _post_cluster_semantic_surface(member_text)
    before = _post_cluster_semantic_surface(before_text)
    after = _post_cluster_semantic_surface(after_text)
    if not cluster or not member or not before or not after:
        return False
    if cluster.count(before) != 1 or cluster.count(after) != 1:
        return False
    start = cluster.find(before) + len(before)
    end = cluster.find(after, start)
    if end < start:
        return False
    return cluster[start:end] == member


def _post_cluster_consensus_member_text(
    cluster_text: object,
    member_text: object,
    *,
    before_text: object = "",
    after_text: object = "",
) -> tuple[str, float] | None:
    """Corroborate a direct lane OCR against the independently OCRed cluster.

    Exact member reads are preserved.  A one-character repair is allowed only
    when neighbouring lane OCR gives an unambiguous boundary inside the cluster
    read.  This is stronger than picking the most similar substring globally:
    it can repair ``Ｅやるよ`` -> ``やるよ`` and ``あれの楽しみ`` ->
    ``おれの楽しみ`` without guessing among unrelated same-looking substrings.
    """
    cluster = _compact_surface(cluster_text)
    member = _compact_surface(member_text)
    before = _compact_surface(before_text)
    after = _compact_surface(after_text)
    if not cluster or not member:
        return None
    if member in cluster:
        return member, 1.0
    if len(member) < 4:
        return None

    lo = 0
    hi = len(cluster)
    anchored = False
    if before and cluster.count(before) == 1:
        position = cluster.find(before)
        lo = position + len(before)
        anchored = True
    if after and cluster.count(after) == 1:
        position = cluster.find(after, lo)
        if position >= lo:
            hi = position
            anchored = True
    if not anchored or hi <= lo:
        return None

    candidate = cluster[lo:hi]
    if not (max(2, len(member) - 1) <= len(candidate) <= len(member) + 1):
        return None
    if _japanese_character_count(candidate) < 2:
        return None
    ratio = difflib.SequenceMatcher(a=member, b=candidate, autojunk=False).ratio()
    if ratio < 0.83:
        return None
    return candidate, ratio


def _post_recognition_cluster_recall(
    model: object,
    image: Image.Image,
    regions: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Recover lanes hidden by provisional Vision geometry, after OCR filtering.

    Initial Apple Vision observations can suppress a tighter pixel lane before
    MangaOCR runs; if that Vision observation is later rejected, the valid lane
    never gets another chance.  Revisit only compact 3-4 lane clusters after the
    normal pipeline has settled.  A missing lane is emitted only when direct OCR
    of every member, combined right-to-left, strongly agrees with direct OCR of
    the whole cluster.  This keeps the pass recall-only and avoids the old
    language/geometry-only fabricated-column failure mode.
    """
    output = [dict(region) for region in regions]
    if sum(str(region.get("orientation") or "") == "vertical" for region in output) < 3:
        return output

    primary = _layout_vertical_lines(image)
    augmented = _augment_cluster_members_with_raw_gaps(image, primary)
    clusters = _layout_cluster_proposals(image, augmented)
    for cluster in clusters:
        provenance = cluster.get("provenance")
        if not isinstance(provenance, dict):
            continue
        members = [
            dict(value)
            for value in provenance.get("member_boxes") or []
            if isinstance(value, dict)
        ]
        if not (3 <= len(members) <= 4):
            continue

        ordered = sorted(
            members,
            key=lambda value: _number(value.get("x")) + _number(value.get("width")) / 2.0,
            reverse=True,
        )
        owners = [_post_cluster_owner(output, member) for member in ordered]
        if all(owner is not None for owner in owners):
            continue
        # Low component counts are unsafe only for a lane we are about to invent.
        # Already-recognized owners are independent evidence for their member
        # geometry and may themselves come from strict merged/raw recovery paths.
        if any(
            owner is None
            and (
                int(member.get("component_count") or 0) < 2
                or float(member.get("component_coverage") or 0.0) < 0.62
            )
            for member, owner in zip(ordered, owners)
        ):
            continue

        try:
            cluster_text = _recognize_post_cluster_surface(model, image, cluster)
        except Exception:
            continue
        if not _layout_text_usable(cluster_text) or not (6 <= len(cluster_text) <= 48):
            continue

        member_texts: list[str] = []
        missing_reads: dict[int, str] = {}
        failed = False
        for index, (member, owner) in enumerate(zip(ordered, owners)):
            if owner is not None:
                text = _compact_surface(owner.get("text"))
                if not _layout_text_usable(text):
                    failed = True
                    break
            else:
                try:
                    text = _recognize_post_cluster_surface(model, image, member)
                except Exception:
                    failed = True
                    break
                missing_reads[index] = text
            member_texts.append(text)
        if failed or not missing_reads:
            continue

        # Direct lane OCR can steal a leading neighbour fragment even when the
        # whole-cluster OCR and both already-recognized neighbouring lanes give
        # exact semantic boundaries.  Before comparing the combined stream, allow
        # a two-of-three local member ensemble to replace only such an unbounded
        # direct read.  This stays recall-only: both semantic owners must be unique
        # in the independent cluster OCR and the replacement itself must be exactly
        # the substring between them.
        bounded_local_ensemble: dict[int, dict[str, object]] = {}
        for index, direct_text in list(missing_reads.items()):
            semantic_cluster = _post_cluster_semantic_surface(cluster_text)
            before = next(
                (
                    value
                    for value in reversed(member_texts[:index])
                    if value
                    and _post_cluster_semantic_surface(value)
                    and semantic_cluster.count(_post_cluster_semantic_surface(value)) == 1
                ),
                "",
            )
            after = next(
                (
                    value
                    for value in member_texts[index + 1 :]
                    if value
                    and _post_cluster_semantic_surface(value)
                    and semantic_cluster.count(_post_cluster_semantic_surface(value)) == 1
                ),
                "",
            )
            if not before or not after:
                continue
            if _post_cluster_semantically_bounded_exact_member(
                cluster_text,
                direct_text,
                before_text=before,
                after_text=after,
            ):
                continue
            try:
                local_views = _recognize_layout_member_ensemble(model, image, ordered[index])
            except Exception:
                continue
            counts: dict[str, int] = {}
            for value in local_views:
                compact = _compact_surface(value)
                if compact:
                    counts[compact] = counts.get(compact, 0) + 1
            candidates = sorted(
                counts.items(),
                key=lambda item: (-item[1], -len(item[0]), item[0]),
            )
            replacement = next(
                (
                    candidate
                    for candidate, votes in candidates
                    if votes >= 2
                    and _post_cluster_semantically_bounded_exact_member(
                        cluster_text,
                        candidate,
                        before_text=before,
                        after_text=after,
                    )
                ),
                "",
            )
            if not replacement:
                continue
            missing_reads[index] = replacement
            member_texts[index] = replacement
            bounded_local_ensemble[index] = {
                "direct_text": direct_text,
                "replacement_text": replacement,
                "votes": int(counts.get(replacement, 0)),
                "views": list(local_views),
            }

        joined = "".join(member_texts)
        length_delta = abs(len(joined) - len(cluster_text))
        if length_delta > max(4, int(math.ceil(len(cluster_text) * 0.24))):
            continue
        agreement = difflib.SequenceMatcher(
            a=cluster_text,
            b=joined,
            autojunk=False,
        ).ratio()
        semantic_cluster = _post_cluster_semantic_surface(cluster_text)
        semantic_joined = _post_cluster_semantic_surface(joined)
        semantic_agreement = difflib.SequenceMatcher(
            a=semantic_cluster,
            b=semantic_joined,
            autojunk=False,
        ).ratio() if semantic_cluster and semantic_joined else 0.0

        exact_stream_consensus = (
            joined == cluster_text
            and semantic_joined == semantic_cluster
            and bool(cluster_text)
        )

        consensus_reads: dict[int, tuple[str, float]] = {}
        bounded_exact_reads: dict[int, bool] = {}
        for index, text in missing_reads.items():
            before = next(
                (value for value in reversed(member_texts[:index]) if value and _post_cluster_semantic_surface(cluster_text).count(_post_cluster_semantic_surface(value)) == 1),
                "",
            )
            after = next(
                (value for value in member_texts[index + 1 :] if value and _post_cluster_semantic_surface(cluster_text).count(_post_cluster_semantic_surface(value)) == 1),
                "",
            )
            consensus = _post_cluster_consensus_member_text(
                cluster_text,
                text,
                before_text=before,
                after_text=after,
            )
            if consensus is None:
                failed = True
                break
            consensus_reads[index] = consensus
            bounded_exact_reads[index] = _post_cluster_semantically_bounded_exact_member(
                cluster_text,
                consensus[0],
                before_text=before,
                after_text=after,
            )
        if failed:
            continue

        # Normal clusters still need the strong whole-stream agreement from v23.
        # A second path exists only for p017-like punctuation/neighbour noise: the
        # missing lane itself must be an exact substring of the independently OCRed
        # cluster and the semantic stream must still agree very strongly.
        if agreement < 0.82:
            if semantic_agreement < 0.90 or any(score < 0.999 for _, score in consensus_reads.values()):
                continue

        emitted_indices: list[int] = []
        for index, raw_text in missing_reads.items():
            text, local_agreement = consensus_reads[index]
            member = ordered[index]
            geometry_ok = _post_cluster_member_geometry_plausible(member, text)
            if not geometry_ok:
                geometry_ok = (
                    local_agreement >= 0.999
                    and (exact_stream_consensus or bounded_exact_reads.get(index, False))
                    and _post_cluster_member_geometry_plausible_under_exact_consensus(member, text)
                )
            if not geometry_ok:
                continue
            donor = dict(cluster)
            donor.update({key: member[key] for key in ("x", "y", "width", "height")})
            donor["text"] = text
            donor["raw_text"] = text
            donor["orientation"] = "vertical"
            donor["orientation_reason"] = "post-recognition-cluster-consensus"
            donor["source"] = _LAYOUT_LINE_SOURCE
            donor["detector"] = _LAYOUT_CLUSTER_DETECTOR
            donor["recognizer"] = "manga-ocr"
            donor["recognition_selection"] = "post-recognition-cluster-consensus-v1"
            donor["selected_hypothesis_id"] = "manga-ocr-post-cluster-member-v1"
            donor["hypotheses"] = [
                {
                    "id": "manga-ocr-post-cluster-member-v1",
                    "text": text,
                    "source": "manga-ocr",
                    "selected": True,
                }
            ]
            donor_provenance = dict(provenance)
            donor_provenance["post_recognition_cluster_donor"] = True
            donor_provenance["cluster_direct_text"] = cluster_text
            donor_provenance["cluster_member_stream"] = joined
            donor_provenance["cluster_member_agreement"] = round(agreement, 4)
            donor_provenance["cluster_member_semantic_agreement"] = round(semantic_agreement, 4)
            donor_provenance["cluster_member_raw_text"] = raw_text
            donor_provenance["cluster_member_consensus_text"] = text
            donor_provenance["cluster_member_local_agreement"] = round(local_agreement, 4)
            if bounded_exact_reads.get(index, False):
                donor_provenance["cluster_member_bounded_exact_consensus"] = True
                donor_provenance["cluster_member_bounded_exact_consensus_kind"] = "semantic-neighbours-v1"
            local_ensemble = bounded_local_ensemble.get(index)
            if local_ensemble is not None:
                donor_provenance["cluster_member_bounded_local_ensemble"] = True
                donor_provenance["cluster_member_bounded_local_ensemble_kind"] = "two-of-three-semantic-boundary-v1"
                donor_provenance["cluster_member_bounded_local_ensemble_direct_text"] = str(
                    local_ensemble.get("direct_text") or ""
                )
                donor_provenance["cluster_member_bounded_local_ensemble_votes"] = int(
                    local_ensemble.get("votes") or 0
                )
            donor["provenance"] = donor_provenance
            segments = _layout_line_character_segments(donor, text, image=image)
            if segments:
                donor["segments"] = _tighten_vertical_slot_ink_segments(image, segments)
                donor["geometry_source"] = "post-recognition-cluster-consensus-v1"
                donor["geometry_status"] = "approximate"
                donor["word_geometry"] = "proportional-single-column-v1"
            output.append(donor)
            emitted_indices.append(index)

        if (
            exact_stream_consensus
            and all(owner is None for owner in owners)
            and len(emitted_indices) == len(ordered)
        ):
            output = _suppress_cross_lane_cluster_amalgams(
                output,
                ordered,
                [consensus_reads[index][0] for index in range(len(ordered))],
            )

    for order, region in enumerate(output):
        region["order"] = order
    return output


def _suppress_cross_lane_cluster_amalgams(
    regions: list[dict[str, object]],
    members: list[dict[str, object]],
    member_texts: list[str],
) -> list[dict[str, object]]:
    """Drop a weak Vision strip that demonstrably mixes several recalled lanes.

    Some provisional Vision rectangles span the tops of multiple vertical columns.
    MangaOCR then emits a fluent but interleaved fragment (p008).  Once all real
    lanes have been independently OCRed and exactly reconstruct the cluster, the
    cross-lane strip is redundant.  Require both geometric coverage of at least two
    lanes and text evidence from at least two distinct lane surfaces.
    """
    if len(members) < 3 or len(member_texts) != len(members):
        return regions
    cluster_left = min(_number(member.get("x")) for member in members)
    cluster_right = max(
        _number(member.get("x")) + _number(member.get("width"))
        for member in members
    )
    cluster_width = max(1e-6, cluster_right - cluster_left)
    lane_semantics = [_post_cluster_semantic_surface(text) for text in member_texts]

    drop: set[int] = set()
    for index, region in enumerate(regions):
        if str(region.get("recognition_selection") or "") == "post-recognition-cluster-consensus-v1":
            continue
        if str(region.get("orientation") or "") != "vertical":
            continue
        if _region_geometry_trust(region) != "weak" or _number(region.get("confidence"), 1.0) > 0.40:
            continue
        region_width = _number(region.get("width"))
        region_height = _number(region.get("height"))
        if region_width <= 0.0 or region_height <= 0.0:
            continue

        # Two shapes are known to create a cross-lane donor:
        # 1) an obviously over-wide region (legacy path);
        # 2) a shallow transverse strip spanning nearly the whole cluster width.
        # The latter is what Accurate Vision produces on p008: it only covers the
        # top ~26-28% of each real vertical lane, so a 30% area threshold misses it
        # even though its horizontal span crosses all lanes.
        legacy_overwide = region_width >= cluster_width * 1.55
        transverse_strip = (
            region_width >= cluster_width * 0.88
            and region_width >= region_height * 1.55
        )
        if not (legacy_overwide or transverse_strip):
            continue

        coverage_floor = 0.18 if transverse_strip else 0.30
        covered = [
            member
            for member in members
            if _region_coverage(region, member) >= coverage_floor
        ]
        if len(covered) < 2:
            continue

        candidate = _post_cluster_semantic_surface(region.get("text"))
        if len(candidate) < 4:
            continue
        textual_hits = 0
        for lane in lane_semantics:
            if len(lane) < 2:
                continue
            match = difflib.SequenceMatcher(a=candidate, b=lane, autojunk=False).find_longest_match()
            if match.size >= 2:
                textual_hits += 1
        if textual_hits >= 2:
            drop.add(index)

    if not drop:
        return regions
    return [dict(region) for index, region in enumerate(regions) if index not in drop]


def _suppress_complete_post_cluster_amalgams(
    regions: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Re-run cross-lane suppression once the final recalled group is complete.

    Inline suppression during recall can miss a weak wide donor when another
    provisional region temporarily affects cluster ownership/order.  At the end
    of the pipeline the recalled donors carry the exact cluster member boxes and
    direct OCR stream, so a complete group can be checked deterministically.
    Only exact whole-cluster reconstruction is eligible, then the existing weak
    cross-lane geometry/text filter decides what (if anything) to remove.
    """
    output = [dict(region) for region in regions]
    grouped: dict[tuple[object, ...], list[dict[str, object]]] = {}
    for region in output:
        signature = _post_cluster_group_signature(region)
        if signature is not None:
            grouped.setdefault(signature, []).append(region)

    for _signature, group in grouped.items():
        provenance = group[0].get("provenance")
        if not isinstance(provenance, dict):
            continue
        members = [
            dict(value)
            for value in provenance.get("member_boxes") or []
            if isinstance(value, dict)
        ]
        if not (3 <= len(members) <= 4) or len(group) != len(members):
            continue
        direct = _compact_surface(provenance.get("cluster_direct_text"))
        if not direct:
            continue
        ordered = sorted(
            group,
            key=lambda value: _number(value.get("x")) + _number(value.get("width")) / 2.0,
            reverse=True,
        )
        texts = [_compact_surface(region.get("text")) for region in ordered]
        if any(not text for text in texts) or "".join(texts) != direct:
            continue
        output = _suppress_cross_lane_cluster_amalgams(output, members, texts)

    for order, region in enumerate(output):
        region["order"] = order
    return output


def _vertical_duplicate_surface(value: object) -> str:
    """Kana-normalized semantic surface for nested vertical duplicate checks."""
    compact = unicodedata.normalize("NFKC", _compact_surface(value))
    normalized: list[str] = []
    for character in compact:
        code = ord(character)
        if 0x30A1 <= code <= 0x30F6:
            character = chr(code - 0x60)
        if character.isalnum() or "\u3040" <= character <= "\u9fff":
            normalized.append(character)
    return "".join(normalized)


def _suppress_supported_raw_same_lane_fragments(
    regions: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Drop a clipped primary fragment only under v43's unique raw support.

    The high-count raw lane is independently pixel-observed and locally OCRed.
    If a much shorter one-component primary result sits inside that exact lane,
    it is the clipped owner that previously blocked cluster recall, not a separate
    manga column.
    """
    drop: set[int] = set()
    for outer_index, outer in enumerate(regions):
        provenance = outer.get("provenance")
        if not isinstance(provenance, dict):
            continue
        if provenance.get("support_kind") != "high-count-adjacent-primary-raw-v1":
            continue
        if str(outer.get("detector") or "") != _RAW_LAYOUT_DETECTOR:
            continue
        outer_text = _post_cluster_semantic_surface(outer.get("text"))
        if len(outer_text) < 8:
            continue
        outer_center = _number(outer.get("x")) + _number(outer.get("width")) / 2.0
        for inner_index, inner in enumerate(regions):
            if inner_index == outer_index or inner_index in drop:
                continue
            if str(inner.get("source") or "") != _LAYOUT_LINE_SOURCE:
                continue
            inner_provenance = inner.get("provenance")
            if not isinstance(inner_provenance, dict):
                continue
            if int(inner_provenance.get("component_count") or 0) > 1:
                continue
            inner_center = _number(inner.get("x")) + _number(inner.get("width")) / 2.0
            if abs(outer_center - inner_center) > 0.006:
                continue
            if _region_coverage(outer, inner) < 0.82:
                continue
            if _number(outer.get("height")) < _number(inner.get("height")) * 1.70:
                continue
            inner_text = _post_cluster_semantic_surface(inner.get("text"))
            if not inner_text or len(outer_text) < len(inner_text) + 4:
                continue
            drop.add(inner_index)

    if not drop:
        return regions
    output = [dict(region) for index, region in enumerate(regions) if index not in drop]
    for order, region in enumerate(output):
        region["order"] = order
    return output


def _suppress_nested_expanded_vertical_duplicates(
    regions: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Remove a nested suffix OCR of the same expanded vertical seed.

    Expanded Vision seeds can overlap heavily and independently OCR the same large
    scream/SFX crop.  Keep the larger observation only when the smaller box is
    mostly contained and its kana-normalized semantic text is a substring of the
    larger read.  Layout-derived neighbours are intentionally excluded.
    """
    expanded_sources = {"expanded-vertical-seed", "expanded-vision-rectangle"}
    drop: set[int] = set()
    for outer_index, outer in enumerate(regions):
        if str(outer.get("orientation") or "") != "vertical":
            continue
        if str(outer.get("source") or "") not in expanded_sources:
            continue
        outer_surface = _vertical_duplicate_surface(outer.get("text"))
        if len(outer_surface) < 4:
            continue
        outer_area = _region_area(outer)
        for inner_index, inner in enumerate(regions):
            if inner_index == outer_index or inner_index in drop:
                continue
            if str(inner.get("orientation") or "") != "vertical":
                continue
            if str(inner.get("source") or "") not in expanded_sources:
                continue
            inner_surface = _vertical_duplicate_surface(inner.get("text"))
            if len(inner_surface) < 4 or len(inner_surface) > len(outer_surface):
                continue
            if inner_surface not in outer_surface:
                continue
            inner_area = _region_area(inner)
            if outer_area < inner_area * 1.45:
                continue
            if _number(outer.get("width")) < _number(inner.get("width")) * 1.30:
                continue
            if _region_coverage(outer, inner) < 0.72:
                continue
            drop.add(inner_index)

    if not drop:
        return regions
    output = [dict(region) for index, region in enumerate(regions) if index not in drop]
    for order, region in enumerate(output):
        region["order"] = order
    return output


def _post_cluster_group_signature(region: dict[str, object]) -> tuple[object, ...] | None:
    provenance = region.get("provenance")
    if not isinstance(provenance, dict) or not provenance.get("post_recognition_cluster_donor"):
        return None
    direct = _compact_surface(provenance.get("cluster_direct_text"))
    members = provenance.get("member_boxes")
    if not direct or not isinstance(members, list) or not (3 <= len(members) <= 4):
        return None
    boxes: list[tuple[float, float, float, float]] = []
    for member in members:
        if not isinstance(member, dict):
            return None
        boxes.append(
            (
                round(_number(member.get("x")), 6),
                round(_number(member.get("y")), 6),
                round(_number(member.get("width")), 6),
                round(_number(member.get("height")), 6),
            )
        )
    return (direct, *boxes)


def _repair_post_cluster_truncated_members(
    image: Image.Image,
    regions: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Extend one OCR-confirmed vertical lane when cluster OCR proves truncation.

    A tight layout box can end between glyphs even though OCR of the whole bubble
    still sees the missing 1-3 character suffix/prefix.  Repair only complete
    3-4 member post-recognition clusters where every current member occurs
    exactly once, in order, in the independent cluster OCR and there is exactly
    one short Japanese gap *between* two members.  Shared top/bottom alignment
    decides which neighbouring lane owns the gap, and pixel-derived character
    segments must confirm real ink in the newly exposed area.
    """
    output = [dict(region) for region in regions]
    grouped: dict[tuple[object, ...], list[tuple[int, dict[str, object]]]] = {}
    for index, region in enumerate(output):
        signature = _post_cluster_group_signature(region)
        if signature is None:
            continue
        grouped.setdefault(signature, []).append((index, region))

    for signature, group in grouped.items():
        direct = str(signature[0])
        provenance = group[0][1].get("provenance")
        if not isinstance(provenance, dict):
            continue
        member_boxes = provenance.get("member_boxes")
        if not isinstance(member_boxes, list) or len(group) != len(member_boxes):
            # Partial recalled clusters do not provide enough ownership evidence.
            continue

        ordered = sorted(
            group,
            key=lambda pair: _number(pair[1].get("x")) + _number(pair[1].get("width")) / 2.0,
            reverse=True,
        )
        texts = [_compact_surface(region.get("text")) for _, region in ordered]
        if any(not text or direct.count(text) != 1 for text in texts):
            continue
        stream = "".join(texts)
        if stream == direct:
            continue
        if difflib.SequenceMatcher(a=direct, b=stream, autojunk=False).ratio() < 0.90:
            continue

        positions: list[tuple[int, int]] = []
        cursor = 0
        valid = True
        for text in texts:
            start = direct.find(text, cursor)
            if start < cursor:
                valid = False
                break
            positions.append((start, start + len(text)))
            cursor = start + len(text)
        if not valid or positions[0][0] != 0 or positions[-1][1] != len(direct):
            continue

        gaps: list[tuple[int, str]] = []
        for index in range(len(positions) - 1):
            left_end = positions[index][1]
            right_start = positions[index + 1][0]
            if right_start > left_end:
                gaps.append((index, direct[left_end:right_start]))
        if len(gaps) != 1:
            continue
        gap_index, gap = gaps[0]
        added_count = _japanese_character_count(gap)
        if not (1 <= added_count <= 3) or added_count != len(gap):
            continue

        right_output_index, right_member = ordered[gap_index]
        left_output_index, left_member = ordered[gap_index + 1]
        right_top = _number(right_member.get("y")) + _number(right_member.get("height"))
        left_top = _number(left_member.get("y")) + _number(left_member.get("height"))
        right_bottom = _number(right_member.get("y"))
        left_bottom = _number(left_member.get("y"))
        top_delta = abs(right_top - left_top)
        bottom_delta = abs(right_bottom - left_bottom)

        trailing = top_delta <= 0.012 and bottom_delta >= 0.020
        leading = bottom_delta <= 0.012 and top_delta >= 0.020
        if trailing == leading:
            continue
        target_index = right_output_index if trailing else left_output_index
        target = right_member if trailing else left_member
        old_text = texts[gap_index] if trailing else texts[gap_index + 1]
        candidate_text = old_text + gap if trailing else gap + old_text

        pitches: list[float] = []
        for _idx, member in ordered:
            member_text = _compact_surface(member.get("text"))
            count = _japanese_character_count(member_text)
            height = _number(member.get("height"))
            if 2 <= count <= 12 and height > 0:
                pitch = height / count
                if 0.006 <= pitch <= 0.030:
                    pitches.append(pitch)
        if len(pitches) < 2:
            continue
        pitch = statistics.median(pitches)
        extension = pitch * added_count
        if not (0.006 <= extension <= 0.070):
            continue

        repaired = dict(target)
        old_y = _number(target.get("y"))
        old_height = _number(target.get("height"))
        if trailing:
            new_y = max(0.0, old_y - extension)
            repaired["y"] = round(new_y, 6)
            repaired["height"] = round(old_height + old_y - new_y, 6)
        else:
            repaired["height"] = round(min(1.0 - old_y, old_height + extension), 6)
        repaired["text"] = candidate_text
        repaired["raw_text"] = candidate_text

        segments = _layout_line_character_segments(repaired, candidate_text, image=image)
        compact_segments = "".join(_compact_surface(segment.get("text")) for segment in segments)
        if compact_segments != candidate_text or len(segments) != len(candidate_text):
            continue
        added_segments = segments[-added_count:] if trailing else segments[:added_count]
        if any(not str(segment.get("source") or "").startswith("layout-line-ink-v2") for segment in added_segments):
            continue
        if trailing:
            # At least one recovered glyph must live below the old lane bottom.
            if not any(_number(segment.get("y")) < old_y - 0.001 for segment in added_segments):
                continue
        else:
            old_top = old_y + old_height
            if not any(
                _number(segment.get("y")) + _number(segment.get("height")) > old_top + 0.001
                for segment in added_segments
            ):
                continue

        repaired["segments"] = _tighten_vertical_slot_ink_segments(image, segments)
        repaired["recognition_selection"] = "post-recognition-cluster-edge-consensus-v1"
        repaired["selected_hypothesis_id"] = "manga-ocr-post-cluster-edge-consensus-v1"
        hypotheses = [
            dict(value) for value in repaired.get("hypotheses", []) if isinstance(value, dict)
        ]
        for hypothesis in hypotheses:
            hypothesis["selected"] = False
        hypotheses.append(
            {
                "id": "manga-ocr-post-cluster-edge-consensus-v1",
                "text": candidate_text,
                "source": "manga-ocr-cluster-consensus",
                "selected": True,
            }
        )
        repaired["hypotheses"] = hypotheses
        repaired["geometry_source"] = "post-recognition-cluster-edge-consensus-v1"
        repaired["geometry_status"] = "approximate"
        repaired["word_geometry"] = "ink-segmented-single-column-v1"
        repaired_provenance = dict(provenance)
        repaired_provenance["cluster_edge_consensus_repair"] = True
        repaired_provenance["cluster_member_stream_before_edge_repair"] = stream
        repaired_provenance["cluster_member_stream_after_edge_repair"] = (
            "".join(texts[:gap_index])
            + (candidate_text if trailing else texts[gap_index])
            + (texts[gap_index + 1] if trailing else candidate_text)
            + "".join(texts[gap_index + 2 :])
        )
        repaired_provenance["cluster_edge_gap_text"] = gap
        repaired_provenance["cluster_edge_gap_side"] = "trailing" if trailing else "leading"
        repaired_provenance["cluster_edge_extension"] = round(extension, 6)
        repaired["provenance"] = repaired_provenance
        output[target_index] = repaired

    for order, region in enumerate(output):
        region["order"] = order
    return output



def _trim_wide_donor_adjacent_tall_prefix(
    regions: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Trim text leaked from the next left-hand lane out of a wide donor.

    Wide Vision text blocks are read in manga order (right-to-left) and then
    projected back onto narrow pixel lanes. A donor for the right lane can
    therefore end with the first 2-4 glyphs of the immediately adjacent left
    lane. Trim only when that left lane was independently recovered by the
    strict tall-merged direct+square consensus, the original wide detector box
    physically spans both lane centres, and the donor has an exact per-glyph
    segment stream. A one-character coincidence is deliberately insufficient.
    """
    output = [dict(region) for region in regions]
    for index, donor in enumerate(output):
        donor_provenance = donor.get("provenance")
        if not (
            str(donor.get("orientation") or "") == "vertical"
            and isinstance(donor_provenance, dict)
            and donor_provenance.get("wide_vertical_text_donor")
        ):
            continue

        donor_text = _compact_surface(donor.get("text"))
        if len(donor_text) < 5:
            continue
        segments = [
            dict(segment)
            for segment in donor.get("segments") or []
            if isinstance(segment, dict)
        ]
        segment_stream = "".join(_compact_surface(segment.get("text")) for segment in segments)
        if len(segments) != len(donor_text) or segment_stream != donor_text:
            continue

        detector_geometry = donor.get("detector_geometry")
        if not isinstance(detector_geometry, dict):
            continue
        source_x = _number(detector_geometry.get("x"))
        source_width = _number(detector_geometry.get("width"))
        if source_width <= _number(donor.get("width")) * 1.45:
            continue
        source_left = source_x - 0.006
        source_right = source_x + source_width + 0.006

        donor_center = _number(donor.get("x")) + _number(donor.get("width")) / 2.0
        if not source_left <= donor_center <= source_right:
            continue

        best: tuple[int, float, int, dict[str, object]] | None = None
        for peer_index, peer in enumerate(output):
            if peer_index == index or str(peer.get("orientation") or "") != "vertical":
                continue
            peer_provenance = peer.get("provenance")
            if not (
                isinstance(peer_provenance, dict)
                and peer_provenance.get("tall_merged_ocr_consensus")
            ):
                continue
            peer_text = _compact_surface(peer.get("text"))
            if len(peer_text) < 3:
                continue
            peer_center = _number(peer.get("x")) + _number(peer.get("width")) / 2.0

            # Japanese vertical columns are consumed right-to-left: the wide
            # donor on the right may steal the prefix of the next lane on its
            # left, not vice versa.
            center_gap = donor_center - peer_center
            if center_gap < 0.015 or center_gap > 0.065:
                continue
            if not source_left <= peer_center <= source_right:
                continue
            overlap = _layout_vertical_overlap(donor, peer)
            if overlap < 0.70:
                continue

            max_trim = min(4, len(peer_text), len(donor_text) - 3)
            trim_count = 0
            for size in range(max_trim, 1, -1):
                suffix = donor_text[-size:]
                if suffix == peer_text[:size] and _japanese_character_count(suffix) == size:
                    trim_count = size
                    break
            if trim_count < 2:
                continue

            score = (-trim_count, center_gap, peer_index)
            if best is None or score < (best[0], best[1], best[2]):
                best = (-trim_count, center_gap, peer_index, peer)

        if best is None:
            continue

        trim_count = -best[0]
        peer = best[3]
        trimmed_suffix = donor_text[-trim_count:]
        retained_text = donor_text[:-trim_count]
        retained_segments = segments[:-trim_count]
        if len(retained_text) < 3 or not retained_segments:
            continue

        low_y = min(_number(segment.get("y")) for segment in retained_segments)
        high_y = max(
            _number(segment.get("y")) + _number(segment.get("height"))
            for segment in retained_segments
        )
        if high_y <= low_y:
            continue

        repaired = dict(donor)
        repaired["text"] = retained_text
        repaired["raw_text"] = retained_text
        repaired["segments"] = retained_segments
        repaired["y"] = low_y
        repaired["height"] = high_y - low_y
        repaired["recognition_selection"] = "wide-vertical-layout-donor-adjacent-trim-v1"
        repaired["selected_hypothesis_id"] = "wide-vertical-layout-donor-adjacent-trim-v1"
        hypotheses = [
            dict(hypothesis)
            for hypothesis in donor.get("hypotheses") or []
            if isinstance(hypothesis, dict)
        ]
        for hypothesis in hypotheses:
            hypothesis["selected"] = False
        hypotheses.append(
            {
                "id": "wide-vertical-layout-donor-adjacent-trim-v1",
                "text": retained_text,
                "source": "adjacent-tall-merged-consensus",
                "selected": True,
            }
        )
        repaired["hypotheses"] = hypotheses

        repaired_provenance = dict(donor_provenance)
        repaired_provenance["adjacent_tall_prefix_trim"] = True
        repaired_provenance["adjacent_tall_prefix_trim_kind"] = "wide-donor-suffix-v1"
        repaired_provenance["adjacent_tall_prefix_trim_original_text"] = donor_text
        repaired_provenance["adjacent_tall_prefix_trim_suffix"] = trimmed_suffix
        repaired_provenance["adjacent_tall_prefix_trim_count"] = trim_count
        peer_center = _number(peer.get("x")) + _number(peer.get("width")) / 2.0
        repaired_provenance["adjacent_tall_prefix_trim_spacing"] = round(
            donor_center - peer_center, 6
        )
        repaired_provenance["adjacent_tall_prefix_trim_overlap"] = round(
            _layout_vertical_overlap(donor, peer), 6
        )
        repaired["provenance"] = repaired_provenance
        output[index] = repaired

    for order, region in enumerate(output):
        region["order"] = order
    return output

def _suppress_redundant_implausible_wide_vertical_donors(
    regions: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Drop only physically impossible wide-text donors duplicated by real lanes.

    A wide Vision rectangle can occasionally donate an entire neighbouring
    phrase to one narrow layout lane.  Do not reject an isolated donor merely
    because its text/geometry ratio is suspicious: some real manga lanes are
    irregular.  Reject it only when the donor is geometrically impossible and
    an independently recognized non-donor vertical lane overlaps the same area.
    """
    drop: set[int] = set()
    for index, donor in enumerate(regions):
        provenance = donor.get("provenance")
        if not (
            str(donor.get("orientation") or "") == "vertical"
            and isinstance(provenance, dict)
            and provenance.get("wide_vertical_text_donor")
        ):
            continue
        if _layout_retry_acceptable(donor, donor.get("text")):
            continue

        donor_center = _number(donor.get("x")) + _number(donor.get("width")) / 2.0
        for peer_index, peer in enumerate(regions):
            if peer_index == index or str(peer.get("orientation") or "") != "vertical":
                continue
            peer_provenance = peer.get("provenance")
            if isinstance(peer_provenance, dict) and peer_provenance.get("wide_vertical_text_donor"):
                continue
            if not _compact_surface(peer.get("text")):
                continue
            peer_center = _number(peer.get("x")) + _number(peer.get("width")) / 2.0
            center_tolerance = max(
                0.045,
                (_number(donor.get("width")) + _number(peer.get("width"))) * 0.85,
            )
            if abs(peer_center - donor_center) > center_tolerance:
                continue
            if _layout_vertical_overlap(donor, peer) < 0.65:
                continue
            if not (
                _layout_retry_acceptable(peer, peer.get("text"))
                or str(peer.get("detector") or "") != "wide-vertical-text-donor-v1"
                and _layout_text_geometry_plausible(peer, peer.get("text"))
            ):
                continue
            drop.add(index)
            break

    if not drop:
        return regions
    output = [dict(region) for idx, region in enumerate(regions) if idx not in drop]
    for order, region in enumerate(output):
        region["order"] = order
    return output


def _recognize_layout_cluster_members(
    model: object,
    image: Image.Image,
    compact: str,
    members: list[dict[str, object]],
) -> list[str]:
    """Read observed cluster lanes independently with small-view consensus.

    Return one entry per right-to-left member; an empty string means that member
    was not independently trustworthy.  This allows recall of two good lanes
    even when a third lane is unreadable, without fabricating text for it.
    """
    if not compact or len(members) < 2:
        return []
    ordered = sorted(
        members,
        key=lambda value: _number(value.get("x")) + _number(value.get("width")) / 2.0,
        reverse=True,
    )
    outputs: list[str] = []
    details: list[dict[str, object]] = []
    for member in ordered:
        try:
            variants = _recognize_layout_member_ensemble(model, image, member)
        except Exception:
            variants = []
        candidate, detail = _layout_member_consensus_candidate(member, variants, compact)
        outputs.append(candidate)
        details.append(detail)

    nonempty = [value for value in outputs if value]
    if not nonempty:
        return ["" for _ in ordered]
    joined = "".join(nonempty)
    ratio = difflib.SequenceMatcher(a=compact, b=joined, autojunk=False).ratio()
    # If every member produced text but together they disagree radically with
    # the wider crop, treat the cluster as unstable. Partial high-confidence
    # members remain useful recall donors and are merged lane-by-lane later.
    if len(nonempty) == len(ordered):
        if ratio < 0.48 and abs(len(joined) - len(compact)) > max(3, int(round(len(compact) * 0.25))):
            return ["" for _ in ordered]
    return outputs


def _split_layout_cluster_region(
    image: Image.Image,
    item: dict[str, object],
    *,
    model: object | None = None,
) -> list[dict[str, object]]:
    if str(item.get("source") or "") not in {"layout-cluster-v1", "layout-cluster-v2"}:
        return [item]
    provenance = item.get("provenance")
    if not isinstance(provenance, dict):
        return []
    members = [dict(value) for value in provenance.get("member_boxes") or [] if isinstance(value, dict)]
    if len(members) < 2:
        return []
    compact = _compact_surface(item.get("text"))
    semantic = [character for character in compact if character.isalnum() or "\u3040" <= character <= "\u9fff"]
    japanese = _japanese_character_count(compact)
    if len(compact) < len(members) * 2 or len(compact) > 48:
        return []
    if japanese < 2 or japanese / max(1, len(semantic)) < 0.55:
        return []

    median_width = sorted(max(1e-6, _number(member.get("width"))) for member in members)[len(members) // 2]
    aggregate_pitch = sum(_number(member.get("height")) for member in members) / max(1, len(compact))
    pitch_to_width = aggregate_pitch / max(1e-6, median_width)
    if not (0.30 <= pitch_to_width <= 1.55):
        return []

    ordered = sorted(
        members,
        key=lambda value: _number(value.get("x")) + _number(value.get("width")) / 2.0,
        reverse=True,
    )
    chunks = (
        _recognize_layout_cluster_members(model, image, compact, members)
        if model is not None
        else []
    )
    member_consensus = bool(chunks)
    split_selection = "layout-cluster-member-consensus-v3"
    # v17 still allowed a language/geometry-only split when independent member
    # OCR failed.  That recovered some text but also manufactured plausible
    # neighbouring phrases on p8/p13. Cluster OCR is now recall-only: every
    # emitted lane must be independently read from its own observed geometry.
    if len(chunks) != len(ordered):
        return []

    donors: list[dict[str, object]] = []
    for member, chunk in zip(ordered, chunks):
        if not chunk or _japanese_character_count(chunk) < 1:
            continue
        donor = dict(item)
        donor.update({key: member[key] for key in ("x", "y", "width", "height")})
        donor["text"] = chunk
        donor["raw_text"] = chunk
        donor["source"] = _LAYOUT_LINE_SOURCE
        donor["detector"] = _LAYOUT_CLUSTER_DETECTOR
        donor["recognition_selection"] = split_selection
        donor["selected_hypothesis_id"] = (
            "manga-ocr-layout-cluster-member-v3"
        )
        donor["hypotheses"] = [
            {
                "id": "manga-ocr-layout-cluster-member-v3",
                "text": chunk,
                "source": "manga-ocr",
                "selected": True,
            }
        ]
        donor_provenance = dict(provenance)
        donor_provenance["cluster_context_donor"] = True
        donor_provenance["cluster_member_consensus"] = member_consensus
        donor_provenance["cluster_pitch_to_width"] = round(pitch_to_width, 4)
        donor["provenance"] = donor_provenance
        segments = _layout_line_character_segments(donor, chunk, image=image)
        if segments:
            donor["segments"] = _tighten_vertical_slot_ink_segments(image, segments)
            donor["geometry_source"] = "layout-cluster-member-consensus-v3"
            donor["geometry_status"] = "approximate"
            donor["word_geometry"] = "proportional-single-column-v1"
        donors.append(donor)
    return donors


def _merge_layout_cluster_donors(regions: list[dict[str, object]]) -> list[dict[str, object]]:
    """Use cluster OCR only for genuinely missing lanes, never rewrite good lanes.

    v13 let a wider-context donor replace an already accepted line. That caused
    neighbouring text to leak into p8/p9/p11/p13/p16. v14 treats cluster OCR as
    recall-only: if any existing vertical region already owns the donor lane,
    the donor is discarded.
    """
    base: list[dict[str, object]] = []
    donors: list[dict[str, object]] = []
    for region in regions:
        provenance = region.get("provenance")
        if isinstance(provenance, dict) and bool(provenance.get("cluster_context_donor")):
            donors.append(dict(region))
        else:
            base.append(dict(region))

    for donor in donors:
        donor_center = _number(donor.get("x")) + _number(donor.get("width")) / 2.0
        covered = False
        for existing in base:
            if str(existing.get("orientation") or "") != "vertical":
                continue
            center = _number(existing.get("x")) + _number(existing.get("width")) / 2.0
            center_tolerance = max(0.012, max(_number(existing.get("width")), _number(donor.get("width"))) * 0.80)
            if abs(center - donor_center) <= center_tolerance and _layout_vertical_overlap(existing, donor) >= 0.42:
                covered = True
                break
            # A broad Vision region can legitimately own several columns. Block
            # cluster duplicates if that region already covers most of this lane.
            if _region_coverage(existing, donor) >= 0.68:
                covered = True
                break
        if not covered:
            base.append(donor)

    for order, region in enumerate(base):
        region["order"] = order
    return base


def _layout_vertical_overlap(left: dict[str, object], right: dict[str, object]) -> float:
    left_top = 1.0 - _number(left.get("y")) - _number(left.get("height"))
    left_bottom = left_top + _number(left.get("height"))
    right_top = 1.0 - _number(right.get("y")) - _number(right.get("height"))
    right_bottom = right_top + _number(right.get("height"))
    overlap = max(0.0, min(left_bottom, right_bottom) - max(left_top, right_top))
    return overlap / max(1e-9, min(_number(left.get("height")), _number(right.get("height"))))


def _ruby_like_layout_proposal(
    candidate: dict[str, object],
    proposals: list[dict[str, object]],
) -> bool:
    """Reject a tiny furigana lane immediately beside a stronger main column."""
    if str(candidate.get("detector") or "") != _LAYOUT_DETECTOR:
        return False
    provenance = candidate.get("provenance")
    if not isinstance(provenance, dict):
        return False
    width = _number(candidate.get("width"))
    height = _number(candidate.get("height"))
    black_ratio = float(provenance.get("black_ratio") or 0.0)
    component_count = int(provenance.get("component_count") or 0)
    if not (
        black_ratio < 0.09
        and width <= 0.0325
        and height <= 0.065
        and component_count <= 4
    ):
        return False
    center = _number(candidate.get("x")) + width / 2.0
    for peer in proposals:
        if peer is candidate:
            continue
        peer_width = _number(peer.get("width"))
        peer_height = _number(peer.get("height"))
        peer_center = _number(peer.get("x")) + peer_width / 2.0
        distance = abs(peer_center - center)
        if distance < 0.010 or distance > 0.036:
            continue
        if _layout_vertical_overlap(candidate, peer) < 0.55:
            continue
        peer_provenance = peer.get("provenance")
        if not isinstance(peer_provenance, dict):
            continue
        peer_black = float(peer_provenance.get("black_ratio") or 0.0)
        if (
            peer_width >= width * 1.15
            and peer_height >= height * 1.25
            and peer_black >= max(0.10, black_ratio * 1.35)
        ):
            return True
    return False


def _supported_short_layout_text(item: dict[str, object], value: object) -> bool:
    compact = _compact_surface(value)
    japanese = [
        character
        for character in compact
        if "\u3040" <= character <= "\u30ff" or "\u3400" <= character <= "\u9fff" or character in "々〆ヶ"
    ]
    if len(japanese) != 1 or len(compact) > 5:
        return False
    provenance = item.get("provenance")
    if not isinstance(provenance, dict):
        return False
    # A primary line with >=4 independently observed glyph blobs is enough
    # evidence for a one-kana utterance such as 「な…」.  Keep rejecting single
    # kanji/art hallucinations from short 2-3 component runs.
    if str(item.get("detector") or "") == _LAYOUT_DETECTOR:
        component_count = int(provenance.get("component_count") or 0)
        black_ratio = float(provenance.get("black_ratio") or 0.0)
        midtone_ratio = float(provenance.get("midtone_ratio") or 1.0)
        if (
            component_count >= 4
            and black_ratio >= 0.08
            and midtone_ratio <= 0.16
            and _number(item.get("height")) >= 0.045
        ):
            return True
        # One large kanji plus furigana/punctuation can be one connected blob.
        # Restrict this to kanji and to the explicit merged-component evidence;
        # single-kana model hallucinations remain rejected.
        if (
            bool(provenance.get("single_merged_component"))
            and len(japanese) == 1
            and _is_kanji_character(japanese[0])
            and black_ratio >= 0.08
            and midtone_ratio <= 0.20
            and 0.040 <= _number(item.get("height")) <= 0.095
            and _number(item.get("width")) <= 0.065
        ):
            return True
        return False
    if str(provenance.get("support_kind") or "") not in {"weak-region", "tiny-geometry-corroboration"}:
        return False
    if float(provenance.get("support_overlap") or 0.0) < 0.45:
        return False
    support_text = _compact_surface(provenance.get("support_text"))
    return japanese[0] in support_text

def _region_segment_sources(region: dict[str, object]) -> set[str]:
    return {
        str(segment.get("source") or "")
        for segment in region.get("segments") or []
        if isinstance(segment, dict)
    }


def _region_geometry_trust(region: dict[str, object]) -> str:
    if str(region.get("source") or "") == "dark-block-proposal":
        return "protected"
    sources = _region_segment_sources(region)
    if sources and all(
        source.startswith(("vision-accurate-range", "vision-accurate-ink"))
        for source in sources
        if source
    ):
        if all(source for source in sources):
            return "observed"
    if (
        str(region.get("source") or "") in {"expanded-vision-rectangle", "expanded-vertical-seed"}
        or not sources
        or "" in sources
        or any(source in _SYNTHETIC_SEGMENT_SOURCES for source in sources)
    ):
        return "weak"
    return "mixed"


def _layout_recovery_enabled(
    regions: list[dict[str, object]],
    primary: list[dict[str, object]] | None = None,
) -> bool:
    vertical = sum(str(region.get("orientation") or "") == "vertical" for region in regions)
    horizontal = sum(str(region.get("orientation") or "") == "horizontal" for region in regions)

    strong: list[dict[str, object]] = []
    if primary:
        for proposal in primary:
            width = max(1e-9, _number(proposal.get("width")))
            height = _number(proposal.get("height"))
            if height >= 0.055 and height / width >= 2.0:
                strong.append(proposal)
    centers = [
        _number(proposal.get("x")) + _number(proposal.get("width")) / 2.0
        for proposal in strong
    ]
    strong_span = max(centers) - min(centers) if len(centers) >= 2 else 0.0

    # Sparse horizontal document/title layouts can have only a few Vision
    # observations, so the older "horizontal >= 6" TOC guard never ran.  The
    # One Piece contents page is exactly that shape: three horizontal Vision
    # regions, one giant block covering the table of contents, and pixel lanes
    # confined to a compact central band.  Block this geometry without relying
    # on language-specific text such as "CONTENTS".
    large_horizontal_block = any(
        str(region.get("orientation") or "") == "horizontal"
        and _number(region.get("width")) >= 0.65
        and _number(region.get("height")) >= 0.20
        for region in regions
    )
    if (
        1 <= len(regions) <= 4
        and horizontal == len(regions)
        and large_horizontal_block
        and len(strong) >= 4
        and strong_span <= 0.55
    ):
        return False

    # Sparse splash/title pages can expose one giant horizontal observation
    # while pixel components inside the artwork resemble short vertical text.
    # Do not manufacture vertical dialogue inside that already-observed title.
    giant_horizontal_title = any(
        str(region.get("orientation") or "") == "horizontal"
        and _number(region.get("width")) >= 0.75
        and _number(region.get("height")) >= 0.14
        for region in regions
    )
    if 1 <= len(regions) <= 3 and horizontal == len(regions) and giant_horizontal_title:
        return False

    # Protect conventional TOC/title layouts with several long, thin rows.
    wide_horizontal_rows = sum(
        _number(region.get("width")) >= 0.28
        and _number(region.get("height")) <= 0.09
        and _number(region.get("width"))
        / max(1e-9, _number(region.get("height")))
        >= 4.0
        for region in regions
    )
    if wide_horizontal_rows >= 4:
        return False

    # Apple Vision can misclassify a normal vertical manga page as almost
    # entirely horizontal (One Piece p009: 14 horizontal / 1 vertical). Allow
    # recovery only when the pixel detector independently sees many tall
    # vertical lanes spread across most of the page.
    if not (horizontal >= 6 and vertical <= 2):
        return True
    if len(strong) < 6:
        return False
    return strong_span >= 0.68


def _horizontal_observation_blocks_layout(
    existing: dict[str, object],
    proposal: dict[str, object],
) -> bool:
    """Reject vertical comb artifacts cut out of an exact horizontal text row."""
    if str(existing.get("orientation") or "") != "horizontal":
        return False
    if str(proposal.get("orientation") or "") != "vertical":
        return False
    if _number(proposal.get("height")) > 0.075:
        return False
    exact = [
        segment
        for segment in existing.get("segments") or []
        if isinstance(segment, dict)
        and str(segment.get("source") or "").startswith(
            ("vision-accurate-range", "vision-accurate-ink")
        )
        and str(segment.get("orientation") or "horizontal") == "horizontal"
        and str(segment.get("text") or "").strip()
    ]
    if len(exact) < 8 and not _coherent_horizontal_multiline_exact_observation(existing):
        return False
    # A proposal almost wholly inside a densely observed horizontal row is
    # usually 3-4 adjacent glyphs incorrectly linked into a vertical lane.
    # Compact multi-row captions are allowed to use the same protection with
    # fewer total glyphs because their row structure is independently observed.
    return _region_coverage(existing, proposal) >= 0.72


def _observed_geometry_blocks_layout(
    existing: dict[str, object],
    proposal: dict[str, object],
) -> bool:
    """Return True only when Vision already owns the same tight vertical lane.

    Exact Vision character geometry is valuable, but an exact *horizontal* strip
    or a wide multi-column rectangle must not suppress a tighter pixel-observed
    manga column merely because that column lies inside it.
    """
    if _region_geometry_trust(existing) != "observed":
        return False
    if str(existing.get("orientation") or "") != "vertical":
        return False
    if str(proposal.get("orientation") or "") != "vertical":
        return False
    ew = max(1e-9, _number(existing.get("width")))
    eh = max(1e-9, _number(existing.get("height")))
    pw = max(1e-9, _number(proposal.get("width")))
    ph = max(1e-9, _number(proposal.get("height")))
    # A wide Vision region spanning several manga columns is not the same lane.
    if ew > pw * 2.35 or eh > ph * 2.10:
        return False
    ecx = _number(existing.get("x")) + ew / 2.0
    pcx = _number(proposal.get("x")) + pw / 2.0
    if abs(ecx - pcx) > max(ew, pw) * 0.85:
        return False
    return _layout_overlap(existing, proposal) >= 0.72


def _attach_partial_weak_raw_component_support(
    image: Image.Image,
    regions: list[dict[str, object]],
    primary: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Corroborate merged-component layout lanes with an independent raw pass.

    A tight source-less Vision fragment can cover only the tail of a real vertical
    line while the pixel detector sees the full lane.  The primary ink detector
    sometimes merges touching glyphs into only 2-3 components; the independent
    raw connected-component pass can still count the full stack.  Carry that raw
    count into geometry validation only when all three signals agree on one lane:
    a short weak Vision fragment, a primary proposal, and a near-identical raw
    proposal.  Expanded detector rectangles are deliberately excluded.
    """
    page_width, page_height = image.size
    if page_width <= 0 or page_height <= 0 or not primary:
        return primary
    raw_lines = _raw_vertical_lines(image)
    if not raw_lines:
        return primary

    output: list[dict[str, object]] = []
    for proposal in primary:
        item = dict(proposal)
        pbox = _pixel_bbox(item, page_width, page_height)
        pwidth = max(1.0, pbox[2] - pbox[0])
        pheight = max(1.0, pbox[3] - pbox[1])
        pcenter = (pbox[0] + pbox[2]) / 2.0

        weak_fragment = False
        for region in regions:
            if str(region.get("source") or ""):
                continue
            if _region_geometry_trust(region) != "weak":
                continue
            rbox = _pixel_bbox(region, page_width, page_height)
            rwidth = max(1.0, rbox[2] - rbox[0])
            rheight = max(1.0, rbox[3] - rbox[1])
            # Vision can label a tiny tail fragment as horizontal even when it
            # sits inside a real vertical manga lane.  Its normalized width/height
            # is especially misleading on portrait pages: p14's ``れは`` seed is
            # ~24x28 px but arrives as orientation=horizontal.  Admit only compact
            # non-horizontal pixel boxes here; a genuine horizontal strip remains
            # ineligible and all same-lane/raw-component checks below still apply.
            if (
                str(region.get("orientation") or "") != "vertical"
                and rwidth > rheight * 1.35
            ):
                continue
            rcenter = (rbox[0] + rbox[2]) / 2.0
            if rheight > pheight * 0.48:
                continue
            if abs(rcenter - pcenter) > max(4.0, pwidth * 0.50):
                continue
            if _pixel_cover(rbox, pbox) < 0.55:
                continue
            weak_fragment = True
            break
        if not weak_fragment:
            output.append(item)
            continue

        best: tuple[float, dict[str, object]] | None = None
        for raw in raw_lines:
            rbox = _pixel_bbox(raw, page_width, page_height)
            rcenter = (rbox[0] + rbox[2]) / 2.0
            if abs(rcenter - pcenter) > max(4.0, pwidth * 0.28):
                continue
            intersection = max(0.0, min(pbox[2], rbox[2]) - max(pbox[0], rbox[0])) * max(
                0.0, min(pbox[3], rbox[3]) - max(pbox[1], rbox[1])
            )
            parea = max(1.0, (pbox[2] - pbox[0]) * (pbox[3] - pbox[1]))
            rarea = max(1.0, (rbox[2] - rbox[0]) * (rbox[3] - rbox[1]))
            pcover = intersection / parea
            rcover = intersection / rarea
            if pcover < 0.82 or rcover < 0.82:
                continue
            provenance = raw.get("provenance")
            if not isinstance(provenance, dict):
                continue
            raw_count = int(provenance.get("component_count") or 0)
            raw_coverage = float(provenance.get("component_coverage") or 0.0)
            proposal_provenance = item.get("provenance")
            proposal_count = (
                int(proposal_provenance.get("component_count") or 0)
                if isinstance(proposal_provenance, dict)
                else 0
            )
            if raw_count < max(5, proposal_count + 2) or raw_coverage < 0.82:
                continue
            score = pcover + rcover + min(0.25, raw_count * 0.01)
            if best is None or score > best[0]:
                best = (score, raw)

        if best is not None:
            raw = best[1]
            raw_provenance = raw.get("provenance") or {}
            provenance = dict(item.get("provenance") or {})
            provenance["raw_component_count_support"] = int(raw_provenance.get("component_count") or 0)
            provenance["raw_component_coverage_support"] = round(
                float(raw_provenance.get("component_coverage") or 0.0), 4
            )
            provenance["raw_component_support_kind"] = "partial-weak-same-lane-v1"
            item["provenance"] = provenance
        output.append(item)
    return output


def _manga_layout_line_proposals(
    image: Image.Image,
    regions: list[dict[str, object]],
) -> list[dict[str, object]]:
    primary = _attach_partial_weak_raw_component_support(
        image, regions, _layout_vertical_lines(image)
    )
    if not _layout_recovery_enabled(regions, primary):
        return []
    raw_supplement = _supplemental_raw_layout_lines(image, regions, primary)
    contextual = _contextual_missing_layout_lines(image, regions, [*primary, *raw_supplement])
    proposals: list[dict[str, object]] = []
    for proposal in [*primary, *raw_supplement, *contextual]:
        blocked = False
        for existing in regions:
            if _horizontal_observation_blocks_layout(existing, proposal):
                blocked = True
                break
            trust = _region_geometry_trust(existing)
            if trust == "protected" and (
                _region_coverage(existing, proposal) >= 0.35
                or _region_coverage(proposal, existing) >= 0.35
            ):
                blocked = True
                break
            if trust == "observed" and _observed_geometry_blocks_layout(existing, proposal):
                blocked = True
                break
        if not blocked:
            proposals.append(proposal)
    proposals = [
        proposal
        for proposal in proposals
        if not _ruby_like_layout_proposal(proposal, proposals)
    ]
    return proposals


def _compact_surface(value: object) -> str:
    return "".join(character for character in str(value or "") if not character.isspace())


def _japanese_character_count(value: object) -> int:
    return sum(
        "\u3040" <= character <= "\u30ff"
        or "\u3400" <= character <= "\u9fff"
        or character in "々〆ヶ"
        for character in _compact_surface(value)
    )


def _layout_text_usable(value: object) -> bool:
    compact = _compact_surface(value)
    if len(compact) < 2:
        return False
    japanese = _japanese_character_count(compact)
    # MangaOCR can hallucinate plausible Japanese on art, so require multiple
    # Japanese glyphs and a meaningful share of the hypothesis.
    semantic = [character for character in compact if character.isalnum() or "\u3040" <= character <= "\u9fff"]
    return japanese >= 2 and japanese / max(1, len(semantic)) >= 0.42


def _exact_vision_segment_surface(item: dict[str, object]) -> str:
    segments = [
        segment
        for segment in item.get("segments") or []
        if isinstance(segment, dict)
        and str(segment.get("source") or "") == "vision-accurate-range-v2"
        and str(segment.get("text") or "").strip()
    ]
    if len(segments) < 4:
        return ""
    return _normalize_line_surface("".join(str(segment.get("text") or "") for segment in segments))



_SFX_EXTENSION_PUNCTUATION = frozenset("!！?？…・、。〜～ー")
_SFX_EXTENSION_SMALL_KANA = frozenset("ぁぃぅぇぉゃゅょっゎァィゥェォャュョッヮ")


def _overlapping_duplicate_vision_seed(item: dict[str, object]) -> str:
    """Return the one-glyph Vision seed when duplicate exact boxes overlap.

    Stylized manga SFX can be observed twice by Vision at almost the same
    geometry while the trailing small kana/punctuation falls just outside the
    enclosing region. This deliberately rejects ordinary multi-glyph text.
    """
    if str(item.get("orientation") or "") != "horizontal":
        return ""
    if _number(item.get("confidence"), 1.0) > 0.65:
        return ""
    segments = [
        dict(segment)
        for segment in item.get("segments") or []
        if isinstance(segment, dict)
        and str(segment.get("source") or "") == "vision-accurate-range-v2"
        and _normalize_line_surface(segment.get("text"))
    ]
    if not 2 <= len(segments) <= 3:
        return ""
    surfaces = [_normalize_line_surface(segment.get("text")) for segment in segments]
    if len(set(surfaces)) != 1 or len(surfaces[0]) != 1:
        return ""
    seed = surfaces[0]
    if _japanese_character_count(seed) != 1:
        return ""
    strong_pair = any(
        max(
            _region_coverage(left, right),
            _region_coverage(right, left),
        ) >= 0.95
        and min(
            _region_coverage(left, right),
            _region_coverage(right, left),
        ) >= 0.60
        for index, left in enumerate(segments)
        for right in segments[index + 1 :]
    )
    return seed if strong_pair else ""


def _horizontal_right_context_crop(
    image: Image.Image,
    item: dict[str, object],
    *,
    right_factor: float,
) -> tuple[Image.Image, dict[str, float]] | tuple[None, None]:
    x = _number(item.get("x"))
    y = _number(item.get("y"))
    width = _number(item.get("width"))
    height = _number(item.get("height"))
    if width <= 0 or height <= 0:
        return None, None
    right = x + width * (1.0 + right_factor)
    if right >= 0.995:
        return None, None

    # This helper already defines its recognition-only context explicitly.
    # Do not route it through _crop_region(): that function adds the generic
    # OCR padding again. On the real p08 SFX this double-padding changed the
    # trace-matched +96px crop from `きっ！` to a wider/noisier `きっ、`.
    image_width, image_height = image.size
    base_left = math.floor(x * image_width)
    base_right = math.ceil((x + width) * image_width)
    base_top = math.floor((1.0 - y - height) * image_height)
    base_bottom = math.ceil((1.0 - y) * image_height)
    left_pad_px = max(1, round(width * image_width * 0.05))
    vertical_pad_px = max(1, round(height * image_height * 0.04))
    right_extra_px = max(1, round(width * image_width * right_factor))
    left_px = max(0, base_left - left_pad_px)
    right_px = min(image_width, base_right + right_extra_px)
    top_px = max(0, base_top - vertical_pad_px)
    bottom_px = min(image_height, base_bottom + vertical_pad_px)
    if right_px <= left_px or bottom_px <= top_px:
        return None, None
    crop = image.crop((left_px, top_px, right_px, bottom_px)).convert("RGB")
    geometry = {
        "x": x,
        "y": y,
        "width": min(1.0 - x, width * (1.0 + right_factor)),
        "height": height,
    }
    return crop, geometry


def _extension_collides_with_peer(
    item: dict[str, object],
    peers: list[dict[str, object]],
    expanded: dict[str, float],
) -> bool:
    original_right = _number(item.get("x")) + _number(item.get("width"))
    extension = {
        "x": original_right,
        "y": expanded["y"],
        "width": max(0.0, expanded["x"] + expanded["width"] - original_right),
        "height": expanded["height"],
    }
    if _region_area(extension) <= 0:
        return True
    for peer in peers:
        if peer is item:
            continue
        if (
            abs(_number(peer.get("x")) - _number(item.get("x"))) <= 1e-9
            and abs(_number(peer.get("y")) - _number(item.get("y"))) <= 1e-9
            and abs(_number(peer.get("width")) - _number(item.get("width"))) <= 1e-9
            and abs(_number(peer.get("height")) - _number(item.get("height"))) <= 1e-9
        ):
            continue
        if not str(peer.get("text") or "").strip():
            continue
        if (
            _region_coverage(extension, peer) >= 0.24
            or _region_coverage(peer, extension) >= 0.34
        ):
            return True
    return False


def _sfx_extension_segments(
    item: dict[str, object],
    surface: str,
    expanded: dict[str, float],
) -> list[dict[str, object]]:
    seed_segments = [
        dict(segment)
        for segment in item.get("segments") or []
        if isinstance(segment, dict)
        and str(segment.get("source") or "") == "vision-accurate-range-v2"
    ]
    if not seed_segments or not surface:
        return []
    first = max(seed_segments, key=_region_area)
    first["text"] = surface[0]
    first["orientation"] = "horizontal"
    output = [first]
    remainder = list(surface[1:])
    if not remainder:
        return output
    extension_left = _number(item.get("x")) + _number(item.get("width"))
    extension_right = expanded["x"] + expanded["width"]
    available = max(1e-6, extension_right - extension_left)
    slot = available / len(remainder)
    for index, character in enumerate(remainder):
        output.append(
            {
                "text": character,
                "orientation": "horizontal",
                "x": round(extension_left + slot * index, 6),
                "y": round(expanded["y"], 6),
                "width": round(slot, 6),
                "height": round(expanded["height"], 6),
                "source": "clipped-horizontal-sfx-extension-v1",
                "geometry_status": "approximate",
            }
        )
    return output


def _recover_clipped_horizontal_sfx(
    model: object,
    image: Image.Image,
    item: dict[str, object],
    peers: list[dict[str, object]],
) -> dict[str, object]:
    seed = _overlapping_duplicate_vision_seed(item)
    if not seed:
        return item
    baseline = _compact_surface(item.get("text"))
    raw = _compact_surface(item.get("raw_text"))
    if len(baseline) > 3 or len(raw) > 3:
        return item

    def stable_surface(right_factor: float) -> tuple[str, dict[str, float] | None]:
        crop, geometry = _horizontal_right_context_crop(
            image, item, right_factor=right_factor
        )
        if crop is None or geometry is None:
            return "", None
        try:
            direct = _compact_surface(str(model(crop) or "").strip())  # type: ignore[operator]
            auto = ImageOps.autocontrast(crop.convert("L")).convert("RGB")
            try:
                contrast = _compact_surface(str(model(auto) or "").strip())  # type: ignore[operator]
            finally:
                auto.close()
        finally:
            crop.close()
        if not direct or direct != contrast:
            return "", None
        return direct, geometry

    middle, middle_geometry = stable_surface(0.70)
    if middle_geometry is None:
        return item
    wide, wide_geometry = stable_surface(1.18)
    if wide_geometry is None:
        return item
    if _extension_collides_with_peer(item, peers, wide_geometry):
        return item

    if not (2 <= len(middle) <= 4 and 2 <= len(wide) <= 5):
        return item
    if not middle.startswith(seed):
        return item
    if not any(
        character in _SFX_EXTENSION_SMALL_KANA or character in _SFX_EXTENSION_PUNCTUATION
        for character in middle[1:]
    ):
        return item
    if wide == middle:
        selected = middle
        selected_geometry = middle_geometry
    elif wide.startswith(middle):
        suffix = wide[len(middle) :]
        if not suffix or any(character not in _SFX_EXTENSION_PUNCTUATION for character in suffix):
            return item
        selected = wide
        selected_geometry = wide_geometry
    else:
        return item

    result = dict(item)
    result.update(selected_geometry)
    result["text"] = selected
    result["segments"] = _sfx_extension_segments(item, selected, selected_geometry)
    result["geometry_status"] = "approximate"
    result["geometry_source"] = "vision-accurate-range-v2+clipped-horizontal-sfx-extension-v1"
    result["recognition_selection"] = "clipped-horizontal-sfx-right-context-v1"
    result["recognizer_retry"] = "horizontal-right-context-v1"
    result["clipped_horizontal_sfx_recovery"] = True
    result["clipped_horizontal_sfx_seed"] = seed
    result["clipped_horizontal_sfx_middle_text"] = middle
    result["clipped_horizontal_sfx_wide_text"] = wide
    result["clipped_horizontal_sfx_right_factor"] = (
        0.70 if selected_geometry is middle_geometry else 1.18
    )
    hypotheses = [
        dict(hypothesis)
        for hypothesis in result.get("hypotheses") or []
        if isinstance(hypothesis, dict)
    ]
    for hypothesis in hypotheses:
        hypothesis["selected"] = False
    hypotheses.extend(
        [
            {
                "id": "manga-ocr-right-context-sfx-middle",
                "text": middle,
                "source": "manga-ocr",
                "selected": False,
            },
            {
                "id": "manga-ocr-right-context-sfx",
                "text": selected,
                "source": "manga-ocr",
                "selected": True,
            },
        ]
    )
    result["hypotheses"] = hypotheses
    result["selected_hypothesis_id"] = "manga-ocr-right-context-sfx"
    return result


def _observed_segment_surface(item: dict[str, object]) -> str:
    """Return the non-empty observed segment stream regardless of Vision source.

    Some Apple Vision regions expose useful per-glyph labels without the exact
    `vision-accurate-range-v2` source tag.  They are still strong evidence when
    their joined surface agrees with the detector text and MangaOCR expands the
    same tiny geometry into a much longer phrase.
    """
    parts: list[str] = []
    for segment in item.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        value = _normalize_line_surface(segment.get("text"))
        if value:
            parts.append(value)
    return _normalize_line_surface("".join(parts))


def _prefer_detector_recognition(item: dict[str, object], manga_text: str) -> bool:
    """Prefer detector/segment evidence when full-crop MangaOCR clearly degrades it.

    Besides horizontal title rows, a small vertical/SFX region can have a short
    detector surface backed by observed glyph boxes while MangaOCR hallucinates
    a long grammatical phrase from surrounding artwork.  In that case keep the
    geometry-backed detector reading instead of rewarding linguistic plausibility.
    """
    orientation = str(item.get("orientation") or "")
    if orientation not in {"horizontal", "vertical"}:
        return False
    raw = str(item.get("raw_text") or "").strip()
    if not raw:
        return False
    raw_japanese = _japanese_character_count(raw)
    manga_japanese = _japanese_character_count(manga_text)
    confidence = _number(item.get("confidence"))

    exact_surface = _exact_vision_segment_surface(item)
    observed_surface = _observed_segment_surface(item)
    normalized_raw = _normalize_line_surface(raw)
    normalized_manga = _normalize_line_surface(manga_text)

    if orientation == "vertical" and observed_surface:
        raw_segment_similarity = difflib.SequenceMatcher(
            a=normalized_raw, b=observed_surface, autojunk=False
        ).ratio()
        manga_segment_similarity = difflib.SequenceMatcher(
            a=normalized_manga, b=observed_surface, autojunk=False
        ).ratio()
        if (
            raw_segment_similarity >= 0.68
            and manga_segment_similarity <= 0.42
            and len(normalized_manga) >= len(normalized_raw) + 3
        ):
            return True

    if orientation != "horizontal":
        return False
    latin_count = sum(character.isascii() and character.isalpha() for character in normalized_raw)
    if (
        exact_surface
        and exact_surface == normalized_raw
        and confidence >= 0.75
        and raw_japanese == 0
        and latin_count >= 4
    ):
        # Strong Accurate Vision geometry on an all-Latin masthead/logo is
        # better evidence than MangaOCR inventing a stray Japanese suffix.
        return True
    if exact_surface and confidence >= 0.45 and raw_japanese >= 2:
        exact_matches_raw = exact_surface == normalized_raw
        # Mixed Latin/Japanese title rows often differ only in a dash glyph
        # (ASCII '-' vs CJK '一').  Treat that as near-exact geometry evidence
        # when the Latin masthead is substantial; this lets detector+segments
        # beat a clearly degraded full-crop MangaOCR without weakening the
        # normal Japanese-text path.
        latin_heavy_near_exact = False
        if latin_count >= 6:
            def _title_surface_key(value: str) -> str:
                return "".join(
                    character
                    for character in value
                    if character.isalnum()
                    or "\u3040" <= character <= "\u30ff"
                    or "\u3400" <= character <= "\u9fff"
                    or character in "々〆ヶ"
                )

            raw_key = _title_surface_key(normalized_raw)
            exact_key = _title_surface_key(exact_surface)
            if raw_key and exact_key:
                latin_heavy_near_exact = (
                    difflib.SequenceMatcher(
                        a=raw_key, b=exact_key, autojunk=False
                    ).ratio()
                    >= 0.94
                )
        if exact_matches_raw or latin_heavy_near_exact:
            similarity = difflib.SequenceMatcher(
                a=normalized_raw,
                b=normalized_manga,
                autojunk=False,
            ).ratio()
            if similarity < 0.78:
                return True

    if raw_japanese < 4:
        return False
    return (
        manga_japanese < max(2, int(math.ceil(raw_japanese * 0.55)))
        or (confidence >= 0.45 and raw_japanese >= manga_japanese + 4)
    )


def _recognition_hypotheses(
    item: dict[str, object],
    manga_text: str,
    *,
    selected_id: str = "manga-ocr",
) -> None:
    raw_text = str(item.get("raw_text") or "").strip()
    hypotheses: list[dict[str, object]] = []
    if raw_text:
        hypotheses.append(
            {
                "id": "detector-recognition",
                "text": raw_text,
                "source": str(item.get("detector") or "apple-vision"),
                "selected": selected_id == "detector-recognition",
            }
        )
    hypotheses.append(
        {
            "id": "manga-ocr",
            "text": manga_text,
            "source": "manga-ocr",
            "selected": selected_id == "manga-ocr",
        }
    )
    item["hypotheses"] = hypotheses
    item["selected_hypothesis_id"] = selected_id
    observations = item.get("observations")
    if not isinstance(observations, list):
        observations = []
    if raw_text:
        observations = [
            *observations,
            {
                "kind": "recognition",
                "source": str(item.get("detector") or "apple-vision"),
                "text": raw_text,
            },
        ]
    item["observations"] = observations


def _non_japanese_latin_noise(item: dict[str, object]) -> bool:
    """Drop detector-backed Latin labels/logos, not generic recognizer output."""
    # _recognize_regions is also a public internal contract used by callers/tests
    # with neutral geometry-only regions.  Those inputs have no OCR detector
    # provenance, so do not infer that arbitrary Latin model output is artwork.
    detector = str(item.get("detector") or "").strip()
    source = str(item.get("source") or "").strip()
    if not detector and not source:
        return False
    text = unicodedata.normalize("NFKC", _compact_surface(item.get("text")))
    raw = unicodedata.normalize("NFKC", _compact_surface(item.get("raw_text")))
    if _japanese_character_count(text) or _japanese_character_count(raw):
        return False
    latin = sum("A" <= character.upper() <= "Z" for character in text)
    raw_latin = sum("A" <= character.upper() <= "Z" for character in raw)
    if (
        str(item.get("selected_hypothesis_id") or "") == "detector-recognition"
        and _number(item.get("confidence"), 0.0) >= 0.75
        and raw_latin >= 4
        and _exact_vision_segment_surface(item) == _normalize_line_surface(raw)
    ):
        return False
    return max(latin, raw_latin) >= 1



def _semantic_empty_noise(item: dict[str, object]) -> bool:
    """Punctuation-only OCR has no study surface and should not become a hitbox."""
    text = unicodedata.normalize("NFKC", _compact_surface(item.get("text")))
    if not text:
        return True
    return not any(character.isalnum() or _japanese_character_count(character) for character in text)


def _detector_script_conflict_noise(item: dict[str, object]) -> bool:
    """Drop low-confidence artwork/logos where MangaOCR invents unrelated Japanese."""
    if str(item.get("source") or "") in {_LAYOUT_LINE_SOURCE, "dark-block-proposal"}:
        return False
    raw = unicodedata.normalize("NFKC", _compact_surface(item.get("raw_text")))
    text = unicodedata.normalize("NFKC", _compact_surface(item.get("text")))
    if not raw or _japanese_character_count(text) < 1:
        return False
    ascii_signal = sum(character.isascii() and character.isalnum() for character in raw)
    raw_japanese = _japanese_character_count(raw)
    text_japanese = _japanese_character_count(text)
    confidence = _number(item.get("confidence"), 1.0)
    if raw_japanese == 0:
        return ascii_signal >= 1 and confidence <= 0.55
    # Mixed logo/poster text is another common failure mode: Vision sees a
    # strong Latin word plus one or two decorative kana, while MangaOCR turns
    # the artwork into a fluent Japanese sentence (p019 WANTED poster). Keep
    # mixed real title lines when their Japanese share is substantial.
    return (
        str(item.get("selected_hypothesis_id") or "") == "manga-ocr"
        and ascii_signal >= 4
        and text_japanese >= 4
        and raw_japanese <= max(2, text_japanese // 3)
        and confidence <= 0.60
    )



def _suppress_nested_ruby_echo_layout_regions(
    regions: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Drop tiny nested vertical layout lanes that are really furigana/art echoes.

    On One Piece p004, MangaOCR correctly reads 財宝か？ in the main column, but
    a second narrow lane carved out of the ruby area produces a fake adjacent
    region 才臣ハ.  That region is not a real neighbour: its bbox is almost
    completely contained inside the stronger main lane.

    Suppress only that nested shape:
    - both regions must be layout-line vertical proposals
    - the candidate must be narrow/short
    - a stronger peer must cover most of the candidate
    - the peer must be clearly wider/taller and have stronger component coverage

    This is intentionally stricter than the old ruby-proposal rejector, because
    it runs after OCR and should never delete normal neighbouring dialogue lines.
    """
    suppressed: set[int] = set()
    for index, candidate in enumerate(regions):
        if str(candidate.get("source") or "") != _LAYOUT_LINE_SOURCE:
            continue
        if str(candidate.get("orientation") or "") != "vertical":
            continue
        provenance = candidate.get("provenance")
        if not isinstance(provenance, dict):
            continue
        width = _number(candidate.get("width"))
        height = _number(candidate.get("height"))
        coverage = float(provenance.get("component_coverage") or 0.0)
        black_ratio = float(provenance.get("black_ratio") or 0.0)
        if not (
            width <= 0.0325
            and height <= 0.075
            and coverage <= 0.76
            and black_ratio <= 0.19
        ):
            continue

        for peer_index, peer in enumerate(regions):
            if peer_index == index:
                continue
            if str(peer.get("source") or "") != _LAYOUT_LINE_SOURCE:
                continue
            if str(peer.get("orientation") or "") != "vertical":
                continue
            peer_provenance = peer.get("provenance")
            if not isinstance(peer_provenance, dict):
                continue

            peer_width = _number(peer.get("width"))
            peer_height = _number(peer.get("height"))
            peer_coverage = float(peer_provenance.get("component_coverage") or 0.0)

            if _region_coverage(peer, candidate) < 0.84:
                continue
            if _layout_vertical_overlap(candidate, peer) < 0.72:
                continue
            if peer_width < width * 1.35 or peer_height < height * 1.15:
                continue
            if peer_coverage < max(0.84, coverage + 0.15):
                continue
            suppressed.add(index)
            break

    if not suppressed:
        return regions
    return [item for index, item in enumerate(regions) if index not in suppressed]


def _layout_overlap(left: dict[str, object], right: dict[str, object]) -> float:
    return max(_region_coverage(left, right), _region_coverage(right, left))


def _merge_layout_recovery_regions(regions: list[dict[str, object]]) -> list[dict[str, object]]:
    base = [dict(region) for region in regions if str(region.get("source") or "") != _LAYOUT_LINE_SOURCE]
    layout = [dict(region) for region in regions if str(region.get("source") or "") == _LAYOUT_LINE_SOURCE]
    drop: set[int] = set()
    accepted: list[dict[str, object]] = []
    for proposal in layout:
        skip = False
        for index, existing in enumerate(base):
            overlap = _layout_overlap(existing, proposal)
            if overlap < 0.42:
                continue
            trust = _region_geometry_trust(existing)
            if trust == "protected" and overlap >= 0.72:
                skip = True
                break
            if trust == "observed" and _observed_geometry_blocks_layout(existing, proposal):
                skip = True
                break
            if trust == "weak" and overlap >= 0.48:
                drop.add(index)
        if not skip:
            accepted.append(proposal)

    # A weak Vision rectangle can cover several real manga columns.  No single
    # tight layout lane then reaches the old 0.48 replacement threshold even
    # though the set of independent lanes clearly explains the rectangle.
    for index, existing in enumerate(base):
        if index in drop or _region_geometry_trust(existing) != "weak":
            continue
        if _number(existing.get("confidence"), 1.0) > 0.35:
            continue
        overlaps = sorted(
            (_layout_overlap(existing, proposal) for proposal in accepted),
            reverse=True,
        )
        if not overlaps:
            continue
        corroborating = sum(overlap >= 0.26 for overlap in overlaps)
        if overlaps[0] >= 0.44 or corroborating >= 2:
            drop.add(index)

    merged = [region for index, region in enumerate(base) if index not in drop]
    merged.extend(accepted)
    for order, region in enumerate(merged):
        region["order"] = order
    return merged


def _contiguous_spans(values: list[bool]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for index, active in enumerate(values):
        if active and start is None:
            start = index
        elif not active and start is not None:
            spans.append((start, index - 1))
            start = None
    if start is not None:
        spans.append((start, len(values) - 1))
    return spans



def _remove_large_ink_components(ink: bytearray, width: int, height: int) -> bytearray:
    """Strip obvious bubble/panel borders without touching normal glyph blobs."""
    if width <= 0 or height <= 0 or not ink:
        return bytearray(ink)
    cleaned = bytearray(ink)
    seen = bytearray(width * height)
    for start in range(width * height):
        if not cleaned[start] or seen[start]:
            continue
        stack = [start]
        seen[start] = 1
        component: list[int] = []
        min_x, min_y = width, height
        max_x = max_y = -1
        while stack:
            index = stack.pop()
            component.append(index)
            row, column = divmod(index, width)
            min_x = min(min_x, column)
            max_x = max(max_x, column)
            min_y = min(min_y, row)
            max_y = max(max_y, row)
            if column > 0:
                nxt = index - 1
                if cleaned[nxt] and not seen[nxt]:
                    seen[nxt] = 1
                    stack.append(nxt)
            if column + 1 < width:
                nxt = index + 1
                if cleaned[nxt] and not seen[nxt]:
                    seen[nxt] = 1
                    stack.append(nxt)
            if row > 0:
                nxt = index - width
                if cleaned[nxt] and not seen[nxt]:
                    seen[nxt] = 1
                    stack.append(nxt)
            if row + 1 < height:
                nxt = index + width
                if cleaned[nxt] and not seen[nxt]:
                    seen[nxt] = 1
                    stack.append(nxt)
        component_width = max_x - min_x + 1
        component_height = max_y - min_y + 1
        area = len(component)
        touches_edge = min_x == 0 or min_y == 0 or max_x == width - 1 or max_y == height - 1
        border_like = (
            (component_width > width * 0.30 and component_height > height * 0.18)
            or component_height > height * 0.50
            or area > width * height * 0.035
            or (touches_edge and (component_width > width * 0.18 or component_height > height * 0.18))
        )
        if border_like:
            for index in component:
                cleaned[index] = 0
    return cleaned


def _merge_small_gaps(spans: list[tuple[int, int]], max_gap: int) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in spans:
        if not merged or start - merged[-1][1] - 1 > max_gap:
            merged.append([start, end])
        else:
            merged[-1][1] = end
    return [(start, end) for start, end in merged]



def _select_dark_block_column_peaks(
    candidates: list[int],
    smoothed: list[float],
    crop_width: int,
) -> list[int]:
    """Keep one projection peak per real dark-block text column.

    Wide printed kanji can create two strong x-projection maxima inside the
    same glyph lane. The older 7% separation treated those stroke clusters as
    separate columns and shifted OCR text into a phantom middle lane.
    """
    separation = max(8, int(round(crop_width * 0.10)))
    peaks: list[int] = []
    for column in sorted(candidates, key=lambda item: smoothed[item], reverse=True):
        if all(abs(column - other) >= separation for other in peaks):
            peaks.append(column)
    peaks.sort(reverse=True)
    return peaks


def _rebalance_dark_column_line_starts(compact: str, counts: list[int]) -> list[int]:
    """Keep a katakana prolonged sound mark with its preceding character."""
    adjusted = list(counts)
    character_offset = 0
    for index in range(len(adjusted) - 1):
        character_offset += adjusted[index]
        if (
            character_offset < len(compact)
            and compact[character_offset] == "ー"
            and "ァ" <= compact[character_offset - 1] <= "ヿ"
            and adjusted[index] > 1
        ):
            adjusted[index] -= 1
            adjusted[index + 1] += 1
            character_offset -= 1
    return adjusted


def _expand_dark_column_weak_ink(
    row_counts: list[int],
    start_y: int,
    end_y: int,
    *,
    margin_y: int,
) -> tuple[int, int]:
    """Include thin kana/prolonged marks connected to a strong column run.

    Projection thresholding finds the main glyph bodies, but a narrow vertical
    prolonged mark can contribute only one or two pixels per row.  Extend the
    selected run across short zero gaps while staying clear of panel borders.
    """
    if not row_counts:
        return start_y, end_y
    lower = max(0, margin_y + 1)
    upper = min(len(row_counts) - 1, len(row_counts) - margin_y - 2)
    max_gap = max(2, int(round(len(row_counts) * 0.018)))

    cursor = end_y + 1
    last_ink = end_y
    gap = 0
    while cursor <= upper:
        if row_counts[cursor] > 0:
            last_ink = cursor
            gap = 0
        else:
            gap += 1
            if gap > max_gap:
                break
        cursor += 1
    end_y = max(end_y, last_ink)

    cursor = start_y - 1
    first_ink = start_y
    gap = 0
    while cursor >= lower:
        if row_counts[cursor] > 0:
            first_ink = cursor
            gap = 0
        else:
            gap += 1
            if gap > max_gap:
                break
        cursor -= 1
    return min(start_y, first_ink), end_y


def _strip_dark_column_horizontal_rules(
    row_counts: list[int],
    lane_width: int,
) -> list[int]:
    """Remove sustained near-solid panel rules from one dark text lane.

    L-shaped narration frames can cross only part of the proposal crop, so the
    page-wide long-row filter does not always see them.  If such a local rule
    is merged with the first glyph run, every character slot in that lane is
    shifted upward.  Real glyphs may contain one or two dense horizontal
    strokes, but not a long band that fills most of the lane width.
    """
    if not row_counts or lane_width < 5:
        return list(row_counts)

    dense_threshold = max(4, int(round(lane_width * 0.76)))
    dense_spans = _contiguous_spans([count >= dense_threshold for count in row_counts])
    dense_spans = _merge_small_gaps(
        dense_spans,
        max(2, int(round(len(row_counts) * 0.018))),
    )
    minimum_rule_height = max(5, int(round(len(row_counts) * 0.025)))
    rules = [
        (start, end)
        for start, end in dense_spans
        if end - start + 1 >= minimum_rule_height
    ]
    if not rules:
        return list(row_counts)

    cleaned = list(row_counts)
    for start, end in rules:
        for row in range(start, end + 1):
            cleaned[row] = 0
    return cleaned


def _dark_column_lane_half_window(crop_width: int) -> int:
    """Search far enough from a furigana-biased x peak to recover main ink."""
    return max(8, int(round(max(0, crop_width) * 0.10)))


def _dark_column_cell_bounds(
    *,
    center: int,
    glyph_left: int,
    glyph_right: int,
    start_x: int,
    end_x: int,
    target_width: int,
) -> tuple[int, int]:
    """Center a dark-column hit cell on the recovered main-glyph envelope.

    The x-projection peak is the densest stroke, not necessarily the visual
    centre of a kanji.  Using it as the hitbox centre clips asymmetric glyphs.
    """
    if end_x <= start_x or target_width <= 0:
        return start_x, start_x
    width = min(target_width, end_x - start_x)
    glyph_center = (
        (glyph_left + glyph_right) / 2.0
        if glyph_right > glyph_left
        else float(center)
    )
    cell_left = max(
        start_x,
        min(end_x - width, int(round(glyph_center - width / 2.0))),
    )
    return cell_left, cell_left + width


def _refine_dark_column_vertical_extent(
    row_counts: list[int],
    *,
    crop_height: int,
    lane_width: int,
    start_y: int,
    end_y: int,
    margin_y: int,
) -> tuple[int, int, list[int]]:
    """Re-fit a lane's vertical span after tightening x to the main glyphs.

    Dark-block peak search intentionally starts from a broad x window so a
    ruby-biased column centre can still recover the full kanji body.  That same
    window can temporarily include stray ink from a neighbouring lane and pull
    the selected y-span upward.  Once the main-glyph envelope is known, rerun
    the vertical-span selection on that tighter x-range and keep the strongest
    span that overlaps the original lane.
    """
    if not row_counts or lane_width < 3:
        return start_y, end_y, list(row_counts)

    cleaned = _strip_dark_column_horizontal_rules(row_counts, lane_width)
    minimum_row_ink = max(1, int(round(lane_width * 0.12)))
    spans = _contiguous_spans([count >= minimum_row_ink for count in cleaned])
    spans = _merge_small_gaps(spans, max(4, int(round(crop_height * 0.07))))
    if not spans:
        return start_y, end_y, cleaned

    overlapping: list[tuple[int, int, int, int]] = []
    for candidate_start, candidate_end in spans:
        span_height = candidate_end - candidate_start + 1
        if span_height < max(4, int(round(crop_height * 0.12))):
            continue
        if candidate_start <= margin_y or candidate_end >= crop_height - margin_y:
            continue
        overlap = min(candidate_end, end_y) - max(candidate_start, start_y) + 1
        if overlap <= 0:
            continue
        overlapping.append((overlap, span_height, candidate_start, candidate_end))
    if not overlapping:
        return start_y, end_y, cleaned

    _overlap, _height, refined_start, refined_end = max(overlapping)
    refined_start, refined_end = _expand_dark_column_weak_ink(
        cleaned,
        refined_start,
        refined_end,
        margin_y=margin_y,
    )
    # Only the leading edge is refined. The broad lane remains authoritative
    # for the tail: a tight x-envelope can miss a wide lower glyph (for example
    # 言) and would otherwise truncate the rest of the vertical phrase.
    return refined_start, max(end_y, refined_end), cleaned


def _dark_column_slot_boundaries(
    row_counts: list[int],
    start_y: int,
    end_y: int,
    count: int,
) -> list[int]:
    """Split a vertical lane at ink valleys rather than into equal rectangles."""
    stop_y = end_y + 1
    if count <= 1 or stop_y - start_y < count * 2:
        return [start_y, stop_y]
    pitch = (stop_y - start_y) / count
    minimum = max(2, int(math.floor(pitch * 0.34)))
    maximum = max(minimum + 1, int(math.ceil(pitch * 2.15)))
    peak = max(row_counts[start_y:stop_y] or [1])

    def boundary_cost(position: int) -> float:
        local = row_counts[max(start_y, position - 1) : min(stop_y, position + 2)]
        return min(local or [0]) / max(1.0, float(peak))

    states: dict[int, tuple[float, list[int]]] = {start_y: (0.0, [start_y])}
    for boundary_index in range(1, count):
        next_states: dict[int, tuple[float, list[int]]] = {}
        first = start_y + minimum * boundary_index
        last = stop_y - minimum * (count - boundary_index)
        for position in range(first, last + 1):
            best: tuple[float, list[int]] | None = None
            for previous, (old_cost, path) in states.items():
                interval = position - previous
                if interval < minimum or interval > maximum:
                    continue
                spacing = ((interval - pitch) / max(1.0, pitch)) ** 2
                cost = old_cost + boundary_cost(position) * 3.0 + spacing * 0.35
                if best is None or cost < best[0]:
                    best = (cost, [*path, position])
            if best is not None:
                next_states[position] = best
        if not next_states:
            break
        states = next_states

    best_final: tuple[float, list[int]] | None = None
    for previous, (old_cost, path) in states.items():
        if len(path) != count:
            continue
        interval = stop_y - previous
        if interval < minimum or interval > maximum:
            continue
        spacing = ((interval - pitch) / max(1.0, pitch)) ** 2
        candidate = (old_cost + spacing * 0.35, [*path, stop_y])
        if best_final is None or candidate[0] < best_final[0]:
            best_final = candidate
    if best_final is not None:
        return best_final[1]
    return [int(round(start_y + (stop_y - start_y) * index / count)) for index in range(count + 1)]


def _accepted_dark_column_surfaces(full_text: str, surfaces: list[str]) -> str:
    """Accept per-column OCR only when it strongly agrees with the full crop."""
    full = _compact_surface(full_text)
    compact_surfaces = [_compact_surface(surface) for surface in surfaces]
    if not full or len(compact_surfaces) < 2 or any(not surface for surface in compact_surfaces):
        return ""
    joined = "".join(compact_surfaces)
    if len(joined) < max(4, int(math.floor(len(full) * 0.82))):
        return ""
    if len(joined) > int(math.ceil(len(full) * 1.12)):
        return ""
    ratio = difflib.SequenceMatcher(a=full, b=joined, autojunk=False).ratio()
    if ratio < 0.88:
        return ""
    return joined


def _dark_column_alignment_surface(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).translate(
        str.maketrans({"カ": "力"})
    )
    return "".join(
        character
        for character in normalized
        if (
            "ぁ" <= character <= "ゖ"
            or "ァ" <= character <= "ヺ"
            or character == "ー"
            or "一" <= character <= "龯"
        )
    )


def _rebalance_dark_column_boundary_punctuation(surfaces: list[str]) -> list[str]:
    """Attach a boundary middle dot to the following kanji lane when needed.

    `男・海賊王` should map more naturally as `手に入れた男` + `・海賊王...`.
    Do not keep moving the same dot into a katakana name such as
    `ゴールド・ロジャー`.
    """
    if len(surfaces) < 2:
        return list(surfaces)
    balanced = list(surfaces)
    for index in range(len(balanced) - 1):
        current = str(balanced[index] or "")
        nxt = str(balanced[index + 1] or "")
        if not current.endswith("・") or nxt.startswith("・"):
            continue
        first = next((ch for ch in nxt if ch != "・"), "")
        if not ("一" <= first <= "龯"):
            continue
        balanced[index] = current[:-1]
        balanced[index + 1] = "・" + nxt
    return balanced


def _align_dark_column_surfaces(
    full_text: str,
    raw_surfaces: list[str],
    prior_counts: list[int],
) -> tuple[str, list[str]]:
    """Partition full OCR by lane evidence and reject one unsupported insertion."""
    full = _compact_surface(full_text)
    if not full or not 2 <= len(raw_surfaces) == len(prior_counts) <= 8:
        return "", []
    evidence = [_dark_column_alignment_surface(value) for value in raw_surfaces]
    if any(not value for value in evidence):
        return "", []
    lane_count = len(evidence)
    maximum_gap = 1

    @lru_cache(maxsize=None)
    def solve(
        lane_index: int,
        position: int,
        gaps_used: int,
    ) -> tuple[float, tuple[str, ...], tuple[str, ...], tuple[float, ...]] | None:
        if lane_index == lane_count:
            return (0.0, (), (), ()) if position == len(full) else None
        gaps = (0,) if lane_index == 0 else range(maximum_gap - gaps_used + 1)
        best: tuple[float, tuple[str, ...], tuple[str, ...], tuple[float, ...]] | None = None
        for gap in gaps:
            start = position + gap
            minimum_remaining = lane_count - lane_index - 1
            if start >= len(full) - minimum_remaining:
                continue
            if lane_index == lane_count - 1:
                lengths = (len(full) - start,)
            else:
                lengths = range(1, min(14, len(full) - start - minimum_remaining) + 1)
            for length in lengths:
                candidate = full[start : start + length]
                candidate_surface = _dark_column_alignment_surface(candidate)
                if not candidate_surface:
                    continue
                ratio = difflib.SequenceMatcher(
                    a=evidence[lane_index],
                    b=candidate_surface,
                    autojunk=False,
                ).ratio()
                if ratio < 0.55:
                    continue
                exact_bonus = 2.0 if evidence[lane_index] == candidate_surface else 0.0
                score = (
                    ratio * 10.0
                    + exact_bonus
                    - abs(length - prior_counts[lane_index]) * 0.12
                    - gap * 0.8
                )
                rest = solve(lane_index + 1, start + length, gaps_used + gap)
                if rest is None:
                    continue
                proposal = (
                    score + rest[0],
                    (candidate, *rest[1]),
                    ((full[position:start] if gap else ""), *rest[2]),
                    (ratio, *rest[3]),
                )
                if best is None or proposal[0] > best[0]:
                    best = proposal
        return best

    result = solve(0, 0, 0)
    if result is None:
        return "", []
    _score, surfaces, dropped, ratios = result
    removed = "".join(dropped)
    exact_matches = sum(
        evidence[index] == _dark_column_alignment_surface(surface)
        for index, surface in enumerate(surfaces)
    )
    if min(ratios) < 0.72 or sum(ratios) / len(ratios) < 0.90 or exact_matches < 2:
        return "", []
    if removed and (
        len(removed) != 1
        or not _dark_column_alignment_surface(removed)
        or difflib.SequenceMatcher(a=full, b="".join(surfaces), autojunk=False).ratio() < 0.96
    ):
        return "", []
    return "".join(surfaces), list(surfaces)


def _square_pad_dark_column_crop(crop: Image.Image) -> Image.Image:
    """Keep a narrow vertical lane's aspect ratio for MangaOCR's square input."""
    width, height = crop.size
    side = max(width, height)
    margin = max(4, int(round(side * 0.045)))
    canvas_side = side + margin * 2
    corners = [
        crop.getpixel((0, 0)),
        crop.getpixel((max(0, width - 1), 0)),
        crop.getpixel((0, max(0, height - 1))),
        crop.getpixel((max(0, width - 1), max(0, height - 1))),
    ]
    background = tuple(
        int(round(statistics.median(int(pixel[channel]) for pixel in corners)))
        for channel in range(3)
    )
    canvas = Image.new("RGB", (canvas_side, canvas_side), background)
    canvas.paste(crop, ((canvas_side - width) // 2, (canvas_side - height) // 2))
    return canvas


def _dark_column_small_kana_polarity_consensus(
    current: object,
    direct: object,
    tight: object,
) -> tuple[str, list[dict[str, object]]]:
    """Accept only large→small kana changes confirmed by two original-polarity views.

    Dark narration boxes are normally inverted before MangaOCR, which is a good
    default for recognition but can normalize printed small kana (especially っ)
    to their full-size forms.  Keep the inverted result unless both an ordinary
    padded crop and a tighter crop agree exactly and differ from it only by
    kana-size pairs with matching local context on both sides.
    """
    baseline = _normalize_line_surface(current)
    padded = _normalize_line_surface(direct)
    tight_surface = _normalize_line_surface(tight)
    if (
        not baseline
        or not padded
        or padded != tight_surface
        or padded == baseline
        or len(padded) != len(baseline)
    ):
        return baseline, []

    changed: list[dict[str, object]] = []
    for index, (large, small) in enumerate(zip(baseline, padded)):
        if large == small:
            continue
        if _SMALL_KANA_TO_LARGE.get(small) != large:
            return baseline, []
        left_support = any(
            baseline[pos] == padded[pos]
            for pos in range(max(0, index - 2), index)
        )
        right_support = any(
            baseline[pos] == padded[pos]
            for pos in range(index + 1, min(len(baseline), index + 4))
        )
        if not (left_support and right_support):
            return baseline, []
        changed.append({"index": index, "from": large, "to": small})

    if not changed:
        return baseline, []
    return padded, changed


def _recognize_dark_column_surfaces(
    model: object,
    image: Image.Image,
    segments: list[dict[str, object]],
    full_text: str,
) -> tuple[str, list[str]]:
    """Re-read detected dark lanes independently to reject cross-lane hallucinations."""
    groups: list[list[dict[str, object]]] = []
    for segment in segments:
        if str(segment.get("source") or "") != "dark-columns-v1":
            continue
        if not groups or abs(_number(groups[-1][0].get("x")) - _number(segment.get("x"))) > 1e-6:
            groups.append([segment])
        else:
            groups[-1].append(segment)
    if not 2 <= len(groups) <= 8:
        return "", []

    page_width, page_height = image.size
    surfaces: list[str] = []
    for group in groups:
        x1 = min(_number(item.get("x")) for item in group)
        y1 = min(_number(item.get("y")) for item in group)
        x2 = max(_number(item.get("x")) + _number(item.get("width")) for item in group)
        y2 = max(_number(item.get("y")) + _number(item.get("height")) for item in group)
        pad_x = max(2, int(round((x2 - x1) * page_width * 0.20)))
        pad_y = max(2, int(round((y2 - y1) * page_height * 0.025)))
        left = max(0, int(math.floor(x1 * page_width)) - pad_x)
        right = min(page_width, int(math.ceil(x2 * page_width)) + pad_x)
        top = max(0, int(math.floor((1.0 - y2) * page_height)) - pad_y)
        bottom = min(page_height, int(math.ceil((1.0 - y1) * page_height)) + pad_y)
        if right - left < 5 or bottom - top < 8:
            return "", []
        crop = image.crop((left, top, right, bottom)).convert("RGB")
        square_crop: Image.Image | None = None
        try:
            gray = crop.convert("L").resize((12, 24), Image.Resampling.BOX)
            try:
                mean = sum(int(value) for value in gray.getdata()) / (12 * 24)
            finally:
                gray.close()
            square_crop = _square_pad_dark_column_crop(crop)
            ocr_crop = ImageOps.invert(square_crop) if mean < 128 else square_crop
            try:
                surface = _compact_surface(model(ocr_crop))  # type: ignore[operator]
            finally:
                if ocr_crop is not square_crop:
                    ocr_crop.close()

            # Inverted dark-column OCR is the normal baseline.  Only probe the
            # original polarity when that baseline contains a kana with a small
            # counterpart.  If the padded original crop suggests only a
            # large→small kana change, confirm it with a second, tighter crop
            # before accepting the typography correction.
            if mean < 128 and any(
                character in set(_SMALL_KANA_TO_LARGE.values())
                for character in surface
            ):
                direct_surface = _compact_surface(model(crop))  # type: ignore[operator]
                _candidate, preliminary = _dark_column_small_kana_polarity_consensus(
                    surface, direct_surface, direct_surface
                )
                if preliminary:
                    tight_left = max(0, int(math.floor(x1 * page_width)))
                    tight_right = min(page_width, int(math.ceil(x2 * page_width)))
                    tight_top = max(0, int(math.floor((1.0 - y2) * page_height)))
                    tight_bottom = min(page_height, int(math.ceil((1.0 - y1) * page_height)))
                    if tight_right - tight_left >= 5 and tight_bottom - tight_top >= 8:
                        tight_crop = image.crop(
                            (tight_left, tight_top, tight_right, tight_bottom)
                        ).convert("RGB")
                        try:
                            tight_surface = _compact_surface(model(tight_crop))  # type: ignore[operator]
                        finally:
                            tight_crop.close()
                        repaired_surface, repairs = _dark_column_small_kana_polarity_consensus(
                            surface, direct_surface, tight_surface
                        )
                        if repairs:
                            surface = repaired_surface
        except Exception:
            return "", []
        finally:
            if square_crop is not None:
                square_crop.close()
            crop.close()
        if not surface or len(surface) > 14:
            return "", [*surfaces, surface]
        surfaces.append(surface)
    surfaces = _rebalance_dark_column_boundary_punctuation(surfaces)
    accepted = _accepted_dark_column_surfaces(full_text, surfaces)
    if accepted:
        return accepted, surfaces
    aligned, aligned_surfaces = _align_dark_column_surfaces(
        full_text,
        surfaces,
        [len(group) for group in groups],
    )
    if aligned:
        aligned_surfaces = _rebalance_dark_column_boundary_punctuation(aligned_surfaces)
        return "".join(aligned_surfaces), aligned_surfaces
    return "", surfaces


def _infer_dark_block_vertical_columns(
    image: Image.Image,
    region: dict[str, object],
    compact: str,
    ink: bytearray,
    *,
    crop_width: int,
    crop_height: int,
    left: int,
    top: int,
) -> list[dict[str, object]]:
    """Recover main vertical text columns inside black narration boxes.

    Furigana and decorative borders make connected-component grids unreliable.
    Main glyph columns still create strong vertical projection peaks, while
    border bands are dense horizontal clusters.  Detect those peaks, recover
    each column's real vertical extent, then distribute MangaOCR's flat text
    across columns proportionally to their measured heights.
    """
    if str(region.get("source") or "") != "dark-block-proposal":
        return []
    if len(compact) < 8 or crop_width < 40 or crop_height < 40:
        return []

    working = bytearray(ink)
    margin_x = max(3, int(round(crop_width * 0.06)))
    margin_y = max(3, int(round(crop_height * 0.025)))
    for row in range(crop_height):
        base = row * crop_width
        if row < margin_y or row >= crop_height - margin_y:
            working[base : base + crop_width] = b"\x00" * crop_width
            continue
        for column in range(margin_x):
            working[base + column] = 0
            working[base + crop_width - 1 - column] = 0

    column_counts = [
        sum(working[row * crop_width + column] for row in range(crop_height))
        for column in range(crop_width)
    ]
    radius = max(2, int(round(crop_width * 0.018)))
    smoothed: list[float] = []
    for column in range(crop_width):
        start = max(0, column - radius)
        end = min(crop_width, column + radius + 1)
        smoothed.append(float(sum(column_counts[start:end])))
    peak_max = max(smoothed[margin_x : crop_width - margin_x] or [0.0])
    if peak_max <= 0:
        return []
    candidates: list[int] = []
    for column in range(margin_x + radius, crop_width - margin_x - radius):
        value = smoothed[column]
        if value < max(8.0, peak_max * 0.52):
            continue
        neighbourhood = smoothed[column - radius : column + radius + 1]
        if value >= max(neighbourhood):
            candidates.append(column)
    peaks = _select_dark_block_column_peaks(candidates, smoothed, crop_width)
    if not 2 <= len(peaks) <= min(8, len(compact)):
        return []

    columns: list[tuple[int, int, int, int, int, int, int, list[int]]] = []
    # The x-projection peak is often pulled toward furigana.  A 6% half-window
    # could start *inside* a wide main kanji column, permanently clipping its
    # opposite edge before glyph-envelope recovery even ran.  Midpoint lane
    # boundaries already prevent crossing into neighbouring main columns, so a
    # wider search window is safe and lets the envelope recover the full glyph.
    half_window = _dark_column_lane_half_window(crop_width)
    for index, center in enumerate(peaks):
        right_boundary = crop_width - margin_x if index == 0 else (peaks[index - 1] + center) // 2
        left_boundary = margin_x if index == len(peaks) - 1 else (center + peaks[index + 1]) // 2
        start_x = max(left_boundary, center - half_window)
        end_x = min(right_boundary + 1, center + half_window + 1)
        if end_x - start_x < 5:
            return []
        row_counts = [
            sum(working[row * crop_width + column] for column in range(start_x, end_x))
            for row in range(crop_height)
        ]
        row_counts = _strip_dark_column_horizontal_rules(row_counts, end_x - start_x)
        minimum_row_ink = max(2, int(round((end_x - start_x) * 0.12)))
        spans = _contiguous_spans([count >= minimum_row_ink for count in row_counts])
        spans = _merge_small_gaps(spans, max(4, int(round(crop_height * 0.07))))
        usable: list[tuple[int, int, float]] = []
        for start_y, end_y in spans:
            span_height = end_y - start_y + 1
            if span_height < crop_height * 0.12:
                continue
            if start_y <= margin_y or end_y >= crop_height - margin_y:
                continue
            density = sum(row_counts[start_y : end_y + 1]) / max(1.0, span_height * (end_x - start_x))
            # Frame/border runs are close to solid; text columns remain sparse.
            if density >= 0.62:
                continue
            usable.append((start_y, end_y, density))
        if not usable:
            return []
        start_y, end_y, _density = max(usable, key=lambda item: item[1] - item[0])
        start_y, end_y = _expand_dark_column_weak_ink(
            row_counts,
            start_y,
            end_y,
            margin_y=margin_y,
        )

        # Tighten x around the actual main-glyph column while keeping the full
        # vertical band.  Small ruby at the side has much weaker projection.
        local_x_counts = [
            sum(working[row * crop_width + column] for row in range(start_y, end_y + 1))
            for column in range(start_x, end_x)
        ]
        local_peak = max(local_x_counts or [0])
        active_x = [count >= max(2, int(round(local_peak * 0.16))) for count in local_x_counts]
        x_spans = _contiguous_spans(active_x)
        containing = [
            span for span in x_spans
            if start_x + span[0] <= center <= start_x + span[1]
        ]
        if containing:
            chosen_x = max(containing, key=lambda span: span[1] - span[0])
            glyph_left = start_x + chosen_x[0]
            glyph_right = start_x + chosen_x[1] + 1
        else:
            glyph_left, glyph_right = start_x, end_x
        columns.append((center, glyph_left, glyph_right, start_x, end_x, start_y, end_y, row_counts))

    heights = [end_y - start_y + 1 for _c, _l, _r, _sx, _ex, start_y, end_y, _rows in columns]
    pitch = sum(heights) / max(1.0, float(len(compact)))
    if pitch < 4.0 or pitch > crop_height * 0.30:
        return []
    raw_surfaces = region.get("dark_column_surfaces")
    supplied_surfaces = (
        [_compact_surface(value) for value in raw_surfaces if _compact_surface(value)]
        if isinstance(raw_surfaces, list)
        else []
    )
    if len(supplied_surfaces) == len(columns) and "".join(supplied_surfaces) == compact:
        counts = [len(value) for value in supplied_surfaces]
    else:
        ideals = [height / pitch for height in heights]
        counts = [max(1, int(round(value))) for value in ideals]
        while sum(counts) < len(compact):
            index = max(range(len(counts)), key=lambda i: ideals[i] - counts[i])
            counts[index] += 1
        while sum(counts) > len(compact):
            candidates_to_reduce = [i for i, count in enumerate(counts) if count > 1]
            if not candidates_to_reduce:
                return []
            index = max(candidates_to_reduce, key=lambda i: counts[i] - ideals[i])
            counts[index] -= 1

        # A Japanese vertical column should not begin with a prolonged sound mark.
        # Height rounding can put the preceding katakana at the bottom of the
        # neighbouring lane, so move that one glyph across the boundary.
        counts = _rebalance_dark_column_line_starts(compact, counts)
    if any(count > 12 for count in counts):
        return []

    page_width, page_height = image.size
    segments: list[dict[str, object]] = []
    character_index = 0
    for (center, glyph_left, glyph_right, start_x, end_x, start_y, end_y, row_counts), count in zip(columns, counts):
        refined_row_counts = [
            sum(working[row * crop_width + column] for column in range(glyph_left, glyph_right))
            for row in range(crop_height)
        ]
        start_y, end_y, refined_row_counts = _refine_dark_column_vertical_extent(
            refined_row_counts,
            crop_height=crop_height,
            lane_width=max(1, glyph_right - glyph_left),
            start_y=start_y,
            end_y=end_y,
            margin_y=margin_y,
        )
        band_height = end_y - start_y + 1
        boundaries = _dark_column_slot_boundaries(refined_row_counts, start_y, end_y, count)
        average_slot = band_height / max(1, count)
        target_width = min(
            end_x - start_x,
            max(glyph_right - glyph_left, int(round(average_slot * 0.92))),
        )
        cell_left, cell_right = _dark_column_cell_bounds(
            center=center,
            glyph_left=glyph_left,
            glyph_right=glyph_right,
            start_x=start_x,
            end_x=end_x,
            target_width=target_width,
        )
        for row_index in range(count):
            slot_top = boundaries[row_index]
            slot_bottom = boundaries[row_index + 1]
            slot_bottom = max(slot_top + 1, min(crop_height, slot_bottom))
            page_left = left + cell_left
            page_right = left + cell_right
            page_top = top + slot_top
            page_bottom = top + slot_bottom
            seg_x = max(0.0, min(1.0, page_left / page_width))
            seg_width = max(0.0, min(1.0 - seg_x, (page_right - page_left) / page_width))
            seg_height = max(0.0, min(1.0, (page_bottom - page_top) / page_height))
            seg_y = max(0.0, min(1.0 - seg_height, 1.0 - page_bottom / page_height))
            if seg_width <= 0.001 or seg_height <= 0.001 or character_index >= len(compact):
                return []
            segments.append({
                "text": compact[character_index],
                "orientation": "vertical",
                "x": round(seg_x, 6),
                "y": round(seg_y, 6),
                "width": round(seg_width, 6),
                "height": round(seg_height, 6),
                "source": "dark-columns-v1",
            })
            character_index += 1
    return segments if character_index == len(compact) else []

def _infer_variable_vertical_columns(
    image: Image.Image,
    region: dict[str, object],
    compact: str,
    ink: bytearray,
    *,
    crop_width: int,
    crop_height: int,
    left: int,
    top: int,
) -> list[dict[str, object]]:
    """Recover real variable-length vertical columns instead of an equal grid.

    Manga bubbles commonly have 3/4/5/5/4 glyphs per column. The previous
    equal-row grid shifted every later token once one short column appeared.
    This path is deliberately strict: if the detected glyph count does not
    exactly match MangaOCR's text length, return [] and keep the legacy fallback.
    """
    if str(region.get("source") or "") not in {"expanded-vertical-seed", "expanded-vision-rectangle", "layout-cluster-v1"}:
        return []
    if len(compact) < 4 or crop_width < 24 or crop_height < 24:
        return []

    cleaned = _remove_large_ink_components(ink, crop_width, crop_height)
    column_counts = [
        sum(cleaned[row * crop_width + column] for row in range(crop_height))
        for column in range(crop_width)
    ]
    minimum_column_ink = max(2, int(round(crop_height * 0.014)))
    raw_columns = [
        span
        for span in _contiguous_spans([count >= minimum_column_ink for count in column_counts])
        if span[1] - span[0] + 1 >= 2
    ]
    minimum_column_width = max(4, int(round(crop_width * 0.038)))
    columns = [
        span for span in raw_columns
        if span[1] - span[0] + 1 >= minimum_column_width
    ]
    columns.sort(key=lambda span: span[0], reverse=True)
    if not 2 <= len(columns) <= min(12, len(compact)):
        return []

    preliminary: list[tuple[int, int, list[tuple[int, int]]]] = []
    normal_heights: list[int] = []
    for column_start, column_end in columns:
        margin = max(1, int(round((column_end - column_start + 1) * 0.08)))
        start_x = max(0, column_start - margin)
        end_x = min(crop_width, column_end + margin + 1)
        row_counts = [
            sum(cleaned[row * crop_width + column] for column in range(start_x, end_x))
            for row in range(crop_height)
        ]
        minimum_row_ink = max(2, int(round((end_x - start_x) * 0.07)))
        row_spans = [
            span
            for span in _contiguous_spans([count >= minimum_row_ink for count in row_counts])
            if span[1] - span[0] + 1 >= 2
        ]
        row_spans = _merge_small_gaps(
            row_spans,
            max(1, int(round(crop_height * 0.012))),
        )
        preliminary.append((column_start, column_end, row_spans))
        normal_heights.extend(
            end - start + 1
            for start, end in row_spans
            if 5 <= end - start + 1 <= max(8, int(round(crop_height * 0.18)))
        )
    if not normal_heights:
        return []
    normal_heights.sort()
    median_height = float(normal_heights[len(normal_heights) // 2])

    first_rows: list[int] = []
    for _column_start, _column_end, row_spans in preliminary:
        for start, end in row_spans:
            if end - start + 1 >= max(4.0, median_height * 0.45):
                first_rows.append(start)
                break
    if not first_rows:
        return []
    first_rows.sort()
    common_top = float(first_rows[len(first_rows) // 2])

    glyphs_by_column: list[tuple[int, int, list[tuple[int, int]]]] = []
    for column_start, column_end, row_spans in preliminary:
        filtered = [
            (start, end)
            for start, end in row_spans
            if end >= common_top - median_height * 0.70
        ]
        glyph_rows: list[tuple[int, int]] = []
        for start, end in filtered:
            span_height = end - start + 1
            split_count = max(1, int(round(span_height / max(1.0, median_height))))
            if span_height > median_height * 1.55 and split_count >= 2:
                unit = span_height / split_count
                for index in range(split_count):
                    part_start = int(round(start + index * unit))
                    part_end = int(round(start + (index + 1) * unit)) - 1
                    glyph_rows.append((part_start, max(part_start, part_end)))
            else:
                glyph_rows.append((start, end))
        glyphs_by_column.append((column_start, column_end, glyph_rows))

    total_glyphs = sum(len(rows) for _start, _end, rows in glyphs_by_column)
    if total_glyphs != len(compact):
        return []

    page_width, page_height = image.size
    segments: list[dict[str, object]] = []
    character_index = 0
    for column_start, column_end, glyph_rows in glyphs_by_column:
        for row_start, row_end in glyph_rows:
            if character_index >= len(compact):
                return []
            page_left = left + column_start
            page_right = left + column_end + 1
            page_top = top + row_start
            page_bottom = top + row_end + 1
            seg_x = max(0.0, min(1.0, page_left / page_width))
            seg_width = max(0.0, min(1.0 - seg_x, (page_right - page_left) / page_width))
            seg_height = max(0.0, min(1.0, (page_bottom - page_top) / page_height))
            seg_y = max(0.0, min(1.0 - seg_height, 1.0 - page_bottom / page_height))
            if seg_width <= 0.001 or seg_height <= 0.001:
                return []
            segments.append(
                {
                    "text": compact[character_index],
                    "orientation": "vertical",
                    "x": round(seg_x, 6),
                    "y": round(seg_y, 6),
                    "width": round(seg_width, 6),
                    "height": round(seg_height, 6),
                    "source": "ink-columns-v2",
                }
            )
            character_index += 1
    return segments if character_index == len(compact) else []


def _strip_dark_block_decorative_suffix(
    item: dict[str, object],
) -> bool:
    """Drop a separator OCR'ed as a trailing dash from a dark narration block.

    The dark-column detector removes tall decorative rules before character
    allocation. If the resulting segment stream exactly matches the OCR text
    except for one trailing dash-like glyph, that suffix is not study text.
    Keeping it in item["text"] makes exact token-surface mapping fail for the
    final real word (e.g. 迎えるー vs segments 迎える).
    """
    if str(item.get("source") or "") != "dark-block-proposal":
        return False
    segments = [
        segment
        for segment in item.get("segments") or []
        if isinstance(segment, dict)
        and str(segment.get("source") or "") == "dark-columns-v1"
    ]
    if not segments:
        return False

    compact = "".join(ch for ch in str(item.get("text") or "") if not ch.isspace())
    if len(compact) < 2 or compact[-1] not in {"ー", "―", "—", "−", "|", "｜"}:
        return False

    surface = "".join(str(segment.get("text") or "") for segment in segments)
    if surface != compact[:-1]:
        return False

    item["text"] = surface
    item["decorative_suffix_removed"] = compact[-1]
    item["decorative_suffix_reason"] = "dark-block-long-rule-v1"
    return True


def _infer_vertical_character_segments(
    image: Image.Image,
    region: dict[str, object],
    text: str,
) -> list[dict[str, object]]:
    """Recover per-character geometry from actual ink in an expanded manga box."""
    if str(region.get("source") or "") not in {"expanded-vision-rectangle", "expanded-vertical-seed", "dark-block-proposal", "layout-cluster-v1"}:
        return []
    compact = "".join(ch for ch in str(text or "") if not ch.isspace())
    if len(compact) < 2:
        return []

    page_width, page_height = image.size
    x = max(0.0, min(1.0, _number(region.get("x"))))
    y = max(0.0, min(1.0, _number(region.get("y"))))
    width = max(0.0, min(1.0 - x, _number(region.get("width"))))
    height = max(0.0, min(1.0 - y, _number(region.get("height"))))
    left = max(0, min(page_width - 1, round(x * page_width)))
    right = max(left + 1, min(page_width, round((x + width) * page_width)))
    top = max(0, min(page_height - 1, round((1.0 - y - height) * page_height)))
    bottom = max(top + 1, min(page_height, round((1.0 - y) * page_height)))
    crop_width = right - left
    crop_height = bottom - top
    if crop_width < 10 or crop_height < 10:
        return []

    crop = image.crop((left, top, right, bottom)).convert("L")
    try:
        pixels = list(crop.getdata())
    finally:
        crop.close()
    if not pixels:
        return []

    histogram = [0] * 256
    for pixel in pixels:
        histogram[int(pixel)] += 1
    total = len(pixels)
    weighted_total = sum(value * count for value, count in enumerate(histogram))
    background_weight = 0
    background_sum = 0
    best_variance = -1.0
    threshold = 160
    for value, count in enumerate(histogram):
        background_weight += count
        if background_weight <= 0:
            continue
        foreground_weight = total - background_weight
        if foreground_weight <= 0:
            break
        background_sum += value * count
        background_mean = background_sum / background_weight
        foreground_mean = (weighted_total - background_sum) / foreground_weight
        variance = background_weight * foreground_weight * (background_mean - foreground_mean) ** 2
        if variance > best_variance:
            best_variance = variance
            threshold = value
    threshold = max(60, min(210, int(threshold)))
    dark_count = sum(1 for pixel in pixels if int(pixel) <= threshold)
    light_count = total - dark_count
    # Manga frequently places white text on solid black narration boxes. Use
    # the minority polarity as glyph ink instead of assuming black-on-white.
    dark_is_ink = dark_count <= light_count
    if dark_is_ink:
        ink = bytearray(1 if int(pixel) <= threshold else 0 for pixel in pixels)
    else:
        ink = bytearray(1 if int(pixel) > threshold else 0 for pixel in pixels)

    row_counts = [
        sum(ink[row * crop_width : (row + 1) * crop_width])
        for row in range(crop_height)
    ]
    col_counts = [
        sum(ink[row * crop_width + column] for row in range(crop_height))
        for column in range(crop_width)
    ]
    long_rows = {
        row for row, count in enumerate(row_counts)
        if count >= max(4, crop_width * 0.62)
    }
    long_columns = {
        column for column, count in enumerate(col_counts)
        if count >= max(4, crop_height * 0.62)
    }
    long_column_spans = _contiguous_spans(
        [column in long_columns for column in range(crop_width)]
    )
    interior_long_rules = [
        (start, end)
        for start, end in long_column_spans
        if (
            end - start + 1 <= max(3, int(round(crop_width * 0.035)))
            and crop_width * 0.12 <= (start + end) / 2.0 <= crop_width * 0.88
        )
    ]
    dark_compact = compact
    if (
        str(region.get("source") or "") == "dark-block-proposal"
        and dark_compact
        and dark_compact[-1] in {"ー", "―", "—", "−", "|", "｜"}
        and interior_long_rules
    ):
        # A tall separator printed beside narration text is decoration. Counting
        # it as a glyph steals one OCR character from the neighbouring real
        # column during height-proportional allocation.
        dark_compact = dark_compact[:-1]

    if long_rows or long_columns:
        for row in range(crop_height):
            base = row * crop_width
            for column in range(crop_width):
                if row in long_rows or column in long_columns:
                    ink[base + column] = 0

    dark_columns = _infer_dark_block_vertical_columns(
        image,
        region,
        dark_compact,
        ink,
        crop_width=crop_width,
        crop_height=crop_height,
        left=left,
        top=top,
    )
    if dark_columns:
        return dark_columns

    variable_columns = _infer_variable_vertical_columns(
        image,
        region,
        compact,
        ink,
        crop_width=crop_width,
        crop_height=crop_height,
        left=left,
        top=top,
    )
    if variable_columns:
        return variable_columns

    row_counts = [
        sum(ink[row * crop_width : (row + 1) * crop_width])
        for row in range(crop_height)
    ]
    minimum_row_ink = max(2, int(round(crop_width * 0.025)))
    row_active = [
        minimum_row_ink <= count <= max(minimum_row_ink, int(crop_width * 0.55))
        for count in row_counts
    ]
    row_spans = [
        span for span in _contiguous_spans(row_active)
        if span[1] - span[0] + 1 >= 2
    ]
    if not row_spans:
        return []

    span_heights = sorted(end - start + 1 for start, end in row_spans)
    median_height = span_heights[len(span_heights) // 2]
    gap_limit = max(3, int(round(median_height * 0.35)))
    clusters: list[list[tuple[int, int]]] = []
    for span in row_spans:
        if not clusters or span[0] - clusters[-1][-1][1] - 1 > gap_limit:
            clusters.append([span])
        else:
            clusters[-1].append(span)

    detector = region.get("detector_geometry")
    detector_center_local = crop_height / 2.0
    if isinstance(detector, dict):
        detector_y = _number(detector.get("y"), y)
        detector_height = _number(detector.get("height"), 0.0)
        detector_center_top = (1.0 - detector_y - detector_height / 2.0) * page_height
        detector_center_local = detector_center_top - top

    def cluster_score(cluster: list[tuple[int, int]]) -> tuple[float, float]:
        start, end = cluster[0][0], cluster[-1][1]
        if start <= detector_center_local <= end:
            distance = 0.0
        else:
            distance = min(abs(detector_center_local - start), abs(detector_center_local - end))
        ink_total = float(sum(row_counts[start : end + 1]))
        return distance, -ink_total

    selected = min(clusters, key=cluster_score)
    band_scores = [
        float(sum(row_counts[start : end + 1]))
        for start, end in selected
    ]
    strongest_band = max(band_scores, default=0.0)
    selected = [
        span for span, score in zip(selected, band_scores)
        if score >= max(2.0, strongest_band * 0.18)
    ]
    if not selected:
        return []

    rows_per_column = max(1, min(len(compact), len(selected)))
    y0, y1 = selected[0][0], selected[-1][1]
    column_counts = [
        sum(
            ink[row * crop_width + column]
            for row in range(y0, y1 + 1)
        )
        for column in range(crop_width)
    ]
    minimum_column_ink = max(2, int(round((y1 - y0 + 1) * 0.035)))
    column_active = [
        minimum_column_ink <= count
        for count in column_counts
    ]
    column_spans = [
        span for span in _contiguous_spans(column_active)
        if span[1] - span[0] + 1 >= 2
    ]
    if not column_spans:
        return []
    column_scores = [
        float(sum(column_counts[start : end + 1]))
        for start, end in column_spans
    ]
    strongest_column = max(column_scores, default=0.0)
    meaningful_columns = [
        span for span, score in zip(column_spans, column_scores)
        if score >= max(2.0, strongest_column * 0.12)
    ]
    if not meaningful_columns:
        return []

    x0 = min(start for start, _end in meaningful_columns)
    x1 = max(end for _start, end in meaningful_columns)
    column_count = max(1, math.ceil(len(compact) / rows_per_column))
    if column_count > 10 or x1 <= x0:
        return []

    cell_width = (x1 - x0 + 1) / column_count
    segments: list[dict[str, object]] = []
    for char_index, character in enumerate(compact):
        column_index = char_index // rows_per_column
        row_index = char_index % rows_per_column
        if column_index >= column_count or row_index >= len(selected):
            break
        row_start, row_end = selected[row_index]
        cell_left = x0 + (column_count - column_index - 1) * cell_width
        cell_right = x0 + (column_count - column_index) * cell_width
        page_left = left + cell_left
        page_right = left + cell_right
        page_top = top + row_start
        page_bottom = top + row_end + 1
        seg_x = max(0.0, min(1.0, page_left / page_width))
        seg_width = max(0.0, min(1.0 - seg_x, (page_right - page_left) / page_width))
        seg_height = max(0.0, min(1.0, (page_bottom - page_top) / page_height))
        seg_y = max(0.0, min(1.0 - seg_height, 1.0 - page_bottom / page_height))
        if seg_width <= 0.001 or seg_height <= 0.001:
            continue
        segments.append(
            {
                "text": character,
                "orientation": "vertical",
                "x": round(seg_x, 6),
                "y": round(seg_y, 6),
                "width": round(seg_width, 6),
                "height": round(seg_height, 6),
                "source": "ink-grid-v1",
            }
        )
    return segments if len(segments) >= max(2, len(compact) // 2) else []



def _ocr_crop_padding(
    image_width: int,
    image_height: int,
    region: dict[str, object],
) -> tuple[int, int]:
    """Return recognition-only padding without changing observed UI geometry.

    Raw undilated vertical lines can omit the first/last glyph when that glyph
    touches panel art and therefore never enters the connected-component run.
    Keep x padding unchanged (to avoid stealing a neighbouring manga column),
    but give these corroborated vertical proposals one glyph-edge of y context.
    """
    pad_x = max(4, round(image_width * 0.006))
    pad_y = max(4, round(image_height * 0.006))
    if (
        str(region.get("detector") or "") == _RAW_LAYOUT_DETECTOR
        and str(region.get("orientation") or "") == "vertical"
    ):
        pad_y = max(pad_y, round(image_height * 0.014))
    return pad_x, pad_y


def _crop_region_with_extra_y(
    image: Image.Image,
    region: dict[str, object],
    *,
    extra_y_ratio: float,
) -> Image.Image:
    width, height = image.size
    x = max(0.0, min(1.0, _number(region.get("x"))))
    y = max(0.0, min(1.0, _number(region.get("y"))))
    region_width = max(0.0, min(1.0 - x, _number(region.get("width"))))
    region_height = max(0.0, min(1.0 - y, _number(region.get("height"))))
    pad_x, pad_y = _ocr_crop_padding(width, height, region)
    pad_y = max(pad_y, round(height * extra_y_ratio))
    left = max(0, round(x * width) - pad_x)
    right = min(width, round((x + region_width) * width) + pad_x)
    top = max(0, round((1.0 - y - region_height) * height) - pad_y)
    bottom = min(height, round((1.0 - y) * height) + pad_y)
    if right <= left or bottom <= top:
        raise ValueError("empty OCR retry region")
    return image.crop((left, top, right, bottom)).convert("RGB")




def _crop_region_with_extra_context(
    image: Image.Image,
    region: dict[str, object],
    *,
    extra_x_ratio: float,
    extra_y_ratio: float,
) -> Image.Image:
    """Recognition-only context padding; observed region geometry is unchanged."""
    width, height = image.size
    x = max(0.0, min(1.0, _number(region.get("x"))))
    y = max(0.0, min(1.0, _number(region.get("y"))))
    region_width = max(0.0, min(1.0 - x, _number(region.get("width"))))
    region_height = max(0.0, min(1.0 - y, _number(region.get("height"))))
    pad_x, pad_y = _ocr_crop_padding(width, height, region)
    pad_x = max(pad_x, round(width * max(0.0, extra_x_ratio)))
    pad_y = max(pad_y, round(height * max(0.0, extra_y_ratio)))
    left = max(0, round(x * width) - pad_x)
    right = min(width, round((x + region_width) * width) + pad_x)
    top = max(0, round((1.0 - y - region_height) * height) - pad_y)
    bottom = min(height, round((1.0 - y) * height) + pad_y)
    if right <= left or bottom <= top:
        raise ValueError("empty OCR context region")
    return image.crop((left, top, right, bottom)).convert("RGB")


def _layout_context_candidate_plausible(
    region: dict[str, object],
    value: object,
) -> bool:
    """Guard wider retry crops from stealing an adjacent manga column."""
    compact = _compact_surface(value)
    if not _layout_text_usable(compact):
        return False
    width = max(1e-6, _number(region.get("width")))
    height = max(1e-6, _number(region.get("height")))
    aspect = height / width
    max_chars = max(4, int(math.ceil(aspect * 2.35)) + 2)
    japanese = _japanese_character_count(compact)
    semantic = [character for character in compact if character.isalnum() or "\u3040" <= character <= "\u9fff"]
    return (
        len(compact) <= max_chars
        and japanese >= 2
        and japanese / max(1, len(semantic)) >= 0.58
    )




def _layout_candidate_glyph_count(value: object) -> int:
    compact = _compact_surface(value)
    return sum(
        character.isalnum()
        or "\u3040" <= character <= "\u30ff"
        or "\u3400" <= character <= "\u9fff"
        or character in {"々", "〆", "ヶ", "ー"}
        for character in compact
    )


def _tall_dense_merged_layout_candidate(item: dict[str, object]) -> bool:
    """Identify a tall bold speech lane collapsed into one connected blob.

    Dilation can merge an entire bold vertical manga column into one component.
    Do not trust that shape by itself: this helper only establishes structural
    eligibility for a later direct+square OCR consensus. Sparse strokes/SFX and
    short single-kanji blobs stay outside this path.
    """
    if str(item.get("source") or "") != _LAYOUT_LINE_SOURCE:
        return False
    if str(item.get("detector") or "") != _LAYOUT_DETECTOR:
        return False
    if str(item.get("orientation") or "") != "vertical":
        return False
    provenance = item.get("provenance")
    if not isinstance(provenance, dict):
        return False
    component_count = int(provenance.get("component_count") or item.get("component_count") or 0)
    if component_count != 1:
        return False
    width = max(1e-6, _number(item.get("width")))
    height = max(1e-6, _number(item.get("height")))
    aspect = height / width
    coverage = float(provenance.get("component_coverage") or 0.0)
    black_ratio = float(provenance.get("black_ratio") or 0.0)
    white_ratio = float(provenance.get("white_ratio") or 0.0)
    midtone_ratio = float(provenance.get("midtone_ratio") or 1.0)
    return (
        height >= 0.10
        and width <= 0.065
        and aspect >= 2.60
        and coverage >= 0.85
        and 0.20 <= black_ratio <= 0.50
        and white_ratio >= 0.40
        and midtone_ratio <= 0.15
    )


def _record_tall_merged_ocr_consensus(
    item: dict[str, object],
    direct_text: object,
    square_text: object,
) -> bool:
    """Authorize merged-lane geometry only after two local OCR views agree.

    The structural heuristic alone is deliberately insufficient: dense artwork
    can also be one tall blob. The tight crop and square-padded crop must return
    exactly the same usable Japanese surface before component-count geometry is
    relaxed for this one proposal.
    """
    if not _tall_dense_merged_layout_candidate(item):
        return False
    direct = _compact_surface(direct_text)
    square = _compact_surface(square_text)
    if not direct or direct != square:
        return False
    if not _layout_text_usable(direct):
        return False
    if not _layout_context_candidate_plausible(item, direct):
        return False
    provenance = dict(item.get("provenance") or {})
    provenance["tall_merged_ocr_consensus"] = True
    provenance["tall_merged_ocr_consensus_kind"] = "direct+square-v1"
    provenance["tall_merged_ocr_consensus_text"] = direct
    item["provenance"] = provenance
    return True


def _layout_text_geometry_plausible(
    region: dict[str, object],
    value: object,
) -> bool:
    """Reject OCR strings that physically cannot fit the observed vertical lane.

    Context retries can read a neighbouring manga column and return a perfectly
    plausible Japanese phrase for a one-glyph-high proposal.  Require roughly
    glyph-sized vertical pitch before accepting a retry.  The bound is broad
    enough for small kana/punctuation, but removes cases such as a 3-kanji word
    stuffed into a ~1-glyph box.
    """
    compact = _compact_surface(value)
    glyphs = _layout_candidate_glyph_count(compact)
    if glyphs <= 0:
        return False
    width = max(1e-6, _number(region.get("width")))
    height = max(1e-6, _number(region.get("height")))
    pitch_ratio = height / max(1e-6, width * glyphs)
    if not (0.38 <= pitch_ratio <= 2.80):
        return False

    provenance = region.get("provenance")
    component_count = int(region.get("component_count") or 0)
    if component_count <= 0 and isinstance(provenance, dict):
        component_count = int(provenance.get("component_count") or 0)
    if isinstance(provenance, dict):
        raw_support = int(provenance.get("raw_component_count_support") or 0)
        raw_coverage = float(provenance.get("raw_component_coverage_support") or 0.0)
        if (
            provenance.get("raw_component_support_kind") == "partial-weak-same-lane-v1"
            and raw_support > component_count
            and raw_coverage >= 0.82
        ):
            component_count = raw_support
    if component_count > 0:
        provenance = region.get("provenance")
        merged_component = bool(
            isinstance(provenance, dict)
            and component_count == 1
            and height / max(width, 1e-6) >= 2.0
            and (
                provenance.get("single_merged_component")
                or (
                    provenance.get("tall_merged_ocr_consensus")
                    and _tall_dense_merged_layout_candidate(region)
                )
            )
        )
        if not merged_component:
            lower = max(1, int(math.floor(component_count * 0.55)))
            upper = max(component_count + 2, int(math.ceil(component_count * 1.75)))
            if not (lower <= glyphs <= upper):
                return False
    return True


def _layout_square_retry_supported(item: dict[str, object]) -> bool:
    """Do not turn weak pixel art into plausible Japanese via square retry.

    A retry is valuable for a high-coverage real lane, but v16 showed that
    3-4 loose components on faces/backgrounds can become convincing words such
    as 「そして」.  Require stronger pixel support for otherwise uncorroborated
    short primary proposals.  Merged tall components are explicitly preserved.
    """
    if str(item.get("source") or "") != _LAYOUT_LINE_SOURCE:
        return True
    provenance = item.get("provenance")
    if not isinstance(provenance, dict):
        return True
    if bool(provenance.get("single_merged_component")):
        return True
    if str(item.get("detector") or "") != _LAYOUT_DETECTOR:
        return True
    if provenance.get("support_kind"):
        return True
    component_count = int(provenance.get("component_count") or 0)
    coverage = float(provenance.get("component_coverage") or 0.0)
    black_ratio = float(provenance.get("black_ratio") or 0.0)
    midtone_ratio = float(provenance.get("midtone_ratio") or 1.0)
    if 2 <= component_count <= 4:
        return coverage >= 0.80 and black_ratio >= 0.07 and midtone_ratio <= 0.18
    return True


def _recognize_layout_square_retry(
    model: object,
    image: Image.Image,
    region: dict[str, object],
) -> str:
    """Retry a narrow vertical lane without aspect-ratio distortion.

    MangaOCR is much more stable when a 20x80 manga column is centred on a
    square white canvas instead of being stretched to the recognizer input.
    Upscale the padded crop as well so tiny print retains stroke separation.
    """
    crop = _crop_region_with_extra_context(
        image,
        region,
        extra_x_ratio=min(0.006, max(0.002, _number(region.get("width")) * 0.12)),
        extra_y_ratio=min(0.008, max(0.002, _number(region.get("height")) * 0.05)),
    )
    square: Image.Image | None = None
    enlarged: Image.Image | None = None
    try:
        square = _square_pad_dark_column_crop(crop)
        target = max(192, min(512, max(square.size) * 4))
        enlarged = square.resize((target, target), Image.Resampling.LANCZOS)
        return str(model(enlarged) or "").strip()  # type: ignore[operator]
    finally:
        if enlarged is not None:
            enlarged.close()
        if square is not None:
            square.close()
        crop.close()


def _recognize_layout_member_ensemble(
    model: object,
    image: Image.Image,
    region: dict[str, object],
) -> list[str]:
    """Run a few aspect-preserving views of one observed vertical lane.

    MangaOCR can be sensitive to tiny antialiasing/background differences. A
    real lane should survive at least two simple renderings, while artwork
    hallucinations are much less stable.  This is used only for recall donors.
    """
    outputs: list[str] = []
    try:
        outputs.append(str(_recognize_layout_square_retry(model, image, region) or ""))
    except Exception:
        pass
    crop = _crop_region_with_extra_context(
        image, region,
        extra_x_ratio=min(0.004, max(0.0015, _number(region.get("width")) * 0.08)),
        extra_y_ratio=min(0.006, max(0.0015, _number(region.get("height")) * 0.04)),
    )
    prepared: list[Image.Image] = []
    try:
        gray = ImageOps.autocontrast(crop.convert("L"))
        prepared.append(gray.convert("RGB"))
        # A high-contrast binary view removes screentone around speech glyphs.
        thresholded = gray.point(lambda value: 255 if int(value) >= 185 else 0).convert("RGB")
        prepared.append(thresholded)
        gray.close()
        for variant in prepared:
            square = _square_pad_dark_column_crop(variant)
            try:
                target = max(224, min(512, max(square.size) * 5))
                enlarged = square.resize((target, target), Image.Resampling.LANCZOS)
                try:
                    outputs.append(str(model(enlarged) or ""))  # type: ignore[operator]
                finally:
                    enlarged.close()
            finally:
                square.close()
    finally:
        for variant in prepared:
            variant.close()
        crop.close()
    return [_compact_surface(value) for value in outputs if _compact_surface(value)]


def _layout_member_consensus_candidate(
    item: dict[str, object],
    values: list[str],
    cluster_text: str,
) -> tuple[str, dict[str, object]]:
    valid = [value for value in values if len(value) <= 14 and _layout_retry_acceptable(item, value)]
    counts: dict[str, int] = {}
    for value in valid:
        counts[value] = counts.get(value, 0) + 1
    detail: dict[str, object] = {"variants": list(values), "valid": list(valid), "counts": dict(counts)}
    if not counts:
        return "", detail
    candidate, count = max(counts.items(), key=lambda pair: pair[1])
    if count >= 2:
        detail["accepted_by"] = "variant-consensus"
        return candidate, detail
    # One clean member read may still be used when it is literally present in
    # the wider cluster OCR and the pixel lane itself has strong component support.
    provenance = item.get("provenance") if isinstance(item.get("provenance"), dict) else {}
    coverage = float(provenance.get("component_coverage") or item.get("component_coverage") or 0.0)
    component_count = int(provenance.get("component_count") or item.get("component_count") or 0)
    if candidate in cluster_text and component_count >= 2 and coverage >= 0.72:
        detail["accepted_by"] = "cluster-substring"
        return candidate, detail
    return "", detail


def _layout_retry_acceptable(item: dict[str, object], value: object) -> bool:
    return (
        (
            _layout_text_usable(value)
            or _supported_short_layout_text(item, value)
        )
        and _layout_text_geometry_plausible(item, value)
    )

def _vertical_dense_span_is_crossing_rule(
    image: Image.Image,
    *,
    scan_left: int,
    scan_right: int,
    row_top: int,
    row_bottom: int,
    lane_width: int,
    threshold: int = 165,
) -> bool:
    """Distinguish a panel/frame rule from a dense glyph such as 一.

    Only inspect the contiguous dark run that actually intersects the text lane.
    Nearby manga columns on the same row must not make a short 一 look like a
    page-wide rule.
    """
    page_width, page_height = image.size
    if lane_width <= 0 or row_bottom <= row_top:
        return False
    extra = max(8, lane_width * 2)
    left = max(0, scan_left - extra)
    right = min(page_width, scan_right + extra)
    top = max(0, min(page_height - 1, row_top))
    bottom = max(top + 1, min(page_height, row_bottom))
    crop = image.crop((left, top, right, bottom)).convert("L")
    try:
        pixels = list(crop.getdata())
        width, height = crop.size
    finally:
        crop.close()
    if not pixels or width < 4:
        return False
    active = []
    for column in range(width):
        count = sum(
            1 for row in range(height)
            if int(pixels[row * width + column]) <= threshold
        )
        active.append(count >= max(1, int(round(height * 0.34))))
    spans = _contiguous_spans(active)
    if not spans:
        return False
    lane_start = scan_left - left
    lane_end = scan_right - left - 1
    lane_center = (lane_start + lane_end) / 2.0

    def span_score(span: tuple[int, int]) -> tuple[float, float]:
        start, end = span
        overlap = max(0, min(end, lane_end) - max(start, lane_start) + 1)
        center = (start + end) / 2.0
        return float(overlap), -abs(center - lane_center)

    chosen = max(spans, key=span_score)
    overlap = max(0, min(chosen[1], lane_end) - max(chosen[0], lane_start) + 1)
    if overlap <= 0:
        return False
    span_width = chosen[1] - chosen[0] + 1
    return span_width >= max(lane_width * 1.22, (scan_right - scan_left) * 1.04)


def _vertical_leading_ink_geometry(
    image: Image.Image,
    region: dict[str, object],
) -> dict[str, object] | None:
    """Extend a vertical text lane upward across adjacent glyph-sized ink.

    Recognition crops intentionally include a little y-context.  That lets
    MangaOCR read a leading glyph that the connected-component bbox omitted,
    but until now the UI geometry stayed clipped and every mapped token shifted
    by one slot.  Recover only directly adjacent, glyph-sized spans on the same
    narrow x lane; stop at large gaps, ruby-only specks and panel borders.
    """
    if str(region.get("orientation") or "") != "vertical":
        return None
    if str(region.get("source") or "") not in {
        _LAYOUT_LINE_SOURCE,
        "expanded-vision-rectangle",
        "expanded-vertical-seed",
        "layout-cluster-v1",
    }:
        return None

    page_width, page_height = image.size
    if page_width <= 0 or page_height <= 0:
        return None
    x = max(0.0, min(1.0, _number(region.get("x"))))
    y = max(0.0, min(1.0, _number(region.get("y"))))
    width = max(0.0, min(1.0 - x, _number(region.get("width"))))
    height = max(0.0, min(1.0 - y, _number(region.get("height"))))
    left = max(0, min(page_width - 1, round(x * page_width)))
    right = max(left + 1, min(page_width, round((x + width) * page_width)))
    current_top = max(0, min(page_height - 1, round((1.0 - y - height) * page_height)))
    current_bottom = max(current_top + 1, min(page_height, round((1.0 - y) * page_height)))
    lane_width = right - left
    lane_height = current_bottom - current_top
    if lane_width < 8 or lane_height < 16:
        return None

    side_pad = max(2, min(5, int(round(lane_width * 0.12))))
    scan_left = max(0, left - side_pad)
    scan_right = min(page_width, right + side_pad)
    max_extension = min(
        int(round(page_height * 0.16)),
        max(56, int(round(lane_height * 1.18))),
    )
    search_top = max(0, current_top - max_extension)
    search_bottom = min(page_height, current_bottom + 3)
    if search_bottom - search_top < 12:
        return None

    crop = image.crop((scan_left, search_top, scan_right, search_bottom)).convert("L")
    try:
        pixels = list(crop.getdata())
        crop_width, crop_height = crop.size
    finally:
        crop.close()
    if not pixels or crop_width < 4:
        return None

    # Black-on-white bubbles dominate this path.  A generous threshold keeps
    # antialiased strokes but ignores screentone/light paper texture.
    threshold = 165
    row_counts = [
        sum(1 for value in pixels[row * crop_width : (row + 1) * crop_width] if int(value) <= threshold)
        for row in range(crop_height)
    ]
    minimum_row_ink = max(2, int(round(crop_width * 0.055)))
    spans = _contiguous_spans([count >= minimum_row_ink for count in row_counts])
    spans = [span for span in spans if span[1] - span[0] + 1 >= 2]
    if not spans:
        return None

    current_top_local = current_top - search_top
    max_gap = max(8, min(17, int(round(crop_width * 0.52))))
    anchor_index: int | None = None
    best_distance = 10**9
    for index, (start, end) in enumerate(spans):
        if start <= current_top_local <= end:
            anchor_index = index
            best_distance = 0
            break
        distance = min(abs(start - current_top_local), abs(end - current_top_local))
        if start <= current_top_local + max_gap and end >= current_top_local - max_gap and distance < best_distance:
            anchor_index = index
            best_distance = distance
    if anchor_index is None:
        return None

    anchor_start, anchor_end = spans[anchor_index]
    robust_height = max(5, int(round(crop_width * 0.35)))
    first = anchor_index
    while first > 0:
        prev_start, prev_end = spans[first - 1]
        cur_start, _cur_end = spans[first]
        gap = cur_start - prev_end - 1
        prev_height = prev_end - prev_start + 1
        prev_peak_ratio = max(row_counts[prev_start : prev_end + 1] or [0]) / max(1.0, float(crop_width))
        # Dense glyphs such as 一 can legitimately saturate the narrow lane.
        # Reject them only when the same dark stroke continues far outside the
        # lane, which is the actual panel/frame signature.
        prev_global_top = search_top + prev_start
        prev_global_bottom = search_top + prev_end + 1
        crossing_rule = (
            prev_peak_ratio >= 0.88
            and _vertical_dense_span_is_crossing_rule(
                image,
                scan_left=scan_left,
                scan_right=scan_right,
                row_top=prev_global_top,
                row_bottom=prev_global_bottom,
                lane_width=lane_width,
            )
        )
        short_horizontal_glyph = (
            2 <= prev_height < robust_height
            and prev_peak_ratio >= 0.55
            and not crossing_rule
        )
        if gap > max_gap or (prev_height < robust_height and not short_horizontal_glyph) or crossing_rule:
            break
        first -= 1

    recovered_top = search_top + spans[first][0]
    extension = current_top - recovered_top
    if extension < max(4, int(round(lane_width * 0.15))):
        return None

    result = dict(region)
    new_height = min(1.0 - y, height + extension / page_height)
    result["height"] = round(new_height, 6)
    provenance = dict(result.get("provenance") or {})
    provenance["leading_ink_geometry"] = {
        "old_top_px": int(current_top),
        "new_top_px": int(recovered_top),
        "extension_px": int(extension),
        "max_gap_px": int(max_gap),
        "scan_x_px": [int(scan_left), int(scan_right)],
    }
    result["provenance"] = provenance
    result["leading_ink_geometry"] = True
    return result


def _vertical_trailing_ink_geometry(
    image: Image.Image,
    region: dict[str, object],
) -> dict[str, object] | None:
    """Extend a vertical text lane downward across adjacent glyph-sized ink."""
    if str(region.get("orientation") or "") != "vertical":
        return None
    if str(region.get("source") or "") not in {
        _LAYOUT_LINE_SOURCE,
        "expanded-vision-rectangle",
        "expanded-vertical-seed",
        "layout-cluster-v1",
    }:
        return None

    page_width, page_height = image.size
    x = max(0.0, min(1.0, _number(region.get("x"))))
    y = max(0.0, min(1.0, _number(region.get("y"))))
    width = max(0.0, min(1.0 - x, _number(region.get("width"))))
    height = max(0.0, min(1.0 - y, _number(region.get("height"))))
    left = max(0, min(page_width - 1, round(x * page_width)))
    right = max(left + 1, min(page_width, round((x + width) * page_width)))
    current_top = max(0, min(page_height - 1, round((1.0 - y - height) * page_height)))
    current_bottom = max(current_top + 1, min(page_height, round((1.0 - y) * page_height)))
    lane_width = right - left
    lane_height = current_bottom - current_top
    if lane_width < 8 or lane_height < 16:
        return None

    side_pad = max(2, min(5, int(round(lane_width * 0.12))))
    scan_left = max(0, left - side_pad)
    scan_right = min(page_width, right + side_pad)
    max_extension = min(
        int(round(page_height * 0.12)),
        max(48, int(round(lane_height * 0.72))),
    )
    search_top = max(0, current_top - 3)
    search_bottom = min(page_height, current_bottom + max_extension)
    if search_bottom - search_top < 12:
        return None

    crop = image.crop((scan_left, search_top, scan_right, search_bottom)).convert("L")
    try:
        pixels = list(crop.getdata())
        crop_width, crop_height = crop.size
    finally:
        crop.close()
    if not pixels or crop_width < 4:
        return None

    threshold = 165
    row_counts = [
        sum(1 for value in pixels[row * crop_width : (row + 1) * crop_width] if int(value) <= threshold)
        for row in range(crop_height)
    ]
    minimum_row_ink = max(2, int(round(crop_width * 0.055)))
    spans = _contiguous_spans([count >= minimum_row_ink for count in row_counts])
    spans = [span for span in spans if span[1] - span[0] + 1 >= 2]
    if not spans:
        return None

    current_bottom_local = current_bottom - search_top - 1
    max_gap = max(8, min(17, int(round(crop_width * 0.52))))
    anchor_index: int | None = None
    best_distance = 10**9
    for index, (start, end) in enumerate(spans):
        if start <= current_bottom_local <= end:
            anchor_index = index
            best_distance = 0
            break
        distance = min(abs(start - current_bottom_local), abs(end - current_bottom_local))
        if start <= current_bottom_local + max_gap and end >= current_bottom_local - max_gap and distance < best_distance:
            anchor_index = index
            best_distance = distance
    if anchor_index is None:
        return None

    robust_height = max(5, int(round(crop_width * 0.35)))
    last = anchor_index
    while last + 1 < len(spans):
        next_start, next_end = spans[last + 1]
        _cur_start, cur_end = spans[last]
        gap = next_start - cur_end - 1
        next_height = next_end - next_start + 1
        next_peak_ratio = max(row_counts[next_start : next_end + 1] or [0]) / max(1.0, float(crop_width))
        next_global_top = search_top + next_start
        next_global_bottom = search_top + next_end + 1
        crossing_rule = (
            next_peak_ratio >= 0.88
            and _vertical_dense_span_is_crossing_rule(
                image,
                scan_left=scan_left,
                scan_right=scan_right,
                row_top=next_global_top,
                row_bottom=next_global_bottom,
                lane_width=lane_width,
            )
        )
        short_horizontal_glyph = (
            2 <= next_height < robust_height
            and next_peak_ratio >= 0.55
            and not crossing_rule
        )
        touches_search_edge = (
            next_end >= crop_height - 2
            and next_height > max(robust_height * 1.6, int(round(lane_width * 1.2)))
        )
        overly_tall = next_height > max(robust_height * 2.6, int(round(lane_width * 1.9)))
        if gap > max_gap or (next_height < robust_height and not short_horizontal_glyph) or crossing_rule or overly_tall or touches_search_edge:
            break
        last += 1

    recovered_bottom = search_top + spans[last][1] + 1
    extension = recovered_bottom - current_bottom
    if extension < max(4, int(round(lane_width * 0.15))):
        return None

    result = dict(region)
    extension_norm = extension / page_height
    new_y = max(0.0, y - extension_norm)
    new_height = min(1.0 - new_y, height + extension_norm)
    result["y"] = round(new_y, 6)
    result["height"] = round(new_height, 6)
    provenance = dict(result.get("provenance") or {})
    provenance["trailing_ink_geometry"] = {
        "old_bottom_px": int(current_bottom),
        "new_bottom_px": int(recovered_bottom),
        "extension_px": int(extension),
        "max_gap_px": int(max_gap),
        "scan_x_px": [int(scan_left), int(scan_right)],
    }
    result["provenance"] = provenance
    result["trailing_ink_geometry"] = True
    return result


def _vertical_recovery_surface(value: object) -> str:
    return unicodedata.normalize("NFKC", _compact_surface(value))


def _leading_extension_has_centered_ink(
    image: Image.Image,
    extended: dict[str, object],
) -> bool:
    """Require recovered prefix ink to live in the lane, not its neighbours.

    The leading-edge geometry pass deliberately uses a narrow vertical scan,
    while MangaOCR gets a small x pad. On crowded balloons that x pad can see
    the previous column and hallucinate its last kana as a prefix. Compare the
    dark-pixel density in the middle 60% of the newly recovered strip with the
    outer 20% bands; a real missing glyph is center-supported, cross-column
    bleed is edge-dominated.
    """
    info = (extended.get("provenance") or {}).get("leading_ink_geometry") or {}
    try:
        new_top = int(info.get("new_top_px"))
        old_top = int(info.get("old_top_px"))
        scan_left, scan_right = [int(value) for value in info.get("scan_x_px", [])]
    except (TypeError, ValueError):
        return True
    if old_top <= new_top or scan_right < scan_left:
        return True

    width, height = image.size
    new_top = max(0, min(height, new_top))
    old_top = max(new_top, min(height, old_top))
    scan_left = max(0, min(width - 1, scan_left))
    scan_right = max(scan_left, min(width - 1, scan_right))
    lane_width = scan_right - scan_left + 1
    if old_top <= new_top or lane_width < 5:
        return True

    crop = image.crop((scan_left, new_top, scan_right + 1, old_top)).convert("L")
    try:
        pixels = list(crop.getdata())
        crop_width, crop_height = crop.size
    finally:
        crop.close()
    if not pixels or crop_width < 5 or crop_height < 2:
        return True

    side = max(1, int(round(crop_width * 0.20)))
    center_left = side
    center_right = crop_width - side
    if center_right <= center_left:
        return True

    center_dark = 0
    center_total = 0
    edge_dark = 0
    edge_total = 0
    for y in range(crop_height):
        row = y * crop_width
        for x in range(crop_width):
            dark = pixels[row + x] < 140
            if center_left <= x < center_right:
                center_total += 1
                center_dark += int(dark)
            else:
                edge_total += 1
                edge_dark += int(dark)

    if center_total <= 0 or edge_total <= 0:
        return True
    center_ratio = center_dark / center_total
    edge_ratio = edge_dark / edge_total
    return center_ratio >= max(0.06, edge_ratio * 1.05)


def _accept_vertical_leading_context_text(old_text: object, candidate_text: object) -> bool:
    old = _vertical_recovery_surface(old_text)
    candidate = _vertical_recovery_surface(candidate_text)
    if not old or not candidate:
        return False
    if candidate == old:
        return True
    if len(candidate) > len(old) + 7 or len(candidate) <= len(old):
        return False
    if candidate.endswith(old):
        prefix = candidate[: -len(old)]
        return 1 <= _japanese_character_count(prefix) <= 6

    punctuation = "!！?？。…‥〜～・、,．."
    old_core = old.rstrip(punctuation)
    candidate_core = candidate.rstrip(punctuation)
    if old_core and candidate_core.endswith(old_core) and len(candidate_core) > len(old_core):
        prefix = candidate_core[: -len(old_core)]
        return 1 <= _japanese_character_count(prefix) <= 6
    return False


def _leading_prefix_width_supported(
    image: Image.Image,
    extended: dict[str, object],
    old_text: object,
    candidate_text: object,
) -> bool:
    """Reject a recovered prefix whose glyph ink is mostly outside its lane.

    A leading-context crop may include a neighbouring column. MangaOCR can then
    prepend one kana even though the recovered y-strip belongs to the current
    lane. Pixel slot tightening exposes this failure: the alleged prefix needs
    a box much wider than the detector lane. Genuine recovered prefixes in the
    first-20 corpus stay well below this bound (for example 海賊 ~=1.44x); the
    p009 false ど expands to ~=2.10x.
    """
    old = _vertical_recovery_surface(old_text)
    candidate = _vertical_recovery_surface(candidate_text)
    if not old or not candidate or len(candidate) <= len(old):
        return True
    punctuation = "!！?？。…‥〜～・、,．."
    old_core = old.rstrip(punctuation)
    candidate_core = candidate.rstrip(punctuation)
    if candidate.endswith(old):
        prefix = candidate[: -len(old)]
    elif old_core and candidate_core.endswith(old_core) and candidate_core != old_core:
        prefix = candidate_core[: -len(old_core)]
    else:
        return True
    prefix_count = len(_study_surface_characters(prefix))
    if prefix_count <= 0:
        return True

    segments = _vertical_leading_ink_character_segments(image, extended, candidate)
    if len(segments) < prefix_count:
        return True
    tightened = _tighten_vertical_slot_ink_segments(image, segments)
    lane_width = max(1e-6, _number(extended.get("width")))
    for segment in tightened[:prefix_count]:
        source = str(segment.get("source") or "")
        width = max(0.0, _number(segment.get("width")))
        if "+x-context-v1" in source and width > lane_width * 1.80:
            return False
    return True


def _recover_vertical_leading_context(
    model: object,
    image: Image.Image,
    item: dict[str, object],
) -> dict[str, object]:
    """Synchronize OCR text and geometry when a line bbox missed its prefix."""
    extended = _vertical_leading_ink_geometry(image, item)
    if extended is None:
        return item
    old_text = str(item.get("text") or "").strip()
    try:
        crop = _crop_region(image, extended)
        try:
            candidate = str(model(crop) or "").strip()  # type: ignore[operator]
        finally:
            crop.close()
    except Exception:
        return item
    if not _accept_vertical_leading_context_text(old_text, candidate):
        return item
    old_surface = _vertical_recovery_surface(old_text)
    candidate_surface = _vertical_recovery_surface(candidate)
    punctuation = "!！?？。…‥〜～・、,．."
    old_core = old_surface.rstrip(punctuation)
    candidate_core = candidate_surface.rstrip(punctuation)
    added_prefix = (
        candidate_surface != old_surface
        and (
            candidate_surface.endswith(old_surface)
            or (old_core and candidate_core.endswith(old_core) and candidate_core != old_core)
        )
    )
    if added_prefix and not _leading_extension_has_centered_ink(image, extended):
        return item
    if added_prefix and not _leading_prefix_width_supported(image, extended, old_text, candidate):
        return item
    if candidate_surface == old_surface:
        info = (extended.get("provenance") or {}).get("leading_ink_geometry") or {}
        extension_px = int(_number(info.get("extension_px"), 0.0))
        page_width = max(1, image.size[0])
        lane_width_px = max(1, int(round(_number(item.get("width")) * page_width)))
        # Geometry-only recovery is intentionally limited to roughly one glyph.
        # Larger extensions require OCR to actually recover a missing prefix.
        if extension_px > max(48, int(round(lane_width_px * 1.30))):
            return item

    result = dict(extended)
    result["text"] = candidate or old_text
    result["raw_text"] = candidate or old_text
    result["recognizer_retry"] = "vertical-leading-ink-v1"
    hypotheses = [dict(value) for value in item.get("hypotheses", []) if isinstance(value, dict)]
    for hypothesis in hypotheses:
        hypothesis["selected"] = False
    hypotheses.append(
        {
            "id": "manga-ocr-leading-ink",
            "text": candidate or old_text,
            "source": "manga-ocr",
            "selected": True,
        }
    )
    result["hypotheses"] = hypotheses
    result["selected_hypothesis_id"] = "manga-ocr-leading-ink"
    return result


def _accept_vertical_trailing_context_text(old_text: object, candidate_text: object) -> bool:
    old = _vertical_recovery_surface(old_text)
    candidate = _vertical_recovery_surface(candidate_text)
    if not old or not candidate or candidate == old:
        return candidate == old
    if len(candidate) > len(old) + 4 or len(candidate) <= len(old):
        return False
    if candidate.startswith(old):
        suffix = candidate[len(old) :]
        return 1 <= _japanese_character_count(suffix) <= 3
    punctuation = "!！?？。…‥〜～・、,．."
    old_core = old.rstrip(punctuation)
    candidate_core = candidate.rstrip(punctuation)
    if old_core and candidate_core.startswith(old_core) and len(candidate_core) > len(old_core):
        suffix = candidate_core[len(old_core) :]
        return 1 <= _japanese_character_count(suffix) <= 3
    return False


def _recover_vertical_trailing_context(
    model: object,
    image: Image.Image,
    item: dict[str, object],
) -> dict[str, object]:
    extended = _vertical_trailing_ink_geometry(image, item)
    if extended is None:
        return item
    old_text = str(item.get("text") or "").strip()
    try:
        crop = _crop_region(image, extended)
        try:
            candidate = str(model(crop) or "").strip()  # type: ignore[operator]
        finally:
            crop.close()
    except Exception:
        return item
    if not _accept_vertical_trailing_context_text(old_text, candidate):
        return item
    result = dict(extended)
    result["text"] = candidate or old_text
    result["raw_text"] = candidate or old_text
    result["recognizer_retry"] = "vertical-trailing-ink-v1"
    hypotheses = [dict(value) for value in item.get("hypotheses", []) if isinstance(value, dict)]
    for hypothesis in hypotheses:
        hypothesis["selected"] = False
    hypotheses.append(
        {
            "id": "manga-ocr-trailing-ink",
            "text": candidate or old_text,
            "source": "manga-ocr",
            "selected": True,
        }
    )
    result["hypotheses"] = hypotheses
    result["selected_hypothesis_id"] = "manga-ocr-trailing-ink"
    return result


def _recover_vertical_edge_context(
    model: object, image: Image.Image, item: dict[str, object]
) -> dict[str, object]:
    result = _recover_vertical_leading_context(model, image, item)
    return _recover_vertical_trailing_context(model, image, result)


def _crop_region(image: Image.Image, region: dict[str, object]) -> Image.Image:
    width, height = image.size
    x = max(0.0, min(1.0, _number(region.get("x"))))
    y = max(0.0, min(1.0, _number(region.get("y"))))
    region_width = max(0.0, min(1.0 - x, _number(region.get("width"))))
    region_height = max(0.0, min(1.0 - y, _number(region.get("height"))))
    pad_x, pad_y = _ocr_crop_padding(width, height, region)
    left = max(0, round(x * width) - pad_x)
    right = min(width, round((x + region_width) * width) + pad_x)
    top = max(0, round((1.0 - y - region_height) * height) - pad_y)
    bottom = min(height, round((1.0 - y) * height) + pad_y)
    if right <= left or bottom <= top:
        raise ValueError("empty OCR region")
    return image.crop((left, top, right, bottom)).convert("RGB")




def _segment_center_y(segment: dict[str, object]) -> float:
    return float(segment.get("y") or 0.0) + float(segment.get("height") or 0.0) / 2.0


def _geometry_union(items: list[dict[str, object]]) -> dict[str, float] | None:
    if not items:
        return None
    x1 = min(float(item.get("x") or 0.0) for item in items)
    y1 = min(float(item.get("y") or 0.0) for item in items)
    x2 = max(float(item.get("x") or 0.0) + float(item.get("width") or 0.0) for item in items)
    y2 = max(float(item.get("y") or 0.0) + float(item.get("height") or 0.0) for item in items)
    return {"x": x1, "y": y1, "width": max(0.0, x2 - x1), "height": max(0.0, y2 - y1)}


def _horizontal_overlap_ratio(left: dict[str, object], right: dict[str, object]) -> float:
    left_y1 = float(left.get("y") or 0.0)
    left_y2 = left_y1 + float(left.get("height") or 0.0)
    right_y1 = float(right.get("y") or 0.0)
    right_y2 = right_y1 + float(right.get("height") or 0.0)
    overlap = max(0.0, min(left_y2, right_y2) - max(left_y1, right_y1))
    return overlap / max(1e-9, min(float(left.get("height") or 0.0), float(right.get("height") or 0.0)))



def _horizontal_segment_x_bounds(item: dict[str, object]) -> tuple[float, float]:
    x1 = float(item.get("x") or 0.0)
    return x1, x1 + float(item.get("width") or 0.0)


def _horizontal_height_components(rows: list[dict[str, object]]) -> list[list[int]]:
    """Split exact Vision observations into coherent same-height x lanes."""
    if not rows:
        return []
    indexed = list(enumerate(rows))
    height_groups: list[list[int]] = []
    for index, row in sorted(indexed, key=lambda pair: float(pair[1].get("height") or 0.0)):
        height = float(row.get("height") or 0.0)
        placed = False
        for group in height_groups:
            median = statistics.median(float(rows[item].get("height") or 0.0) for item in group)
            if abs(height - median) <= max(0.0025, median * 0.12):
                group.append(index)
                placed = True
                break
        if not placed:
            height_groups.append([index])

    components: list[list[int]] = []
    for group in height_groups:
        ordered = sorted(group, key=lambda item: float(rows[item].get("x") or 0.0))
        widths = [float(rows[item].get("width") or 0.0) for item in ordered]
        median_width = statistics.median(widths) if widths else 0.0
        maximum_gap = max(0.025, median_width * 1.6)
        component = [ordered[0]]
        for index in ordered[1:]:
            previous = component[-1]
            previous_end = float(rows[previous].get("x") or 0.0) + float(rows[previous].get("width") or 0.0)
            gap = float(rows[index].get("x") or 0.0) - previous_end
            if gap <= maximum_gap:
                component.append(index)
            else:
                components.append(component)
                component = [index]
        components.append(component)
    return components


def _component_span(rows: list[dict[str, object]], component: list[int]) -> tuple[float, float]:
    return (
        min(_horizontal_segment_x_bounds(rows[index])[0] for index in component),
        max(_horizontal_segment_x_bounds(rows[index])[1] for index in component),
    )


def _component_overlap_ratio(
    rows: list[dict[str, object]], left: list[int], right: list[int]
) -> float:
    left_x1, left_x2 = _component_span(rows, left)
    right_x1, right_x2 = _component_span(rows, right)
    overlap = max(0.0, min(left_x2, right_x2) - max(left_x1, right_x1))
    return overlap / max(1e-9, min(left_x2 - left_x1, right_x2 - right_x1))


def _dedupe_competing_horizontal_lanes(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Remove duplicate Accurate Vision passes without dropping real neighbours.

    Some TOC rows arrive twice: one ~4% page-height lane and one ~5% lane,
    offset by a fraction of a glyph.  Keeping both interleaves duplicate text
    and makes word hitboxes overlap.  Prefer the tighter lane, but preserve an
    unmatched prefix/suffix (for example the leading 第) from the other pass.
    """
    if len(rows) < 6:
        return rows
    rows = [dict(item) for item in rows]
    components = [component for component in _horizontal_height_components(rows) if len(component) >= 3]
    suppressed: set[int] = set()
    for left_index, left in enumerate(components):
        for right in components[left_index + 1 :]:
            left_height = statistics.median(float(rows[item].get("height") or 0.0) for item in left)
            right_height = statistics.median(float(rows[item].get("height") or 0.0) for item in right)
            if min(left_height, right_height) <= 0:
                continue
            if max(left_height, right_height) / min(left_height, right_height) < 1.12:
                continue
            if _component_overlap_ratio(rows, left, right) < 0.50:
                continue
            winner, loser = (left, right) if left_height < right_height else (right, left)
            winner_text = "".join(str(rows[item].get("text") or "") for item in winner)
            loser_text = "".join(str(rows[item].get("text") or "") for item in loser)
            matcher = difflib.SequenceMatcher(a=winner_text, b=loser_text, autojunk=False)
            if matcher.ratio() < 0.50:
                continue
            matched_loser_positions: set[int] = set()
            for block in matcher.get_matching_blocks():
                matched_loser_positions.update(range(block.b, block.b + block.size))
            winner_x1, winner_x2 = _component_span(rows, winner)
            for position, item in enumerate(loser):
                x1, x2 = _horizontal_segment_x_bounds(rows[item])
                center = (x1 + x2) / 2.0
                if position in matched_loser_positions or winner_x1 - 0.002 <= center <= winner_x2 + 0.002:
                    suppressed.add(item)
    if not suppressed:
        return rows
    return [item for index, item in enumerate(rows) if index not in suppressed]


def _strip_horizontal_page_number_tail(rows: list[dict[str, object]]) -> tuple[list[dict[str, object]], bool]:
    """Drop far-right TOC page numbers/leader OCR; they are not Jiten targets."""
    if len(rows) < 5:
        return rows, False
    ordered = sorted((dict(item) for item in rows), key=lambda item: float(item.get("x") or 0.0))
    allowed = set("0123456789-—–ー―")
    far_right = []
    digit_count = 0
    for index, item in enumerate(ordered):
        text = unicodedata.normalize("NFKC", str(item.get("text") or "")).strip()
        x = float(item.get("x") or 0.0)
        if x >= 0.78 and text and all(character in allowed for character in text):
            far_right.append(index)
            digit_count += sum(character.isdigit() for character in text)
    if digit_count < 1:
        return ordered, False
    # This path is deliberately TOC-shaped: a chapter label starts on the left,
    # while the numeric navigation column is on the far right. Avoid touching a
    # normal short horizontal sentence containing a number.
    leftmost = min(float(item.get("x") or 0.0) for item in ordered)
    if leftmost > 0.22:
        return ordered, False
    drop = set(far_right)
    # Vision sometimes reads the leader immediately before the page number as
    # 一/ー. Remove that visual navigation artifact too when it is to the right
    # of the title body.
    for index, item in enumerate(ordered):
        text = unicodedata.normalize("NFKC", str(item.get("text") or "")).strip()
        if float(item.get("x") or 0.0) >= 0.55 and text in {"一", "ー", "―", "—", "-"}:
            drop.add(index)
    kept = [item for index, item in enumerate(ordered) if index not in drop]
    return (kept if kept else ordered), bool(drop)


def _refine_horizontal_segment_ink(image: Image.Image, segment: dict[str, object]) -> dict[str, object]:
    """Tighten an Accurate Vision range box to the main printed glyph ink.

    Range boxes can include furigana above a kanji.  We first select the
    strongest contiguous horizontal ink band, then bound ink only inside that
    band.  This keeps the core hitbox on the base glyph instead of ruby.
    """
    item = dict(segment)
    if str(item.get("source") or "") not in {
        "vision-accurate-range-v2",
        "vision-accurate-aligned-v4",
    }:
        return item
    page_width, page_height = image.size
    x = max(0.0, min(1.0, float(item.get("x") or 0.0)))
    y = max(0.0, min(1.0, float(item.get("y") or 0.0)))
    width = max(0.0, min(1.0 - x, float(item.get("width") or 0.0)))
    height = max(0.0, min(1.0 - y, float(item.get("height") or 0.0)))
    left = max(0, min(page_width - 1, int(round(x * page_width))))
    right = max(left + 1, min(page_width, int(round((x + width) * page_width))))
    top = max(0, min(page_height - 1, int(round((1.0 - y - height) * page_height))))
    bottom = max(top + 1, min(page_height, int(round((1.0 - y) * page_height))))
    crop = image.crop((left, top, right, bottom)).convert("L")
    try:
        pixels = list(crop.getdata())
        crop_width, crop_height = crop.size
    finally:
        crop.close()
    if not pixels or crop_width < 2 or crop_height < 2:
        return item
    ordered_pixels = sorted(int(value) for value in pixels)
    p20 = ordered_pixels[max(0, int(len(ordered_pixels) * 0.20) - 1)]
    p80 = ordered_pixels[min(len(ordered_pixels) - 1, int(len(ordered_pixels) * 0.80))]
    threshold = min(175, max(70, int((p20 + p80) * 0.48)))
    mask = [int(value) <= threshold for value in pixels]
    if sum(mask) < max(3, int(crop_width * crop_height * 0.008)):
        return item

    row_counts = [
        sum(mask[row * crop_width : (row + 1) * crop_width])
        for row in range(crop_height)
    ]
    active_rows = [count >= max(1, int(round(crop_width * 0.035))) for count in row_counts]
    spans: list[tuple[int, int]] = []
    start_row: int | None = None
    for row, active in enumerate(active_rows + [False]):
        if active and start_row is None:
            start_row = row
        elif not active and start_row is not None:
            spans.append((start_row, row - 1))
            start_row = None
    # Merge one-pixel antialiasing gaps inside a main glyph, but keep the larger
    # furigana-to-base gap separate.
    merged: list[tuple[int, int]] = []
    for span in spans:
        if merged and span[0] - merged[-1][1] - 1 <= 1:
            merged[-1] = (merged[-1][0], span[1])
        else:
            merged.append(span)
    spans = merged
    band_top, band_bottom = 0, crop_height - 1
    if spans:
        candidates = []
        for span_top, span_bottom in spans:
            span_height = span_bottom - span_top + 1
            ink_count = sum(row_counts[span_top : span_bottom + 1])
            # Base glyphs dominate by ink/height and usually sit below ruby.
            lower_bonus = 1.0 + 0.10 * ((span_top + span_bottom) / max(1, 2 * crop_height))
            score = ink_count * max(1.0, span_height ** 0.5) * lower_bonus
            candidates.append((score, span_height, span_top, span_bottom))
        _score, chosen_height, chosen_top, chosen_bottom = max(candidates)
        if chosen_height >= max(3, int(round(crop_height * 0.24))):
            band_top = max(0, chosen_top - 1)
            band_bottom = min(crop_height - 1, chosen_bottom + 1)

    ink = [
        (index % crop_width, index // crop_width)
        for index, active in enumerate(mask)
        if active and band_top <= index // crop_width <= band_bottom
    ]
    if len(ink) < 3:
        return item
    xs = [point[0] for point in ink]
    ys = [point[1] for point in ink]
    ink_left = max(0, min(xs) - 1)
    ink_right = min(crop_width, max(xs) + 2)
    ink_top = max(0, min(ys) - 1)
    ink_bottom = min(crop_height, max(ys) + 2)
    if ink_right - ink_left < 2 or ink_bottom - ink_top < 2:
        return item
    page_left = left + ink_left
    page_right = left + ink_right
    page_top = top + ink_top
    page_bottom = top + ink_bottom
    refined_x = page_left / page_width
    refined_width = (page_right - page_left) / page_width
    refined_height = (page_bottom - page_top) / page_height
    refined_y = 1.0 - page_bottom / page_height
    area_before = max(1e-9, width * height)
    area_after = refined_width * refined_height
    if area_after < area_before * 0.06 or area_after > area_before * 1.05:
        return item
    item.update(
        {
            "x": round(refined_x, 6),
            "y": round(refined_y, 6),
            "width": round(refined_width, 6),
            "height": round(refined_height, 6),
            "source": "vision-accurate-ink-v4",
        }
    )
    return item




def _recover_vertical_slot_x_ink(image: Image.Image, segment: dict[str, object]) -> dict[str, object]:
    """Recover glyph ink clipped just outside a narrow vertical detector lane.

    Search only left/right within the character's own y-slot and keep the ink
    cluster that overlaps the original detector box. This can recover the left
    strokes of 村/は without stealing the neighbouring vertical text column.
    """
    item = dict(segment)
    if str(item.get("orientation") or "") != "vertical":
        return item
    source = str(item.get("source") or "")
    if not source.startswith(("layout-line-ink-v2", "vertical-leading-ink-v2")):
        return item

    page_width, page_height = image.size
    x = max(0.0, min(1.0, float(item.get("x") or 0.0)))
    y = max(0.0, min(1.0, float(item.get("y") or 0.0)))
    width = max(0.0, min(1.0 - x, float(item.get("width") or 0.0)))
    height = max(0.0, min(1.0 - y, float(item.get("height") or 0.0)))
    left = max(0, min(page_width - 1, int(round(x * page_width))))
    right = max(left + 1, min(page_width, int(round((x + width) * page_width))))
    top = max(0, min(page_height - 1, int(round((1.0 - y - height) * page_height))))
    bottom = max(top + 1, min(page_height, int(round((1.0 - y) * page_height))))
    box_width = right - left
    box_height = bottom - top
    if box_width < 6 or box_height < 6:
        return item

    pad_x = max(2, min(int(round(page_width * 0.020)), int(round(box_width * 0.58))))
    scan_left = max(0, left - pad_x)
    scan_right = min(page_width, right + pad_x)
    crop = image.crop((scan_left, top, scan_right, bottom)).convert("L")
    try:
        pixels = [int(value) for value in crop.getdata()]
        crop_width, crop_height = crop.size
    finally:
        crop.close()
    if not pixels or crop_width < box_width:
        return item
    column_counts = [
        sum(1 for row in range(crop_height) if pixels[row * crop_width + column] <= 165)
        for column in range(crop_width)
    ]
    minimum_column_ink = max(1, int(round(crop_height * 0.08)))
    spans = _contiguous_spans([count >= minimum_column_ink for count in column_counts])
    if not spans:
        return item
    spans = _merge_small_gaps(spans, max(1, min(2, int(round(crop_height * 0.10)))))

    original_left = left - scan_left
    original_right = right - scan_left - 1
    candidates: list[tuple[float, int, int]] = []
    for span_left, span_right in spans:
        overlap = max(0, min(span_right, original_right) - max(span_left, original_left) + 1)
        if overlap <= 0:
            continue
        ink_total = sum(column_counts[span_left : span_right + 1])
        candidates.append((overlap * 1000.0 + ink_total, span_left, span_right))
    if not candidates:
        return item
    _score, span_left, span_right = max(candidates)
    recovered_left = max(scan_left, scan_left + span_left - 1)
    recovered_right = min(scan_right, scan_left + span_right + 2)
    left_extension = max(0, left - recovered_left)
    right_extension = max(0, recovered_right - right)
    if left_extension > box_width * 0.70 or right_extension > box_width * 0.70:
        return item
    if left_extension < 2 and right_extension < 2:
        return item

    new_x = recovered_left / page_width
    new_width = (recovered_right - recovered_left) / page_width
    item["x"] = round(new_x, 6)
    item["width"] = round(new_width, 6)
    item["source"] = f"{source}+x-context-v1"
    item["x_context_recovery"] = {
        "old_px": [int(left), int(right)],
        "new_px": [int(recovered_left), int(recovered_right)],
    }
    return item


def _tighten_vertical_slot_ink(image: Image.Image, segment: dict[str, object]) -> dict[str, object]:
    """Tighten synthetic vertical glyph slots to their observed ink footprint.

    Layout/grid allocation gives every character a coarse equal-height slot.
    That is good enough for ordering, but it leaves visibly offset hitboxes on
    mixed kana/kanji columns (e.g. いたって, じゃね, 海賊). Refine inside the
    slot only, so ordering stays intact while the box better hugs the real ink.
    """
    item = dict(segment)
    if str(item.get("orientation") or "") != "vertical":
        return item
    source = str(item.get("source") or "")
    if not source.startswith((
        "layout-line-ink-v2",
        "vertical-leading-ink-v2",
        "ink-grid-v1",
        "ink-columns-v2",
        "dark-columns-v1",
    )):
        return item

    page_width, page_height = image.size
    x = max(0.0, min(1.0, float(item.get("x") or 0.0)))
    y = max(0.0, min(1.0, float(item.get("y") or 0.0)))
    width = max(0.0, min(1.0 - x, float(item.get("width") or 0.0)))
    height = max(0.0, min(1.0 - y, float(item.get("height") or 0.0)))
    left = max(0, min(page_width - 1, int(round(x * page_width))))
    right = max(left + 1, min(page_width, int(round((x + width) * page_width))))
    top = max(0, min(page_height - 1, int(round((1.0 - y - height) * page_height))))
    bottom = max(top + 1, min(page_height, int(round((1.0 - y) * page_height))))
    crop = image.crop((left, top, right, bottom)).convert("L")
    try:
        pixels = list(crop.getdata())
        crop_width, crop_height = crop.size
    finally:
        crop.close()
    if not pixels or crop_width < 2 or crop_height < 2:
        return item

    ordered = sorted(int(value) for value in pixels)
    p20 = ordered[max(0, int(len(ordered) * 0.20) - 1)]
    p80 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.80))]
    threshold = min(190, max(55, int((p20 + p80) * 0.48)))
    mask = [int(value) <= threshold for value in pixels]
    if sum(mask) < max(3, int(round(crop_width * crop_height * 0.010))):
        return item

    column_counts = [
        sum(mask[row * crop_width + column] for row in range(crop_height))
        for column in range(crop_width)
    ]
    row_counts = [
        sum(mask[row * crop_width : (row + 1) * crop_width])
        for row in range(crop_height)
    ]
    min_col = max(1, int(round(crop_height * 0.10)))
    min_row = max(1, int(round(crop_width * 0.10)))
    active_cols = [index for index, count in enumerate(column_counts) if count >= min_col]
    active_rows = [index for index, count in enumerate(row_counts) if count >= min_row]
    if not active_cols or not active_rows:
        return item

    ink_left = max(0, active_cols[0] - 1)
    ink_right = min(crop_width, active_cols[-1] + 2)
    ink_top = max(0, active_rows[0] - 1)
    ink_bottom = min(crop_height, active_rows[-1] + 2)

    min_width = max(2, int(round(crop_width * 0.35)))
    min_height = max(2, int(round(crop_height * 0.35)))
    if ink_right - ink_left < min_width:
        pad = min_width - (ink_right - ink_left)
        left_pad = pad // 2
        right_pad = pad - left_pad
        ink_left = max(0, ink_left - left_pad)
        ink_right = min(crop_width, ink_right + right_pad)
    if ink_bottom - ink_top < min_height:
        pad = min_height - (ink_bottom - ink_top)
        top_pad = pad // 2
        bottom_pad = pad - top_pad
        ink_top = max(0, ink_top - top_pad)
        ink_bottom = min(crop_height, ink_bottom + bottom_pad)
    if ink_right - ink_left < 2 or ink_bottom - ink_top < 2:
        return item

    page_left = left + ink_left
    page_right = left + ink_right
    page_top = top + ink_top
    page_bottom = top + ink_bottom
    refined_x = page_left / page_width
    refined_width = (page_right - page_left) / page_width
    refined_height = (page_bottom - page_top) / page_height
    refined_y = 1.0 - page_bottom / page_height

    area_before = max(1e-9, width * height)
    area_after = refined_width * refined_height
    if area_after < area_before * 0.14 or area_after > area_before * 1.06:
        return item
    item.update(
        {
            "x": round(refined_x, 6),
            "y": round(refined_y, 6),
            "width": round(refined_width, 6),
            "height": round(refined_height, 6),
            "source": f"{str(item.get('source') or 'layout-line-ink-v2')}+tight-v1",
        }
    )
    return item


def _tighten_vertical_slot_ink_segments(
    image: Image.Image, segments: list[dict[str, object]]
) -> list[dict[str, object]]:
    tightened = [
        _tighten_vertical_slot_ink(image, _recover_vertical_slot_x_ink(image, dict(segment)))
        for segment in segments
    ]
    return tightened if tightened else segments


def _expand_narrow_horizontal_piece_segments(piece: dict[str, object]) -> dict[str, object]:
    """Broaden obviously over-tight horizontal glyph boxes within local gaps.

    Tight ink refinement is desirable for ruby-heavy rows, but extremely narrow
    terminal boxes (notably 話 in 第1話) become frustrating one-pixel click
    targets. Expand only unusually narrow single-glyph segments and stay inside
    the free gap to neighbouring boxes.
    """
    segments = piece.get("segments")
    if not isinstance(segments, list) or len(segments) < 2:
        return piece
    prepared = [dict(segment) for segment in segments if isinstance(segment, dict)]
    if len(prepared) < 2:
        return piece
    widths = sorted(max(1e-6, float(segment.get("width") or 0.0)) for segment in prepared)
    reference_width = widths[len(widths) // 2]
    if reference_width <= 0:
        return piece
    right_edge = max(float(piece.get("x") or 0.0) + float(piece.get("width") or 0.0), 0.0)
    left_edge = max(0.0, float(piece.get("x") or 0.0))
    updated: list[dict[str, object]] = []
    changed = False
    for index, segment in enumerate(prepared):
        current = dict(segment)
        surface = _study_surface_characters(current.get("text"))
        width = max(1e-6, float(current.get("width") or 0.0))
        height = max(1e-6, float(current.get("height") or 0.0))
        if len(surface) != 1 or width >= min(reference_width * 0.64, height * 0.92):
            updated.append(current)
            continue
        left_limit = left_edge if index == 0 else float(prepared[index - 1].get("x") or 0.0) + float(prepared[index - 1].get("width") or 0.0)
        right_limit = right_edge if index == len(prepared) - 1 else float(prepared[index + 1].get("x") or 0.0)
        gap_left = max(0.0, float(current.get("x") or 0.0) - left_limit)
        gap_right = max(0.0, right_limit - (float(current.get("x") or 0.0) + width))
        target = min(max(width, min(reference_width * 0.72, height * 0.98)), width + gap_left + gap_right)
        if target <= width * 1.12:
            updated.append(current)
            continue
        grow = target - width
        grow_left = min(gap_left, grow / 2.0)
        grow_right = min(gap_right, grow - grow_left)
        leftover = grow - grow_left - grow_right
        if leftover > 1e-6 and gap_right - grow_right > gap_left - grow_left:
            extra = min(leftover, gap_right - grow_right)
            grow_right += extra
            leftover -= extra
        if leftover > 1e-6:
            grow_left += min(leftover, gap_left - grow_left)
        current["x"] = round(max(0.0, float(current.get("x") or 0.0) - grow_left), 6)
        current["width"] = round(width + grow_left + grow_right, 6)
        source = str(current.get("source") or "")
        current["source"] = f"{source}+narrow-expand-v1".strip("+")
        updated.append(current)
        changed = True
    if not changed:
        return piece
    result = dict(piece)
    result["segments"] = updated
    geometry = _geometry_union(updated)
    if geometry is not None:
        result.update(geometry)
        source = str(result.get("geometry_source") or "")
        result["geometry_source"] = f"{source}+horizontal-narrow-expand-v1".strip("+")
    return result


def _weak_synthetic_vertical_region(item: dict[str, object]) -> bool:
    """Reject synthetic vertical regions whose segment coverage is implausibly sparse."""
    if str(item.get("orientation") or "") != "vertical":
        return False
    if str(item.get("source") or "") not in {"expanded-vision-rectangle", "expanded-vertical-seed"}:
        return False
    segments = [segment for segment in item.get("segments") or [] if isinstance(segment, dict)]
    if len(segments) < 2:
        return False
    union = _geometry_union(segments)
    if union is None:
        return False
    region_height = max(1e-9, float(item.get("height") or 0.0))
    region_width = max(1e-9, float(item.get("width") or 0.0))
    union_height = max(1e-9, float(union.get("height") or 0.0))
    union_width = max(1e-9, float(union.get("width") or 0.0))
    height_ratio = union_height / region_height
    width_ratio = union_width / region_width
    areas = [max(0.0, float(seg.get("width") or 0.0) * float(seg.get("height") or 0.0)) for seg in segments]
    fill_ratio = sum(areas) / max(1e-9, region_width * region_height)
    texts = [str(seg.get("text") or "") for seg in segments]
    tiny_followups = sum(1 for seg in segments if float(seg.get("height") or 0.0) <= region_height * 0.08)
    if height_ratio >= 0.42:
        return False
    if fill_ratio >= 0.13 and not (
        width_ratio >= 0.95
        and tiny_followups >= max(2, len(segments) // 2)
        and height_ratio <= 0.30
    ):
        return False
    if width_ratio >= 0.92 and tiny_followups <= max(1, len(segments) // 3):
        return False
    item["synthetic_rejection"] = {
        "reason": "weak-segment-coverage-v1",
        "union_height_ratio": round(height_ratio, 4),
        "union_width_ratio": round(width_ratio, 4),
        "fill_ratio": round(fill_ratio, 4),
        "tiny_segment_count": tiny_followups,
        "segment_text": texts,
    }
    return True



def _synthetic_vertical_texture_noise(image: Image.Image, item: dict[str, object]) -> bool:
    """Reject expanded synthetic lanes that sit on artwork/panel borders.

    MangaOCR can produce perfectly plausible Japanese from character art. Real
    speech balloons in this recovery path have a very white background with
    sparse black glyph ink; screentone/line-art hallucinations have much more
    midtone texture. A second edge rule removes long punctuation-heavy strips
    accidentally built from the page/panel border.
    """
    if str(item.get("orientation") or "") != "vertical":
        return False
    if str(item.get("source") or "") not in {"expanded-vision-rectangle", "expanded-vertical-seed"}:
        return False
    page_width, page_height = image.size
    x = max(0.0, min(1.0, _number(item.get("x"))))
    y = max(0.0, min(1.0, _number(item.get("y"))))
    width = max(0.0, min(1.0 - x, _number(item.get("width"))))
    height = max(0.0, min(1.0 - y, _number(item.get("height"))))
    if width <= 0.001 or height <= 0.001:
        return False
    left = max(0, min(page_width - 1, int(round(x * page_width))))
    right = max(left + 1, min(page_width, int(round((x + width) * page_width))))
    top = max(0, min(page_height - 1, int(round((1.0 - y - height) * page_height))))
    bottom = max(top + 1, min(page_height, int(round((1.0 - y) * page_height))))
    crop = image.crop((left, top, right, bottom)).convert("L")
    try:
        histogram = crop.histogram()
    finally:
        crop.close()
    total = max(1, sum(histogram))
    black_ratio = sum(histogram[:100]) / total
    white_ratio = sum(histogram[220:]) / total
    midtone_ratio = max(0.0, 1.0 - black_ratio - white_ratio)
    compact = unicodedata.normalize("NFKC", _compact_surface(item.get("text")))
    japanese = _japanese_character_count(compact)
    punctuation = sum(character in "！？!?。…‥〜～ー―—−・、,.．" for character in compact)
    aspect = height / max(width, 1e-9)
    near_edge = x <= 0.035 or x + width >= 0.955

    texture_noise = (
        midtone_ratio >= 0.175
        and white_ratio <= 0.76
        and black_ratio <= 0.14
    )
    edge_strip_noise = (
        near_edge
        and aspect >= 5.0
        and japanese <= 2
        and punctuation >= 2
        and height >= 0.18
    )
    if not (texture_noise or edge_strip_noise):
        return False
    item["synthetic_rejection"] = {
        "reason": "texture-noise-v1" if texture_noise else "edge-strip-noise-v1",
        "black_ratio": round(black_ratio, 4),
        "white_ratio": round(white_ratio, 4),
        "midtone_ratio": round(midtone_ratio, 4),
        "aspect": round(aspect, 3),
    }
    return True

def _normalize_line_surface(value: object) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")))


def _study_surface_characters(value: object) -> list[str]:
    normalized = _normalize_line_surface(value)
    return [
        character
        for character in normalized
        if character.isalnum()
        or "\u3040" <= character <= "\u30ff"
        or "\u3400" <= character <= "\u9fff"
        or character in {"々", "〆", "ヶ", "ー"}
    ]


_SMALL_KANA_TO_LARGE = {
    "ぁ": "あ", "ぃ": "い", "ぅ": "う", "ぇ": "え", "ぉ": "お",
    "っ": "つ", "ゃ": "や", "ゅ": "ゆ", "ょ": "よ", "ゎ": "わ",
    "ァ": "ア", "ィ": "イ", "ゥ": "ウ", "ェ": "エ", "ォ": "オ",
    "ッ": "ツ", "ャ": "ヤ", "ュ": "ユ", "ョ": "ヨ", "ヮ": "ワ",
    "ヵ": "カ", "ヶ": "ケ",
}


def _repair_horizontal_small_kana_consensus(piece: dict[str, object]) -> dict[str, object]:
    """Accept only strongly anchored large→small kana corrections from line OCR.

    Vision is generally better than MangaOCR for the whole horizontal line, but
    it sometimes normalizes a printed small kana (notably っ) to its full-size
    form.  A weak MangaOCR line hypothesis may still be useful for that one bit
    of typography.  Keep every unrelated Vision character and change only a
    size-variant pair with matching neighbours on both sides at the same offset.
    """
    current = _normalize_line_surface(piece.get("text"))
    alternate = _normalize_line_surface(piece.get("line_ocr_text"))
    segments = [dict(item) for item in piece.get("segments") or [] if isinstance(item, dict)]
    if not current or not alternate or not segments:
        return piece
    stream = "".join(str(item.get("text") or "") for item in segments)
    if _normalize_line_surface(stream) != current or len(stream) != len(current):
        return piece

    corrected = list(current)
    repairs: list[dict[str, object]] = []
    limit = min(len(current), len(alternate))
    for index in range(limit):
        small = alternate[index]
        large = current[index]
        if _SMALL_KANA_TO_LARGE.get(small) != large:
            continue
        left_support = any(
            current[pos] == alternate[pos]
            for pos in range(max(0, index - 2), index)
        )
        right_support = any(
            current[pos] == alternate[pos]
            for pos in range(index + 1, min(limit, index + 4))
        )
        if not (left_support and right_support):
            continue
        corrected[index] = small
        segments[index]["text"] = small
        segments[index]["recognition_correction"] = "mangaocr-small-kana-consensus-v1"
        repairs.append({"index": index, "from": large, "to": small})

    if not repairs:
        return piece
    result = dict(piece)
    surface = "".join(corrected)
    result["text"] = surface
    result["raw_text"] = surface
    result["segments"] = segments
    result["small_kana_consensus"] = repairs
    return result


def _recognize_wide_horizontal_segment(
    model: object,
    image: Image.Image,
    segment: dict[str, object],
) -> str:
    """Re-read one suspiciously wide Vision character box with MangaOCR."""
    page_width, page_height = image.size
    x = max(0.0, min(1.0, _number(segment.get("x"))))
    y = max(0.0, min(1.0, _number(segment.get("y"))))
    width = max(0.0, min(1.0 - x, _number(segment.get("width"))))
    height = max(0.0, min(1.0 - y, _number(segment.get("height"))))
    left = max(0, min(page_width - 1, int(round(x * page_width))))
    right = max(left + 1, min(page_width, int(round((x + width) * page_width))))
    top = max(0, min(page_height - 1, int(round((1.0 - y - height) * page_height))))
    bottom = max(top + 1, min(page_height, int(round((1.0 - y) * page_height))))
    if right - left < 8 or bottom - top < 6:
        return ""
    crop = image.crop((left, top, right, bottom)).convert("RGB")
    square: Image.Image | None = None
    try:
        square = _square_pad_dark_column_crop(crop)
        gray = square.convert("L").resize((12, 12), Image.Resampling.BOX)
        try:
            mean = sum(int(value) for value in gray.getdata()) / 144.0
        finally:
            gray.close()
        ocr_crop = ImageOps.invert(square) if mean < 128 else square
        try:
            recognized = _normalize_line_surface(model(ocr_crop))  # type: ignore[operator]
        finally:
            if ocr_crop is not square:
                ocr_crop.close()
    except Exception:
        return ""
    finally:
        if square is not None:
            square.close()
        crop.close()
    return recognized




def _recognize_horizontal_lower_band(
    model: object,
    image: Image.Image,
    geometry: dict[str, object],
    *,
    trim_ratio: float = 0.24,
) -> str:
    """Re-read a horizontal glyph run while suppressing furigana above it."""
    page_width, page_height = image.size
    x = max(0.0, min(1.0, _number(geometry.get("x"))))
    y = max(0.0, min(1.0, _number(geometry.get("y"))))
    width = max(0.0, min(1.0 - x, _number(geometry.get("width"))))
    height = max(0.0, min(1.0 - y, _number(geometry.get("height"))))
    left = max(0, min(page_width - 1, int(round(x * page_width))))
    right = max(left + 1, min(page_width, int(round((x + width) * page_width))))
    top = max(0, min(page_height - 1, int(round((1.0 - y - height) * page_height))))
    bottom = max(top + 1, min(page_height, int(round((1.0 - y) * page_height))))
    crop_height = bottom - top
    if right - left < 12 or crop_height < 10:
        return ""
    trimmed_top = min(bottom - 6, top + int(round(crop_height * max(0.0, min(0.45, trim_ratio)))))
    crop = image.crop((left, trimmed_top, right, bottom)).convert("RGB")
    prepared: Image.Image | None = None
    try:
        prepared = ImageOps.expand(crop, border=max(4, int(round(crop.height * 0.14))), fill="white")
        if prepared.height < 56:
            scale = 56 / max(1, prepared.height)
            prepared = prepared.resize(
                (max(1, int(round(prepared.width * scale))), 56),
                Image.Resampling.LANCZOS,
            )
        return _normalize_line_surface(model(prepared))  # type: ignore[operator]
    except Exception:
        return ""
    finally:
        if prepared is not None:
            prepared.close()
        crop.close()




def _recognize_horizontal_baseline_masked(
    model: object,
    image: Image.Image,
    segments: list[dict[str, object]],
) -> str:
    """OCR a horizontal run using the shared base-glyph band, not ruby height.

    Accurate Vision often gives one kanji a taller bbox because furigana is
    included. Using the median neighbouring base height and common baseline
    masks that ruby while preserving normal glyph aspect.
    """
    if len(segments) < 2:
        return ""
    page_width, page_height = image.size
    left = min(_number(segment.get("x")) for segment in segments)
    right = max(_number(segment.get("x")) + _number(segment.get("width")) for segment in segments)
    bottoms = [1.0 - _number(segment.get("y")) for segment in segments]
    heights = sorted(max(1e-6, _number(segment.get("height"))) for segment in segments)
    base_height = heights[len(heights) // 2]
    bottom_norm = statistics.median(bottoms)
    top_norm = max(0.0, bottom_norm - base_height * 1.10)
    px_left = max(0, min(page_width - 1, int(math.floor(left * page_width))))
    px_right = max(px_left + 1, min(page_width, int(math.ceil(right * page_width))))
    px_top = max(0, min(page_height - 1, int(math.floor(top_norm * page_height))))
    px_bottom = max(px_top + 1, min(page_height, int(math.ceil(bottom_norm * page_height))))
    if px_right - px_left < 12 or px_bottom - px_top < 8:
        return ""
    crop = image.crop((px_left, px_top, px_right, px_bottom)).convert("RGB")
    prepared: Image.Image | None = None
    try:
        border = max(5, int(round(crop.height * 0.18)))
        prepared = ImageOps.expand(crop, border=border, fill="white")
        if prepared.height < 72:
            scale = 72 / max(1, prepared.height)
            resized = prepared.resize(
                (max(1, int(round(prepared.width * scale))), 72),
                Image.Resampling.LANCZOS,
            )
            prepared.close()
            prepared = resized
        return _normalize_line_surface(model(prepared))  # type: ignore[operator]
    except Exception:
        return ""
    finally:
        if prepared is not None:
            prepared.close()
        crop.close()

def _repair_wide_horizontal_segments_with_mangaocr(
    model: object,
    image: Image.Image,
    piece: dict[str, object],
) -> dict[str, object]:
    """Recover one swallowed glyph from an anomalously wide Vision range box.

    Accurate Vision occasionally labels two adjacent printed glyphs as one
    character while preserving a bbox almost exactly two normal glyph widths
    wide.  Re-read only that local crop.  Split it only when MangaOCR returns
    exactly two study characters and the first agrees with Vision, so unrelated
    line-level MangaOCR errors cannot rewrite the surrounding sentence.
    """
    if str(piece.get("orientation") or "") != "horizontal":
        return piece
    if _japanese_character_count(piece.get("text")) == 0:
        return piece
    segments = [dict(item) for item in piece.get("segments") or [] if isinstance(item, dict)]
    if len(segments) < 4:
        return piece
    old_stream = "".join(str(item.get("text") or "") for item in segments)
    if _normalize_line_surface(old_stream) != _normalize_line_surface(piece.get("text")):
        return piece

    widths = [
        float(item.get("width") or 0.0)
        for item in segments
        if len(_study_surface_characters(item.get("text"))) == 1
        and float(item.get("width") or 0.0) > 0.0
    ]
    if len(widths) < 4:
        return piece
    reference_width = statistics.median(widths)
    if reference_width <= 1e-6:
        return piece

    output: list[dict[str, object]] = []
    repairs: list[dict[str, object]] = []
    for index, segment in enumerate(segments):
        chars = _study_surface_characters(segment.get("text"))
        width = float(segment.get("width") or 0.0)
        ratio = width / reference_width if reference_width > 0 else 0.0
        if len(chars) != 1 or not (1.65 <= ratio <= 2.55):
            output.append(segment)
            continue

        local_text = _recognize_wide_horizontal_segment(model, image, segment)
        local_chars = _study_surface_characters(local_text)
        if len(local_chars) != 2 or local_chars[0] != chars[0]:
            output.append(segment)
            continue
        if index + 1 < len(segments):
            next_chars = _study_surface_characters(segments[index + 1].get("text"))
            if len(next_chars) == 1 and local_chars[1] == next_chars[0]:
                output.append(segment)
                continue

        first = dict(segment)
        second = dict(segment)
        half = width / 2.0
        first.update({
            "text": local_chars[0],
            "width": round(half, 6),
            "source": "vision-accurate-split-v1",
            "recognition_correction": "mangaocr-wide-segment-v1",
        })
        second.update({
            "text": local_chars[1],
            "x": round(float(segment.get("x") or 0.0) + half, 6),
            "width": round(width - half, 6),
            "source": "vision-accurate-split-v1",
            "recognition_correction": "mangaocr-wide-segment-v1",
        })
        output.extend((first, second))
        repairs.append({
            "segment_index": index,
            "from": chars[0],
            "to": "".join(local_chars),
            "width_ratio": round(ratio, 3),
        })

    if not repairs:
        return piece
    result = dict(piece)
    surface = "".join(str(item.get("text") or "") for item in output)
    result["text"] = surface
    result["raw_text"] = surface
    result["segments"] = output
    result["wide_segment_repairs"] = repairs
    result["geometry_source"] = f"{str(piece.get('geometry_source') or '')}+wide-segment-v1".strip("+")
    result["word_geometry"] = "mapped_segments"
    return result


def _is_kanji_character(value: object) -> bool:
    text = str(value or "")
    return len(text) == 1 and "\u3400" <= text <= "\u9fff"


def _compact_text_index_map(value: object) -> tuple[str, list[int]]:
    original = str(value or "")
    compact: list[str] = []
    indices: list[int] = []
    for source_index, character in enumerate(original):
        normalized = unicodedata.normalize("NFKC", character)
        for normalized_character in normalized:
            if normalized_character.isspace():
                continue
            compact.append(normalized_character)
            indices.append(source_index)
    return "".join(compact), indices


def _replace_compact_pair_at(value: object, compact_index: int, replacement: str) -> str:
    original = str(value or "")
    compact, indices = _compact_text_index_map(original)
    if compact_index < 0 or compact_index + 1 >= len(compact) or len(indices) != len(compact):
        return original
    start = indices[compact_index]
    end = indices[compact_index + 1] + 1
    return original[:start] + replacement + original[end:]


def _candidate_pair_text_index(
    text: object,
    segments: list[dict[str, object]],
    pair_index: int,
) -> int | None:
    """Locate one two-kanji Vision pair inside the selected OCR surface.

    The first kanji itself may disagree (`管険` geometry vs `皆険` OCR), so use
    the anchored second kanji plus neighbouring glyphs before giving up.
    """
    compact, _indices = _compact_text_index_map(text)
    if pair_index < 0 or pair_index + 1 >= len(segments):
        return None
    first = str(segments[pair_index].get("text") or "")
    second = str(segments[pair_index + 1].get("text") or "")
    exact = compact.find(first + second)
    if exact >= 0:
        return exact

    previous = ""
    following = ""
    if pair_index > 0:
        prev_chars = _study_surface_characters(segments[pair_index - 1].get("text"))
        if len(prev_chars) == 1:
            previous = prev_chars[0]
    if pair_index + 2 < len(segments):
        next_chars = _study_surface_characters(segments[pair_index + 2].get("text"))
        if len(next_chars) == 1:
            following = next_chars[0]

    candidates: list[tuple[int, int]] = []
    for position in range(max(0, len(compact) - 1)):
        if compact[position + 1] != second:
            continue
        score = 1
        if previous and position > 0 and compact[position - 1] == previous:
            score += 2
        if following and position + 2 < len(compact) and compact[position + 2] == following:
            score += 3
        candidates.append((score, position))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None
    return candidates[0][1]


def _extract_two_kanji_candidate(value: object) -> str:
    """Extract exactly two kanji while ignoring surrounding decoration/ruby noise."""
    chars = _study_surface_characters(value)
    kanji = [character for character in chars if _is_kanji_character(character)]
    if len(kanji) != 2:
        return ""
    return "".join(kanji)


def _detector_pair_support(piece: dict[str, object], candidate: str) -> bool:
    if not candidate:
        return False
    surfaces = [str(piece.get("raw_text") or ""), str(piece.get("text") or "")]
    for hypothesis in piece.get("hypotheses") or []:
        if isinstance(hypothesis, dict) and str(hypothesis.get("source") or "") != "manga-ocr":
            surfaces.append(str(hypothesis.get("text") or ""))
    normalized_candidate = _normalize_line_surface(candidate)
    return any(normalized_candidate in _normalize_line_surface(surface) for surface in surfaces if surface)


def _repair_short_kanji_pairs_with_mangaocr(
    model: object,
    image: Image.Image,
    piece: dict[str, object],
) -> dict[str, object]:
    """Repair a locally misread two-kanji compound without rewriting the line.

    This is deliberately invoked only when full-crop MangaOCR was rejected in
    favour of exact Vision geometry *and* the selected text disagrees with the
    Vision character pair. A tight MangaOCR crop may then correct one character
    while the other character acts as an anchor (e.g. 管険/皆険 -> 冒険).
    """
    if str(piece.get("orientation") or "") != "horizontal":
        return piece
    if str(piece.get("selected_hypothesis_id") or "") != "detector-recognition":
        return piece
    segments = [dict(item) for item in piece.get("segments") or [] if isinstance(item, dict)]
    if len(segments) < 3:
        return piece

    result_text = str(piece.get("text") or "")
    repairs: list[dict[str, object]] = []
    attempt_log: list[dict[str, object]] = []
    attempts = 0
    for index in range(len(segments) - 1):
        left_chars = _study_surface_characters(segments[index].get("text"))
        right_chars = _study_surface_characters(segments[index + 1].get("text"))
        if len(left_chars) != 1 or len(right_chars) != 1:
            continue
        old_pair = left_chars[0] + right_chars[0]
        if not all(_is_kanji_character(character) for character in old_pair):
            continue
        if not all(
            str(segments[position].get("source") or "").startswith("vision-accurate")
            for position in (index, index + 1)
        ):
            continue
        if _horizontal_overlap_ratio(segments[index], segments[index + 1]) < 0.72:
            continue
        target_index = _candidate_pair_text_index(result_text, segments, index)
        if target_index is None:
            continue
        compact_text, _ = _compact_text_index_map(result_text)
        selected_pair = compact_text[target_index : target_index + 2]
        attempts += 1
        if attempts > 4:
            break

        union = _geometry_union([segments[index], segments[index + 1]])
        if union is None:
            continue
        local_text = _recognize_wide_horizontal_segment(model, image, union)
        attempt_entry: dict[str, object] = {
            "segment_index": index,
            "vision": old_pair,
            "selected": selected_pair,
            "local_ocr": local_text,
            "lower_band": [],
        }
        attempt_log.append(attempt_entry)
        candidate_votes: dict[str, int] = {}
        variant_texts = [local_text]
        for trim_ratio in (0.18, 0.28, 0.36):
            lower_text = _recognize_horizontal_lower_band(
                model, image, union, trim_ratio=trim_ratio
            )
            attempt_entry["lower_band"].append({"trim": trim_ratio, "text": lower_text})  # type: ignore[union-attr]
            variant_texts.append(lower_text)
        baseline_text = _recognize_horizontal_baseline_masked(
            model, image, [segments[index], segments[index + 1]]
        )
        attempt_entry["baseline_masked"] = baseline_text
        variant_texts.append(baseline_text)
        for variant in variant_texts:
            pair = _extract_two_kanji_candidate(variant)
            if pair:
                candidate_votes[pair] = candidate_votes.get(pair, 0) + 1
        candidate = ""
        evidence_count = 0
        if candidate_votes:
            candidate, evidence_count = max(
                candidate_votes.items(),
                key=lambda value: (value[1], value[0] == selected_pair, value[0] == old_pair),
            )
        attempt_entry["candidate_votes"] = dict(candidate_votes)

        # A bare two-kanji crop is sometimes too little context for MangaOCR.
        # Retry with one neighbouring observed glyph and use that glyph only as
        # an anchor; the neighbour itself is never rewritten.
        if not candidate and index + 2 < len(segments):
            next_chars = _study_surface_characters(segments[index + 2].get("text"))
            if len(next_chars) == 1:
                context_union = _geometry_union([segments[index], segments[index + 1], segments[index + 2]])
                if context_union is not None:
                    context_text = _recognize_wide_horizontal_segment(model, image, context_union)
                    context_chars = _study_surface_characters(context_text)
                    if not (
                        len(context_chars) == 3
                        and context_chars[2] == next_chars[0]
                        and all(_is_kanji_character(character) for character in context_chars[:2])
                    ):
                        for trim_ratio in (0.18, 0.28, 0.36):
                            lower_text = _recognize_horizontal_lower_band(
                                model, image, context_union, trim_ratio=trim_ratio
                            )
                            lower_chars = _study_surface_characters(lower_text)
                            if (
                                len(lower_chars) == 3
                                and lower_chars[2] == next_chars[0]
                                and all(_is_kanji_character(character) for character in lower_chars[:2])
                            ):
                                context_chars = lower_chars
                                break
                    if (
                        len(context_chars) == 3
                        and context_chars[2] == next_chars[0]
                        and all(_is_kanji_character(character) for character in context_chars[:2])
                    ):
                        candidate = "".join(context_chars[:2])
                    if not candidate:
                        baseline_context = _recognize_horizontal_baseline_masked(
                            model, image, [segments[index], segments[index + 1], segments[index + 2]]
                        )
                        baseline_chars = _study_surface_characters(baseline_context)
                        if (
                            len(baseline_chars) == 3
                            and baseline_chars[2] == next_chars[0]
                            and all(_is_kanji_character(character) for character in baseline_chars[:2])
                        ):
                            candidate = "".join(baseline_chars[:2])
        if not candidate and index > 0:
            prev_chars = _study_surface_characters(segments[index - 1].get("text"))
            if len(prev_chars) == 1:
                context_union = _geometry_union([segments[index - 1], segments[index], segments[index + 1]])
                if context_union is not None:
                    context_text = _recognize_wide_horizontal_segment(model, image, context_union)
                    context_chars = _study_surface_characters(context_text)
                    if not (
                        len(context_chars) == 3
                        and context_chars[0] == prev_chars[0]
                        and all(_is_kanji_character(character) for character in context_chars[1:])
                    ):
                        for trim_ratio in (0.18, 0.28, 0.36):
                            lower_text = _recognize_horizontal_lower_band(
                                model, image, context_union, trim_ratio=trim_ratio
                            )
                            lower_chars = _study_surface_characters(lower_text)
                            if (
                                len(lower_chars) == 3
                                and lower_chars[0] == prev_chars[0]
                                and all(_is_kanji_character(character) for character in lower_chars[1:])
                            ):
                                context_chars = lower_chars
                                break
                    if (
                        len(context_chars) == 3
                        and context_chars[0] == prev_chars[0]
                        and all(_is_kanji_character(character) for character in context_chars[1:])
                    ):
                        candidate = "".join(context_chars[1:])
                    if not candidate:
                        baseline_context = _recognize_horizontal_baseline_masked(
                            model, image, [segments[index - 1], segments[index], segments[index + 1]]
                        )
                        baseline_chars = _study_surface_characters(baseline_context)
                        if (
                            len(baseline_chars) == 3
                            and baseline_chars[0] == prev_chars[0]
                            and all(_is_kanji_character(character) for character in baseline_chars[1:])
                        ):
                            candidate = "".join(baseline_chars[1:])
        if not candidate:
            continue
        attempt_entry["candidate"] = candidate
        attempt_entry["candidate_evidence_count"] = evidence_count
        detector_support = _detector_pair_support(piece, candidate)
        attempt_entry["candidate_detector_support"] = detector_support
        # If the selected detector surface and Accurate Vision already agree on
        # this pair, tiny-crop consensus alone is not allowed to overrule them.
        # v19 violated this and changed correct 栄一/海軍/夜明 into 宋一/毎軍/夜呼.
        # A different detector/full-line hypothesis may still authorize a local
        # correction (e.g. 大左 -> 大佐). When detector text and segment geometry
        # disagree, repeated local evidence remains useful (管険/皆険 -> 冒険).
        if selected_pair == old_pair and not detector_support:
            # v20 deliberately protects detector-selected Japanese surfaces
            # from tiny-crop hallucinations.  Keep that protection generally,
            # but mixed Latin/Japanese title rows have an extra independent
            # signal: all five pair-crop variants can agree while the detector
            # differs in exactly one kanji.  This case occurs on compact
            # bilingual mastheads where ruby/decoration confuses Vision.
            compact_result = _normalize_line_surface(result_text)
            latin_title_count = sum(
                character.isascii() and character.isalpha()
                for character in compact_result
            )
            unanimous_title_pair = (
                latin_title_count >= 6
                and evidence_count == len(variant_texts)
                and evidence_count >= 5
                and len(candidate_votes) == 1
            )
            if not unanimous_title_pair:
                continue
        # A single tiny-crop OCR is not enough to rewrite observed kanji.
        # Accept either repeated local evidence or explicit support from the
        # detector/full-line hypothesis.
        if evidence_count < 2 and not detector_support:
            continue
        anchored = sum(candidate[pos] == old_pair[pos] for pos in range(2))
        if anchored != 1 or candidate == old_pair:
            continue

        segments[index]["text"] = candidate[0]
        segments[index + 1]["text"] = candidate[1]
        segments[index]["recognition_correction"] = "mangaocr-short-kanji-pair-v2"
        segments[index + 1]["recognition_correction"] = "mangaocr-short-kanji-pair-v2"
        result_text = _replace_compact_pair_at(result_text, target_index, candidate)
        repairs.append(
            {
                "segment_index": index,
                "vision": old_pair,
                "selected": selected_pair,
                "local_ocr": candidate,
            }
        )

    if not repairs:
        if not attempt_log:
            return piece
        result = dict(piece)
        result["short_kanji_pair_attempts"] = attempt_log
        return result
    result = dict(piece)
    result["text"] = result_text
    result["raw_text"] = result_text
    result["segments"] = segments
    result["short_kanji_pair_repairs"] = repairs
    result["short_kanji_pair_attempts"] = attempt_log
    result["geometry_source"] = f"{str(piece.get('geometry_source') or '')}+short-kanji-pair-v2".strip("+")
    return result




_LATIN_RUBY_READINGS = {
    "エー": "A", "エイ": "A",
    "ビー": "B",
    "シー": "C",
    "ディー": "D", "デイー": "D",
    "イー": "E",
    "エフ": "F",
    "ジー": "G",
    "エイチ": "H",
    "アイ": "I",
    "ジェー": "J", "ジェイ": "J",
    "ケー": "K", "ケイ": "K",
    "エル": "L",
    "エム": "M",
    "エヌ": "N",
    "オー": "O",
    "ピー": "P",
    "キュー": "Q",
    "アール": "R",
    "エス": "S",
    "ティー": "T", "テイー": "T",
    "ユー": "U",
    "ブイ": "V", "ヴィー": "V",
    "ダブリュー": "W",
    "エックス": "X",
    "ワイ": "Y",
    "ゼット": "Z", "ズィー": "Z",
}


def _replace_compact_character_at(value: object, compact_index: int, replacement: str) -> str:
    original = str(value or "")
    compact, indices = _compact_text_index_map(original)
    if compact_index < 0 or compact_index >= len(compact) or len(indices) != len(compact):
        return original
    source_index = indices[compact_index]
    return original[:source_index] + replacement + original[source_index + 1 :]


def _recognize_ruby_above_segment(
    model: object,
    image: Image.Image,
    segment: dict[str, object],
) -> str:
    page_width, page_height = image.size
    x = max(0.0, min(1.0, _number(segment.get("x"))))
    y = max(0.0, min(1.0, _number(segment.get("y"))))
    width = max(0.0, min(1.0 - x, _number(segment.get("width"))))
    height = max(0.0, min(1.0 - y, _number(segment.get("height"))))
    base_left = x * page_width
    base_right = (x + width) * page_width
    base_top = (1.0 - y - height) * page_height
    glyph_height = max(8.0, height * page_height)
    glyph_width = max(8.0, width * page_width)
    left = max(0, int(math.floor(base_left - glyph_width * 1.05)))
    right = min(page_width, int(math.ceil(base_right + glyph_width * 1.05)))
    top = max(0, int(math.floor(base_top - glyph_height * 4.0)))
    bottom = min(page_height, max(top + 1, int(math.floor(base_top - 1))))
    if right - left < 8 or bottom - top < 6:
        return ""
    crop = image.crop((left, top, right, bottom)).convert("RGB")
    focused: Image.Image | None = None
    prepared: Image.Image | None = None
    try:
        gray = crop.convert("L")
        try:
            values = list(gray.getdata())
            if not values:
                return ""
            ordered = sorted(int(value) for value in values)
            p20 = ordered[max(0, int(len(ordered) * 0.20) - 1)]
            p80 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.80))]
            threshold = min(205, max(75, int((p20 + p80) * 0.50)))
            mask = gray.point(lambda value: 255 if int(value) <= threshold else 0)
            try:
                components = _binary_components(mask)
            finally:
                mask.close()
        finally:
            gray.close()

        # Ruby is normally the bottom-most small glyph cluster above the base
        # character. Long bubble/panel rules are discarded by size/aspect.
        candidates: list[tuple[int, int, int, int, int]] = []
        crop_width, crop_height = crop.size
        center_x = crop_width / 2.0
        for cx, cy, cw, ch, area in components:
            if area < 2 or cw <= 0 or ch <= 0:
                continue
            if cw >= crop_width * 0.58 or ch >= crop_height * 0.55:
                continue
            if cy + ch < crop_height * 0.30:
                continue
            component_center = cx + cw / 2.0
            if abs(component_center - center_x) > crop_width * 0.40:
                continue
            if cw > max(20, glyph_width * 1.55) or ch > max(18, glyph_height * 1.10):
                continue
            candidates.append((cx, cy, cw, ch, area))
        if candidates:
            # Keep a coherent bottom cluster; dakuten/long mark can be separate
            # components, so start from the bottom-most component then include
            # nearby small components in the same ruby row.
            anchor = max(candidates, key=lambda item: item[1] + item[3])
            anchor_mid_y = anchor[1] + anchor[3] / 2.0
            row = [
                item for item in candidates
                if abs((item[1] + item[3] / 2.0) - anchor_mid_y) <= max(12.0, glyph_height * 0.85)
            ]
            x1 = min(item[0] for item in row)
            y1 = min(item[1] for item in row)
            x2 = max(item[0] + item[2] for item in row)
            y2 = max(item[1] + item[3] for item in row)
            pad = max(2, int(round(min(crop_width, crop_height) * 0.04)))
            left2 = max(0, x1 - pad)
            top2 = max(0, y1 - pad)
            right2 = min(crop_width, x2 + pad)
            bottom2 = min(crop_height, y2 + pad)
            # Do not leave bubble borders/artwork inside the ruby crop. Copy
            # only the selected connected components onto white, preserving
            # their grayscale strokes. This makes tiny readings like ディー
            # far more stable for MangaOCR.
            focused = Image.new("RGB", (max(1, right2-left2), max(1, bottom2-top2)), "white")
            for cx, cy, cw, ch, _area in row:
                sx1 = max(left2, cx)
                sy1 = max(top2, cy)
                sx2 = min(right2, cx + cw)
                sy2 = min(bottom2, cy + ch)
                if sx2 <= sx1 or sy2 <= sy1:
                    continue
                patch = crop.crop((sx1, sy1, sx2, sy2))
                try:
                    focused.paste(patch, (sx1-left2, sy1-top2))
                finally:
                    patch.close()
        else:
            focused = crop.copy()

        border = max(8, int(round(focused.height * 0.45)))
        prepared = ImageOps.expand(focused, border=border, fill="white")
        target_height = max(160, prepared.height * 6)
        scale = target_height / max(1, prepared.height)
        resized = prepared.resize(
            (max(1, int(round(prepared.width * scale))), target_height),
            Image.Resampling.LANCZOS,
        )
        prepared.close()
        prepared = resized
        return _normalize_line_surface(model(prepared))  # type: ignore[operator]
    except Exception:
        return ""
    finally:
        if prepared is not None:
            prepared.close()
        if focused is not None:
            focused.close()
        crop.close()


def _latin_letter_from_ruby_reading(value: object) -> str | None:
    ruby = _compact_surface(value)
    if not ruby:
        return None
    if ruby.endswith(("一", "―", "—", "−")):
        ruby = ruby[:-1] + "ー"
    exact = _LATIN_RUBY_READINGS.get(ruby)
    if exact:
        return exact
    stem_matches = {
        letter
        for reading, letter in _LATIN_RUBY_READINGS.items()
        if len(reading) >= 2 and reading.endswith("ー") and reading[:-1] == ruby
    }
    if len(stem_matches) == 1:
        return next(iter(stem_matches))
    # Ruby OCR is tiny and often drops the long mark or expands small kana.
    # The surrounding ・X・ pattern is already extremely restrictive, so allow
    # one small OCR edit when the candidate still unambiguously names one Latin
    # letter.
    candidates: list[tuple[float, str]] = []
    for reading, letter in _LATIN_RUBY_READINGS.items():
        if reading in ruby or ruby in reading:
            ratio = min(len(reading), len(ruby)) / max(len(reading), len(ruby))
        else:
            ratio = difflib.SequenceMatcher(a=reading, b=ruby, autojunk=False).ratio()
        if ratio >= 0.72:
            candidates.append((ratio, letter))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    best_ratio, best_letter = candidates[0]
    second = candidates[1][0] if len(candidates) > 1 and candidates[1][1] != best_letter else 0.0
    if best_ratio - second < 0.08:
        return None
    return best_letter


def _repair_interpunct_latin_from_ruby(
    model: object,
    image: Image.Image,
    piece: dict[str, object],
) -> dict[str, object]:
    """Recover a Latin initial between interpuncts from its printed ruby name.

    MangaOCR is Japanese-first and can read a boxed Latin initial as a similar
    kana/kanji glyph (D -> ロ). Manga often prints the letter name as ruby above
    it. Only replace a single non-Latin glyph in the strict pattern ・X・ when
    OCR of that ruby is an unambiguous Japanese Latin-letter name.
    """
    if str(piece.get("orientation") or "") != "horizontal":
        return piece
    segments = [dict(item) for item in piece.get("segments") or [] if isinstance(item, dict)]
    if len(segments) < 3:
        return piece
    stream_chars: list[str] = []
    for segment in segments:
        chars = _study_surface_characters(segment.get("text"))
        if len(chars) != 1:
            return piece
        stream_chars.append(chars[0])
    compact_text, _ = _compact_text_index_map(piece.get("text"))
    if "".join(stream_chars) != compact_text:
        return piece

    repairs: list[dict[str, object]] = []
    attempts: list[dict[str, object]] = []
    result_text = str(piece.get("text") or "")
    for index in range(1, len(segments) - 1):
        if stream_chars[index - 1] != "・" or stream_chars[index + 1] != "・":
            continue
        current = stream_chars[index]
        if len(current) == 1 and current.isascii() and current.isalpha():
            continue
        ruby = _compact_surface(_recognize_ruby_above_segment(model, image, segments[index]))
        letter = _latin_letter_from_ruby_reading(ruby)
        attempts.append({"segment_index": index, "from": current, "ruby": ruby, "letter": letter or ""})
        if not letter:
            continue
        segments[index]["text"] = letter
        segments[index]["recognition_correction"] = "ruby-latin-letter-v2"
        stream_chars[index] = letter
        result_text = _replace_compact_character_at(result_text, index, letter)
        repairs.append({"segment_index": index, "from": current, "to": letter, "ruby": ruby})

    if not repairs:
        if not attempts:
            return piece
        result = dict(piece)
        result["ruby_latin_attempts"] = attempts
        return result
    result = dict(piece)
    result["text"] = result_text
    result["raw_text"] = result_text
    result["segments"] = segments
    result["ruby_latin_repairs"] = repairs
    result["ruby_latin_attempts"] = attempts
    result["geometry_source"] = f"{str(piece.get('geometry_source') or '')}+ruby-latin-letter-v2".strip("+")
    return result

def _align_horizontal_segments_to_ocr(
    segments: list[dict[str, object]], target_text: str
) -> tuple[list[dict[str, object]], float, float]:
    """Choose one monotonic Vision box per MangaOCR character.

    Multiple Accurate Vision observations often overlap on the same TOC row.
    A plain x-sort interleaves them (第第88話話...). This dynamic program skips
    duplicate candidates and permits rare label substitutions (美 -> 男, 手 ->
    斧) while keeping geometry monotonic and strongly penalising overlapping
    consecutive boxes.
    """
    target = _study_surface_characters(target_text)
    candidates: list[dict[str, object]] = []
    for item in segments:
        if not isinstance(item, dict):
            continue
        chars = _study_surface_characters(item.get("text"))
        if len(chars) != 1:
            continue
        candidate = dict(item)
        candidate["_surface"] = chars[0]
        candidates.append(candidate)
    candidates.sort(
        key=lambda item: (
            float(item.get("x") or 0.0) + float(item.get("width") or 0.0) / 2.0,
            -float(item.get("height") or 0.0),
        )
    )
    if not target or len(candidates) < len(target):
        return [dict(item) for item in segments], 0.0, 0.0

    heights = sorted(max(1e-6, float(item.get("height") or 0.0)) for item in candidates)
    widths = sorted(max(1e-6, float(item.get("width") or 0.0)) for item in candidates)
    preferred_height = heights[max(0, int((len(heights) - 1) * 0.35))]
    preferred_width = widths[len(widths) // 2]
    target_count = len(target)
    candidate_count = len(candidates)
    infinity = 10**12
    costs = [[infinity] * candidate_count for _ in range(target_count)]
    previous = [[-1] * candidate_count for _ in range(target_count)]

    def map_cost(character: str, candidate: dict[str, object]) -> float:
        label = str(candidate.get("_surface") or "")
        mismatch = 0.0 if label == character else 1.25
        height = max(1e-6, float(candidate.get("height") or 0.0))
        return mismatch + 0.25 * abs(math.log(height / preferred_height))

    for candidate_index, candidate in enumerate(candidates):
        costs[0][candidate_index] = map_cost(target[0], candidate) + candidate_index * 0.002

    for target_index in range(1, target_count):
        for candidate_index in range(target_index, candidate_count):
            candidate = candidates[candidate_index]
            candidate_x = float(candidate.get("x") or 0.0)
            candidate_width = max(1e-6, float(candidate.get("width") or 0.0))
            candidate_center = candidate_x + candidate_width / 2.0
            best_cost = infinity
            best_previous = -1
            for previous_index in range(target_index - 1, candidate_index):
                if costs[target_index - 1][previous_index] >= infinity:
                    continue
                prior = candidates[previous_index]
                prior_x = float(prior.get("x") or 0.0)
                prior_width = max(1e-6, float(prior.get("width") or 0.0))
                prior_center = prior_x + prior_width / 2.0
                if candidate_center <= prior_center + 1e-6:
                    continue
                overlap = max(0.0, prior_x + prior_width - candidate_x) / max(
                    1e-6, min(prior_width, candidate_width)
                )
                center_gap = candidate_center - prior_center
                near = max(
                    0.0,
                    (preferred_width * 0.55 - center_gap) / max(1e-6, preferred_width * 0.55),
                )
                prior_height = max(1e-6, float(prior.get("height") or 0.0))
                candidate_height = max(1e-6, float(candidate.get("height") or 0.0))
                height_jump = abs(math.log(candidate_height / prior_height))
                transition = 1.7 * overlap + 1.2 * near + 0.08 * height_jump
                value = costs[target_index - 1][previous_index] + transition
                if value < best_cost:
                    best_cost = value
                    best_previous = previous_index
            if best_previous >= 0:
                costs[target_index][candidate_index] = best_cost + map_cost(target[target_index], candidate)
                previous[target_index][candidate_index] = best_previous

    end = min(
        range(target_count - 1, candidate_count),
        key=lambda index: costs[target_count - 1][index],
    )
    if costs[target_count - 1][end] >= infinity:
        return [dict(item) for item in segments], 0.0, 0.0
    selected = [end]
    cursor = end
    for target_index in range(target_count - 1, 0, -1):
        cursor = previous[target_index][cursor]
        if cursor < 0:
            return [dict(item) for item in segments], 0.0, 0.0
        selected.append(cursor)
    selected.reverse()

    output: list[dict[str, object]] = []
    exact_matches = 0
    for character, candidate_index in zip(target, selected):
        candidate = dict(candidates[candidate_index])
        original = str(candidate.pop("_surface", ""))
        if original == character:
            exact_matches += 1
        else:
            candidate["source"] = "vision-accurate-aligned-v4"
        candidate["text"] = character
        output.append(candidate)
    exact_ratio = exact_matches / max(1, target_count)
    normalized_cost = costs[target_count - 1][selected[-1]] / max(1, target_count)
    return output, exact_ratio, normalized_cost



def _chapter_main_ink_prefix_runs(
    image: Image.Image, piece: dict[str, object]
) -> list[dict[str, float]]:
    """Reconstruct visible 第N話 glyph boxes from connected base-ink components.

    Accurate Vision range boxes on chapter headers can overlap several glyphs or
    collapse the final 話 to a narrow strip.  Rebuild the prefix from the page
    pixels instead of stretching those already-wrong boxes. Small ruby/artwork
    touching the top of the crop is ignored; disconnected radicals of one base
    glyph are merged by x-overlap / tiny gaps.
    """
    text = _normalize_line_surface(piece.get("text"))
    match = re.match(r"^(第[0-9０-９一二三四五六七八九十百千]+話)", text)
    if not match:
        return []
    prefix_chars = _study_surface_characters(match.group(1))
    if len(prefix_chars) < 3 or len(prefix_chars) > 8:
        return []

    page_width, page_height = image.size
    x = max(0.0, min(1.0, float(piece.get("x") or 0.0)))
    y = max(0.0, min(1.0, float(piece.get("y") or 0.0)))
    width = max(0.0, min(1.0 - x, float(piece.get("width") or 0.0)))
    height = max(0.0, min(1.0 - y, float(piece.get("height") or 0.0)))
    left = max(0, min(page_width - 1, int(round(x * page_width))))
    right = max(left + 1, min(page_width, int(round((x + width) * page_width))))
    top = max(0, min(page_height - 1, int(round((1.0 - y - height) * page_height))))
    bottom = max(top + 1, min(page_height, int(round((1.0 - y) * page_height))))
    crop = image.crop((left, top, right, bottom)).convert("L")
    try:
        crop_width, crop_height = crop.size
        pixels = [int(value) for value in crop.getdata()]
        if crop_width < 12 or crop_height < 12 or not pixels:
            return []
        ordered = sorted(pixels)
        p20 = ordered[max(0, int(len(ordered) * 0.20) - 1)]
        p80 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.80))]
        threshold = min(175, max(70, int((p20 + p80) * 0.48)))
        mask = crop.point(lambda value: 255 if int(value) <= threshold else 0)
        components = _binary_components(mask)
        mask.close()
    finally:
        crop.close()

    minimum_height = max(4, int(round(crop_height * 0.18)))
    minimum_area = max(8, int(round(crop_height * 0.70)))
    candidates: list[list[float]] = []
    for component_x, component_y, component_width, component_height, area in components:
        if component_height < minimum_height or area < minimum_area:
            continue
        # Ruby / panel art touching the crop's upper edge can otherwise bridge
        # the chapter number to 話. Real base glyphs occupy most of the row.
        component_density = area / max(1.0, float(component_width * component_height))
        if (
            component_y <= 1
            and component_height < crop_height * 0.70
            and component_density < 0.50
        ):
            continue
        candidates.append([
            float(component_x),
            float(component_x + component_width),
            float(component_y),
            float(component_y + component_height),
            float(area),
        ])
    if len(candidates) < len(prefix_chars):
        return []

    candidates.sort(key=lambda value: value[0])
    join_gap = max(1, int(round(crop_height * 0.04)))
    groups: list[list[float]] = []
    for candidate in candidates:
        if not groups or candidate[0] > groups[-1][1] + join_gap:
            groups.append(list(candidate))
            continue
        group = groups[-1]
        group[1] = max(group[1], candidate[1])
        group[2] = min(group[2], candidate[2])
        group[3] = max(group[3], candidate[3])
        group[4] += candidate[4]

    groups = [
        group
        for group in groups
        if group[3] - group[2] >= crop_height * 0.45
        and group[1] - group[0] >= max(3.0, crop_height * 0.10)
    ]
    if len(groups) < len(prefix_chars):
        return []
    groups = groups[: len(prefix_chars)]

    # Sanity: chapter prefix must be a compact left-to-right run rather than
    # unrelated artwork scattered through the whole title row.
    if groups[-1][1] > crop_width * 0.995:
        return []
    centers = [(group[0] + group[1]) / 2.0 for group in groups]
    if any(right_center <= left_center for left_center, right_center in zip(centers, centers[1:])):
        return []

    output: list[dict[str, float]] = []
    for group in groups:
        gx1, gx2, gy1, gy2, _area = group
        page_left = left + max(0, int(math.floor(gx1)) - 1)
        page_right = left + min(crop_width, int(math.ceil(gx2)) + 1)
        page_top = top + max(0, int(math.floor(gy1)) - 1)
        page_bottom = top + min(crop_height, int(math.ceil(gy2)) + 1)
        output.append(
            {
                "x": page_left / page_width,
                "y": 1.0 - page_bottom / page_height,
                "width": (page_right - page_left) / page_width,
                "height": (page_bottom - page_top) / page_height,
            }
        )
    return output


def _repair_chapter_horizontal_geometry(
    image: Image.Image, piece: dict[str, object]
) -> dict[str, object]:
    """Anchor TOC range geometry on visible 第N話 components and shift the row."""
    if str(piece.get("orientation") or "") != "horizontal":
        return piece
    text = _normalize_line_surface(piece.get("text"))
    prefix_match = re.match(r"^(第[0-9０-９一二三四五六七八九十百千]+話)", text)
    if not prefix_match:
        return piece
    prefix_chars = _study_surface_characters(prefix_match.group(1))
    segments = [dict(item) for item in piece.get("segments") or [] if isinstance(item, dict)]
    if len(segments) < len(prefix_chars):
        return piece
    ordered = sorted(segments, key=lambda item: float(item.get("x") or 0.0))
    prefix_segments = ordered[: len(prefix_chars)]
    if [(_study_surface_characters(item.get("text")) or [""])[0] for item in prefix_segments] != prefix_chars:
        return piece
    anchors = _chapter_main_ink_prefix_runs(image, piece)
    if len(anchors) != len(prefix_chars):
        return piece

    old_last_x = float(prefix_segments[-1].get("x") or 0.0)
    shift = float(anchors[-1]["x"]) - old_last_x
    # Positive shifts are suspicious; the failure mode this repair targets is
    # Vision labels displaced to the right by ruby/overlap.
    if not (-0.12 <= shift <= 0.004):
        return piece
    old_last_center = old_last_x + float(prefix_segments[-1].get("width") or 0.0) / 2.0
    repaired: list[dict[str, object]] = []
    for item in ordered:
        # `ordered` contains copied dicts, so identity is stable within this list.
        prefix_index = next((i for i, candidate in enumerate(prefix_segments) if candidate is item), None)
        if prefix_index is not None:
            anchored = dict(item)
            anchored.update(anchors[prefix_index])
            anchored["source"] = "chapter-prefix-components-v2"
            anchored["geometry_correction"] = "chapter-prefix-components-v2"
            repaired.append(anchored)
            continue
        current = dict(item)
        center = float(current.get("x") or 0.0) + float(current.get("width") or 0.0) / 2.0
        if center > old_last_center and shift < -0.002:
            current["x"] = round(max(0.0, float(current.get("x") or 0.0) + shift), 6)
            current["source"] = "vision-accurate-aligned-v4"
            current = _refine_horizontal_segment_ink(image, current)
            current["geometry_correction"] = "chapter-prefix-shift-v2"
        repaired.append(current)
    result = dict(piece)
    result["segments"] = repaired
    result["geometry_source"] = "chapter-prefix-components-v2+vision-accurate-ink-v4"
    result["chapter_geometry_shift"] = round(shift, 6)
    result["word_geometry"] = "mapped_segments"
    return result


def _is_cjk_kanji(character: str) -> bool:
    return bool(character) and ("\u3400" <= character <= "\u9fff" or character in {"々", "〆", "ヶ"})


def _repair_chapter_text_from_line_ocr(piece: dict[str, object]) -> dict[str, object]:
    """Use one conservative MangaOCR kanji correction after a long exact TOC prefix."""
    current = _normalize_line_surface(piece.get("text"))
    alternate = _normalize_line_surface(piece.get("line_ocr_text"))
    if not current or not alternate:
        return piece
    if not re.match(r"^第[0-9０-９一二三四五六七八九十百千]+話", current):
        return piece
    if not re.match(r"^第[0-9０-９一二三四五六七八九十百千]+話", alternate):
        return piece
    common = 0
    for left, right in zip(current, alternate):
        if left != right:
            break
        common += 1
    if common < 5 or common >= min(len(current), len(alternate)):
        return piece
    left_char, right_char = current[common], alternate[common]
    if left_char == right_char or not (_is_cjk_kanji(left_char) and _is_cjk_kanji(right_char)):
        return piece
    corrected = current[:common] + right_char + current[common + 1 :]
    result = dict(piece)
    result["text"] = corrected
    result["raw_text"] = corrected
    result["chapter_text_repair"] = {
        "index": common,
        "from": left_char,
        "to": right_char,
        "source": "mangaocr-long-prefix-v1",
    }
    segments = [dict(item) for item in result.get("segments") or [] if isinstance(item, dict)]
    stream = "".join(str(item.get("text") or "") for item in segments)
    if stream == current and common < len(segments):
        segments[common]["text"] = right_char
        segments[common]["recognition_correction"] = "mangaocr-long-prefix-v1"
        result["segments"] = segments
    return result



def _chapter_prefix_surface(value: object) -> str:
    surface = _normalize_line_surface(value)
    match = re.match(r"^(第[0-9０-９一二三四五六七八九十百千]+話)", surface)
    return match.group(1) if match else ""


def _repair_chapter_latin_from_detector_consensus(
    piece: dict[str, object],
) -> dict[str, object]:
    """Restore one damaged Latin title glyph from detector evidence."""
    if str(piece.get("orientation") or "") != "horizontal":
        return piece
    current = _normalize_line_surface(piece.get("text"))
    prefix = _chapter_prefix_surface(current)
    if not prefix:
        return piece
    tail = current[len(prefix):]
    match = re.match(r"([A-Za-z]{4,})", tail)
    if not match:
        return piece
    current_run = match.group(1)

    candidates: list[str] = []
    for hypothesis in piece.get("hypotheses") or []:
        if not isinstance(hypothesis, dict):
            continue
        if str(hypothesis.get("source") or "") == "manga-ocr":
            continue
        surface = _normalize_line_surface(hypothesis.get("text"))
        start = 0
        while True:
            pos = surface.find(prefix, start)
            if pos < 0:
                break
            after = surface[pos + len(prefix):]
            candidate_match = re.match(r"[-—–ー―]*([A-Za-z]{4,})", after)
            if candidate_match:
                candidate = candidate_match.group(1)
                if len(candidate) == len(current_run):
                    candidates.append(candidate)
            start = pos + len(prefix)

    unique = sorted(set(candidates))
    if len(unique) != 1:
        return piece
    candidate = unique[0]
    diffs = [i for i, (left, right) in enumerate(zip(current_run, candidate)) if left != right]
    if len(diffs) != 1:
        return piece

    compact, _ = _compact_text_index_map(piece.get("text"))
    run_start = compact.find(current_run)
    if run_start < 0:
        return piece
    compact_index = run_start + diffs[0]
    replacement = candidate[diffs[0]]

    result = dict(piece)
    result_text = _replace_compact_character_at(piece.get("text"), compact_index, replacement)
    result["text"] = result_text
    result["raw_text"] = result_text
    result["chapter_latin_consensus"] = {
        "index": compact_index,
        "from": current_run[diffs[0]],
        "to": replacement,
        "source": "detector-chapter-latin-v1",
    }

    segments = [dict(item) for item in piece.get("segments") or [] if isinstance(item, dict)]
    stream = "".join(str(item.get("text") or "") for item in segments)
    if _normalize_line_surface(stream) == current and compact_index < len(segments):
        segments[compact_index]["text"] = replacement
        segments[compact_index]["recognition_correction"] = "detector-chapter-latin-v1"
        result["segments"] = segments
    return result


def _chapter_consensus_crop(
    image: Image.Image,
    piece: dict[str, object],
    *,
    xpad: float,
    ypad: float,
) -> Image.Image:
    expanded = dict(piece)
    x0 = _number(piece.get("x"))
    y0 = _number(piece.get("y"))
    x = max(0.0, x0 - xpad)
    y = max(0.0, y0 - ypad)
    right = min(1.0, x0 + _number(piece.get("width")) + xpad)
    top = min(1.0, y0 + _number(piece.get("height")) + ypad)
    expanded["x"] = x
    expanded["y"] = y
    expanded["width"] = max(0.0, right - x)
    expanded["height"] = max(0.0, top - y)
    return _crop_region(image, expanded)


def _strip_removed_chapter_tail(piece: dict[str, object], value: object) -> str:
    surface = _normalize_line_surface(value)
    if piece.get("page_number_tail_removed"):
        surface = re.sub(r"[-—–ー―]*\d{1,4}$", "", surface)
        # A long TOC page-number leader is occasionally read by MangaOCR as
        # the kanji 一.  Strip it only when it follows an independently read
        # ornamental closing quote, which makes the leader interpretation
        # unambiguous without deleting a legitimate title-final 一.
        surface = re.sub(r"〟一$", "〟", surface)
        surface = re.sub(r"[-—–ー―]+$", "", surface)
    return surface


def _repair_chapter_from_repeated_mangaocr(
    model: object,
    image: Image.Image,
    piece: dict[str, object],
) -> dict[str, object]:
    """Recheck an unstable TOC row with slightly different MangaOCR crops."""
    if str(piece.get("orientation") or "") != "horizontal":
        return piece
    current = _normalize_line_surface(piece.get("text"))
    prefix = _chapter_prefix_surface(current)
    if not prefix:
        return piece

    alignment = _number(piece.get("line_ocr_alignment"), 1.0)
    line_hint = str(piece.get("line_ocr_text") or "")
    if not line_hint and alignment >= 0.95:
        return piece

    variants: list[str] = []
    for xpad, ypad in ((0.0, 0.0), (0.008, 0.005), (0.015, 0.010)):
        crop = _chapter_consensus_crop(image, piece, xpad=xpad, ypad=ypad)
        try:
            surface = _strip_removed_chapter_tail(piece, model(crop))  # type: ignore[operator]
        except Exception:
            surface = ""
        finally:
            crop.close()
        if surface.startswith(prefix):
            variants.append(surface)
    if len(variants) < 2:
        return piece

    # Keep the independent crop reads even when they do not agree strongly
    # enough for the narrow v26 repair below.  A later page-level TOC pass can
    # combine them with line OCR and deterministic page ink without re-running
    # MangaOCR or depending on how many neighbouring rows happened to be fixed.
    result = dict(piece)
    result["chapter_repeat_candidates"] = list(variants)

    full_counts: dict[str, int] = {}
    for surface in variants:
        full_counts[surface] = full_counts.get(surface, 0) + 1
    repeated_surface, repeated_count = max(full_counts.items(), key=lambda item: item[1])

    current_study = _study_surface_characters(current)
    study_counts: dict[str, int] = {}
    study_surfaces: dict[str, list[str]] = {}
    for surface in variants:
        chars = _study_surface_characters(surface)
        key = "".join(chars)
        if not key:
            continue
        study_counts[key] = study_counts.get(key, 0) + 1
        study_surfaces[key] = chars
    if not study_counts:
        return result
    consensus_key, consensus_count = max(study_counts.items(), key=lambda item: item[1])
    consensus_study = study_surfaces[consensus_key]

    segments = [dict(item) for item in piece.get("segments") or [] if isinstance(item, dict)]
    repairs: list[dict[str, object]] = []

    if consensus_count >= 2 and len(consensus_study) == len(current_study):
        diffs = [
            i for i, (left, right) in enumerate(zip(current_study, consensus_study))
            if left != right
        ]
        if len(diffs) == 1:
            study_index = diffs[0]
            compact, _ = _compact_text_index_map(result.get("text"))
            study_positions = [
                pos
                for pos, character in enumerate(compact)
                if bool(_study_surface_characters(character))
            ]
            if study_index < len(study_positions):
                compact_index = study_positions[study_index]
                old_char = current_study[study_index]
                new_char = consensus_study[study_index]
                result_text = _replace_compact_character_at(
                    result.get("text"), compact_index, new_char
                )
                result["text"] = result_text
                result["raw_text"] = result_text
                stream = "".join(str(item.get("text") or "") for item in segments)
                if _normalize_line_surface(stream) == current and compact_index < len(segments):
                    segments[compact_index]["text"] = new_char
                    segments[compact_index]["recognition_correction"] = "mangaocr-chapter-repeat-v1"
                repairs.append({
                    "kind": "character",
                    "index": compact_index,
                    "from": old_char,
                    "to": new_char,
                    "votes": consensus_count,
                })
                current = _normalize_line_surface(result_text)
                current_study = _study_surface_characters(current)

    if repeated_count >= 2:
        desired = repeated_surface
        if (
            _study_surface_characters(desired) == current_study
            and segments
            and len(desired) == len(segments)
            and len(current) == len(segments)
            and desired != current
        ):
            changed = [
                i for i, (left, right) in enumerate(zip(current, desired))
                if left != right
            ]
            if changed and all(
                not _study_surface_characters(current[i])
                and not _study_surface_characters(desired[i])
                for i in changed
            ):
                for i in changed:
                    segments[i]["text"] = desired[i]
                    segments[i]["recognition_correction"] = "mangaocr-chapter-punctuation-v1"
                result["text"] = desired
                result["raw_text"] = desired
                repairs.append({
                    "kind": "punctuation",
                    "indices": changed,
                    "from": current,
                    "to": desired,
                    "votes": repeated_count,
                })

    if not repairs:
        return result
    result["segments"] = segments
    result["chapter_repeat_consensus"] = repairs
    return result


_CHAPTER_QUOTE_MARKERS = {"〝", "〟"}
_CHAPTER_QUOTE_BOUNDARY_PUNCTUATION = {
    "・", "、", "。", ",", ".", "，", "．",
    '"', "'", "“", "”", "‘", "’", "〝", "〟",
}


def _chapter_quote_core_characters(value: object) -> list[str]:
    normalized = _normalize_line_surface(value)
    return [
        character
        for character in normalized
        if character not in _CHAPTER_QUOTE_BOUNDARY_PUNCTUATION
        and (
            character.isalnum()
            or "\u3040" <= character <= "\u30fa"
            or "\u30fc" <= character <= "\u30fa"
            or "\u3400" <= character <= "\u9fff"
            or character in {"々", "〆", "ヶ", "ー"}
        )
    ]


def _chapter_quote_boundary_punctuation(value: object) -> dict[int, list[str]]:
    surface = _normalize_line_surface(value)
    boundary = 0
    out: dict[int, list[str]] = {}
    for character in surface:
        if _chapter_quote_core_characters(character):
            boundary += 1
            continue
        if character in _CHAPTER_QUOTE_BOUNDARY_PUNCTUATION:
            out.setdefault(boundary, []).append(character)
    return out


def _chapter_quote_core_segments(
    piece: dict[str, object],
) -> list[dict[str, object]]:
    prepared: list[dict[str, object]] = []
    for segment in piece.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        chars = _chapter_quote_core_characters(segment.get("text"))
        if len(chars) != 1:
            continue
        item = dict(segment)
        item["_chapter_core_char"] = chars[0]
        prepared.append(item)
    return sorted(prepared, key=lambda item: float(item.get("x") or 0.0))


def _chapter_quote_candidate_boundaries(piece: dict[str, object]) -> list[int]:
    core_segments = _chapter_quote_core_segments(piece)
    current_core = _chapter_quote_core_characters(piece.get("text"))
    if len(core_segments) < 4 or len(core_segments) != len(current_core):
        return []

    candidates = set(_chapter_quote_boundary_punctuation(piece.get("text")))
    widths = [
        max(1e-6, float(item.get("width") or 0.0))
        for item in core_segments
    ]
    gaps = [
        float(right.get("x") or 0.0)
        - (
            float(left.get("x") or 0.0)
            + float(left.get("width") or 0.0)
        )
        for left, right in zip(core_segments, core_segments[1:])
    ]
    positive_gaps = sorted(gap for gap in gaps if gap > 0.0)
    median_gap = (
        statistics.median(positive_gaps) if positive_gaps else 0.0
    )
    median_width = statistics.median(widths)
    threshold = max(0.012, median_gap * 1.75, median_width * 0.28)
    for boundary, gap in enumerate(gaps, start=1):
        if gap >= threshold:
            candidates.add(boundary)
    return sorted(
        boundary
        for boundary in candidates
        if 0 < boundary <= len(current_core)
    )


def _chapter_quote_context_crop(
    image: Image.Image,
    piece: dict[str, object],
    boundary: int,
) -> tuple[Image.Image | None, str]:
    core_segments = _chapter_quote_core_segments(piece)
    if not core_segments or not (0 < boundary <= len(core_segments)):
        return None, ""
    if boundary == len(core_segments):
        start = max(0, boundary - 4)
        end = boundary
        left_pad, right_pad = 0.012, 0.035
    else:
        start = max(0, boundary - 3)
        end = min(len(core_segments), boundary + 3)
        left_pad = right_pad = 0.012
    selected = core_segments[start:end]
    if len(selected) < 3:
        return None, ""

    x1 = min(float(item.get("x") or 0.0) for item in selected)
    y1 = min(float(item.get("y") or 0.0) for item in selected)
    x2 = max(
        float(item.get("x") or 0.0) + float(item.get("width") or 0.0)
        for item in selected
    )
    y2 = max(
        float(item.get("y") or 0.0) + float(item.get("height") or 0.0)
        for item in selected
    )
    region = {
        "x": max(0.0, x1 - left_pad),
        "y": max(0.0, y1 - 0.010),
        "width": 0.0,
        "height": 0.0,
    }
    right = min(1.0, x2 + right_pad)
    top = min(1.0, y2 + 0.010)
    region["width"] = max(0.0, right - float(region["x"]))
    region["height"] = max(0.0, top - float(region["y"]))
    expected = "".join(
        str(item.get("_chapter_core_char") or "") for item in selected
    )
    return _crop_region(image, region), expected


def _chapter_local_quote_signal(
    model: object,
    image: Image.Image,
    piece: dict[str, object],
    boundary: int,
) -> tuple[bool, bool, str]:
    crop, expected = _chapter_quote_context_crop(
        image, piece, boundary
    )
    if crop is None:
        return False, False, ""
    try:
        try:
            value = str(model(crop) or "").strip()  # type: ignore[operator]
        except Exception:
            value = ""
    finally:
        crop.close()
    if not value:
        return False, False, ""

    observed_core = "".join(_chapter_quote_core_characters(value))
    expected_core = "".join(_chapter_quote_core_characters(expected))
    if len(observed_core) < 2 or len(expected_core) < 3:
        return False, False, value
    similarity = difflib.SequenceMatcher(
        a=expected_core,
        b=observed_core,
        autojunk=False,
    ).ratio()
    if similarity < 0.42:
        return False, False, value
    return "〝" in value, "〟" in value, value


def _replace_chapter_boundary_mark(
    value: object,
    boundary: int,
    marker: str,
) -> str:
    surface = _normalize_line_surface(value)
    chars = list(surface)
    core_seen = 0
    replace_positions: list[int] = []
    insert_at: int | None = None

    for index, character in enumerate(chars):
        if _chapter_quote_core_characters(character):
            if core_seen == boundary and insert_at is None:
                insert_at = index
            core_seen += 1
            continue
        if (
            core_seen == boundary
            and character in _CHAPTER_QUOTE_BOUNDARY_PUNCTUATION
        ):
            replace_positions.append(index)

    if boundary == core_seen:
        insert_at = len(chars)
    if replace_positions:
        chars[replace_positions[0]] = marker
        for index in reversed(replace_positions[1:]):
            del chars[index]
    elif insert_at is not None:
        chars.insert(insert_at, marker)
    return "".join(chars)



def _chapter_repeat_evidence_surfaces(piece: dict[str, object]) -> list[str]:
    """Return normalized chapter-row OCR evidence already collected this pass."""
    prefix = _chapter_prefix_surface(piece.get("text"))
    if not prefix:
        return []
    values: list[object] = []
    line_hint = piece.get("line_ocr_text")
    if line_hint:
        values.append(line_hint)
    repeated = piece.get("chapter_repeat_candidates")
    if isinstance(repeated, list):
        values.extend(repeated)
    output: list[str] = []
    for value in values:
        surface = _strip_removed_chapter_tail(piece, value)
        if surface.startswith(prefix):
            output.append(surface)
    return output


def _chapter_consensus_core_from_repeat_evidence(
    piece: dict[str, object], current_core: str
) -> tuple[str, int]:
    """Promote a changed chapter core only when two OCR reads agree exactly."""
    if not current_core:
        return current_core, 0
    prefix = _chapter_prefix_surface(piece.get("text"))
    votes: dict[str, int] = {}
    for surface in _chapter_repeat_evidence_surfaces(piece):
        if _chapter_prefix_surface(surface) != prefix:
            continue
        core = "".join(_chapter_quote_core_characters(surface))
        if not core:
            continue
        if abs(len(core) - len(current_core)) > 3:
            continue
        similarity = difflib.SequenceMatcher(
            a=current_core, b=core, autojunk=False
        ).ratio()
        if similarity < 0.82:
            continue
        votes[core] = votes.get(core, 0) + 1
    if not votes:
        return current_core, 0
    ranked = sorted(votes.items(), key=lambda item: (-item[1], item[0]))
    candidate, count = ranked[0]
    if count < 2:
        return current_core, count
    if len(ranked) > 1 and ranked[1][1] == count:
        return current_core, count
    return candidate, count


def _chapter_small_kana_core_consensus(
    piece: dict[str, object], target_core: str
) -> tuple[str, list[dict[str, object]]]:
    """Recover large->small kana typography from positionally stable OCR votes."""
    if not target_core:
        return target_core, []
    evidence = [
        "".join(_chapter_quote_core_characters(surface))
        for surface in _chapter_repeat_evidence_surfaces(piece)
    ]
    chars = list(target_core)
    repairs: list[dict[str, object]] = []
    for index, large in enumerate(chars):
        small_options = [
            small for small, mapped_large in _SMALL_KANA_TO_LARGE.items()
            if mapped_large == large
        ]
        if not small_options:
            continue
        for small in small_options:
            votes = 0
            for candidate in evidence:
                if len(candidate) != len(chars) or candidate[index] != small:
                    continue
                left_ok = index == 0 or candidate[index - 1] == chars[index - 1]
                right_ok = index + 1 == len(chars) or candidate[index + 1] == chars[index + 1]
                if index in {0, len(chars) - 1}:
                    supported = left_ok and right_ok
                else:
                    supported = left_ok and right_ok
                if supported:
                    votes += 1
            if votes >= 2:
                repairs.append({
                    "index": index,
                    "from": large,
                    "to": small,
                    "votes": votes,
                })
                chars[index] = small
                break
    return "".join(chars), repairs


def _chapter_page_latin_tokens(
    pieces: list[dict[str, object]],
) -> list[str]:
    """Collect standalone long Latin title tokens on the same page."""
    tokens: set[str] = set()
    for piece in pieces:
        if str(piece.get("orientation") or "") != "horizontal":
            continue
        if _chapter_prefix_surface(piece.get("text")):
            continue
        surface = _normalize_line_surface(piece.get("text"))
        for match in re.finditer(r"[A-Za-z]{7,}", surface):
            tokens.add(match.group(0))
    return sorted(tokens)


def _repair_chapter_latin_from_page_duplicate(
    piece: dict[str, object],
    page_tokens: list[str],
) -> dict[str, object]:
    """Repair a damaged chapter Latin title from an independently read page title."""
    current = _normalize_line_surface(piece.get("text"))
    prefix = _chapter_prefix_surface(current)
    if not prefix:
        return piece
    tail = current[len(prefix):]
    match = re.match(r"([A-Za-z]{7,})(.*)$", tail)
    if not match or len(_study_surface_characters(match.group(2))) < 3:
        return piece
    current_run = match.group(1)
    scored: list[tuple[float, str]] = []
    for candidate in page_tokens:
        if candidate == current_run or abs(len(candidate) - len(current_run)) > 2:
            continue
        similarity = difflib.SequenceMatcher(
            a=current_run, b=candidate, autojunk=False
        ).ratio()
        if similarity >= 0.82:
            scored.append((similarity, candidate))
    if not scored:
        return piece
    scored.sort(reverse=True)
    best_score, best = scored[0]
    if len(scored) > 1 and best_score - scored[1][0] < 0.08:
        return piece

    target = prefix + best + match.group(2)
    segments = [
        dict(item) for item in piece.get("segments") or []
        if isinstance(item, dict)
    ]
    mapped, exact_ratio, cost = _align_horizontal_segments_to_ocr(segments, target)
    if exact_ratio < 0.70 or cost > 0.65:
        return piece
    result = dict(piece)
    result["text"] = target
    result["raw_text"] = target
    result["segments"] = mapped
    result["chapter_page_latin_consensus"] = {
        "from": current_run,
        "to": best,
        "similarity": round(best_score, 4),
        "geometry_exact_ratio": round(exact_ratio, 4),
        "geometry_cost": round(cost, 4),
        "source": "same-page-latin-title-v1",
    }
    return result


def _chapter_main_ink_groups(
    image: Image.Image,
    piece: dict[str, object],
) -> tuple[list[dict[str, float]], float, float] | None:
    """Extract deterministic base-line glyph groups from one horizontal TOC row."""
    page_width, page_height = image.size
    x = max(0.0, min(1.0, _number(piece.get("x"))))
    y = max(0.0, min(1.0, _number(piece.get("y"))))
    width = max(0.0, min(1.0 - x, _number(piece.get("width"))))
    height = max(0.0, min(1.0 - y, _number(piece.get("height"))))
    left = max(0, min(page_width - 1, int(math.floor(x * page_width))))
    right = max(left + 1, min(page_width, int(math.ceil((x + width) * page_width))))
    top = max(0, min(page_height - 1, int(math.floor((1.0 - y - height) * page_height))))
    bottom = max(top + 1, min(page_height, int(math.ceil((1.0 - y) * page_height))))
    crop = image.crop((left, top, right, bottom)).convert("L")
    try:
        crop_width, crop_height = crop.size
        if crop_width < 20 or crop_height < 16:
            return None
        pixels = [int(value) for value in crop.getdata()]
        if not pixels:
            return None
        ordered = sorted(pixels)
        p20 = ordered[max(0, int(len(ordered) * 0.20) - 1)]
        p80 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.80))]
        threshold = min(175, max(70, int((p20 + p80) * 0.48)))
        mask = crop.point(lambda value: 255 if int(value) <= threshold else 0)
        try:
            components = _binary_components(mask)
        finally:
            mask.close()
    finally:
        crop.close()

    candidates: list[list[float]] = []
    minimum_area = max(5, int(crop_height * 0.08))
    for cx, cy, cw, ch, area in components:
        if area < minimum_area:
            continue
        if cy + ch < crop_height * 0.30:
            continue
        if ch < max(2.0, crop_height * 0.06) and cw < max(3.0, crop_height * 0.12):
            continue
        candidates.append([
            float(cx), float(cx + cw), float(cy), float(cy + ch), float(area)
        ])
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])

    grouped: list[list[float]] = []
    for candidate in candidates:
        if not grouped or candidate[0] > grouped[-1][1] + 1.0:
            grouped.append(list(candidate))
            continue
        group = grouped[-1]
        group[1] = max(group[1], candidate[1])
        group[2] = min(group[2], candidate[2])
        group[3] = max(group[3], candidate[3])
        group[4] += candidate[4]

    output: list[dict[str, float]] = []
    for gx1, gx2, gy1, gy2, area in grouped:
        output.append({
            "px_left": gx1,
            "px_right": gx2,
            "px_top": gy1,
            "px_bottom": gy2,
            "area": area,
            "x": (left + gx1) / page_width,
            "y": 1.0 - (top + gy2) / page_height,
            "width": (gx2 - gx1) / page_width,
            "height": (gy2 - gy1) / page_height,
        })
    return output, float(crop_width), float(crop_height)


def _merge_chapter_ink_groups_to_count(
    groups: list[dict[str, float]],
    target_count: int,
    median_width: float,
) -> list[dict[str, float]]:
    output = [dict(group) for group in groups]
    if target_count <= 0 or len(output) < target_count:
        return []
    while len(output) > target_count:
        best: tuple[float, int] | None = None
        for index, (left, right) in enumerate(zip(output, output[1:])):
            gap = max(0.0, right["px_left"] - left["px_right"])
            span = right["px_right"] - left["px_left"]
            left_width = left["px_right"] - left["px_left"]
            right_width = right["px_right"] - right["px_left"]
            score = (
                abs(span / max(1e-6, median_width) - 1.0)
                + 0.25 * gap / max(1e-6, median_width)
                + 0.15 * max(
                    0.0,
                    (left_width + right_width) / max(1e-6, median_width) - 1.4,
                )
            )
            if best is None or score < best[0]:
                best = (score, index)
        if best is None or best[0] > 0.55:
            return []
        index = best[1]
        left, right = output[index], output[index + 1]
        merged = dict(left)
        merged["px_left"] = min(left["px_left"], right["px_left"])
        merged["px_right"] = max(left["px_right"], right["px_right"])
        merged["px_top"] = min(left["px_top"], right["px_top"])
        merged["px_bottom"] = max(left["px_bottom"], right["px_bottom"])
        merged["area"] = left.get("area", 0.0) + right.get("area", 0.0)
        # Page-normalized geometry is simply the union of the two groups.
        merged["x"] = min(left["x"], right["x"])
        right_edge = max(left["x"] + left["width"], right["x"] + right["width"])
        merged["width"] = right_edge - merged["x"]
        merged["y"] = min(left["y"], right["y"])
        top_edge = max(left["y"] + left["height"], right["y"] + right["height"])
        merged["height"] = top_edge - merged["y"]
        output[index:index + 2] = [merged]
    return output


def _repair_chapter_quotes_from_page_ink(
    image: Image.Image,
    piece: dict[str, object],
    target_core: str,
    *,
    style_rows: int,
) -> dict[str, object]:
    """Rebuild ornamental quote pairs from the printed glyph geometry itself."""
    if style_rows < 3 or not target_core or re.search(r"[A-Za-z]{4,}", target_core):
        return piece
    extracted = _chapter_main_ink_groups(image, piece)
    if extracted is None:
        return piece
    groups, _crop_width, crop_height = extracted
    tall = [
        group for group in groups
        if group["px_bottom"] - group["px_top"] >= crop_height * 0.35
    ]
    if len(tall) < 4:
        return piece
    median_height = statistics.median(
        group["px_bottom"] - group["px_top"] for group in tall
    )
    median_width = statistics.median(
        group["px_right"] - group["px_left"] for group in tall
    )
    segments = [
        item for item in piece.get("segments") or [] if isinstance(item, dict)
    ]
    prefix_len = len(_study_surface_characters(_chapter_prefix_surface(piece.get("text"))))
    prefix_segments = [
        item for item in segments
        if len(_study_surface_characters(item.get("text"))) == 1
    ][:prefix_len]
    if len(prefix_segments) != prefix_len or not prefix_segments:
        return piece
    page_width = image.size[0]
    row_left_px = _number(piece.get("x")) * page_width
    prefix_end_px = max(
        (_number(item.get("x")) + _number(item.get("width"))) * page_width
        - row_left_px
        for item in prefix_segments
    )

    small: list[tuple[int, dict[str, float], float]] = []
    for index, group in enumerate(groups):
        group_height = group["px_bottom"] - group["px_top"]
        group_width = group["px_right"] - group["px_left"]
        center_x = (group["px_left"] + group["px_right"]) / 2.0
        center_y = (group["px_top"] + group["px_bottom"]) / 2.0
        if center_x <= prefix_end_px:
            continue
        if group_height > median_height * 0.52 or group_width > median_width * 0.72:
            continue
        small.append((index, group, center_y / max(1.0, crop_height)))

    openings = [item for item in small if item[2] < 0.64]
    closings = [item for item in small if item[2] >= 0.64]
    pairs = [
        (opening, closing)
        for opening in openings
        for closing in closings
        if opening[1]["px_left"] < closing[1]["px_left"]
    ]
    if len(pairs) != 1:
        return piece
    opening, closing = pairs[0]
    opening_index, opening_group, _ = opening
    closing_index, closing_group, _ = closing
    opening_center = (opening_group["px_left"] + opening_group["px_right"]) / 2.0
    closing_center = (closing_group["px_left"] + closing_group["px_right"]) / 2.0

    core_groups: list[dict[str, float]] = []
    for index, group in enumerate(groups):
        if index in {opening_index, closing_index}:
            continue
        group_height = group["px_bottom"] - group["px_top"]
        group_width = group["px_right"] - group["px_left"]
        center_x = (group["px_left"] + group["px_right"]) / 2.0
        # Horizontal page-number leaders can start immediately after the closing
        # mark. Keep the printed long-vowel mark inside the title, but drop only
        # thin rules that occur after the independently detected closing quote.
        if (
            center_x > closing_center
            and group_height <= median_height * 0.18
            and group_width >= median_width * 0.55
        ):
            continue
        core_groups.append(group)

    target_chars = list(target_core)
    core_groups = _merge_chapter_ink_groups_to_count(
        core_groups, len(target_chars), median_width
    )
    if len(core_groups) != len(target_chars):
        return piece
    centers = [
        (group["px_left"] + group["px_right"]) / 2.0 for group in core_groups
    ]
    opening_boundary = sum(center < opening_center for center in centers)
    closing_boundary = sum(center < closing_center for center in centers)
    if not (prefix_len <= opening_boundary < closing_boundary <= len(target_chars)):
        return piece

    repaired_text = (
        "".join(target_chars[:opening_boundary])
        + "〝"
        + "".join(target_chars[opening_boundary:closing_boundary])
        + "〟"
        + "".join(target_chars[closing_boundary:])
    )
    def _chapter_ink_segment(
        character: str,
        group: dict[str, float],
        *,
        quote: bool = False,
    ) -> dict[str, object]:
        return {
            "text": character,
            "orientation": "horizontal",
            "x": round(group["x"], 6),
            "y": round(group["y"], 6),
            "width": round(group["width"], 6),
            "height": round(group["height"], 6),
            "source": "chapter-quote-ink-v1" if quote else "chapter-main-ink-v1",
            "recognition_correction": (
                "chapter-quote-ink-v1" if quote else "chapter-main-ink-v1"
            ),
        }

    # The quote detector above already found the physical opening/closing ink
    # groups and used them to prove the boundaries.  Older versions rebuilt
    # only the core glyph stream, so the accepted text contained 〝〟 while the
    # clickable segment stream silently omitted both marks.  Keep those real
    # page-ink boxes in reading order instead of inventing proportional quote
    # hitboxes later in the frontend.
    rebuilt_segments: list[dict[str, object]] = []
    for index, (character, group) in enumerate(zip(target_chars, core_groups)):
        if index == opening_boundary:
            rebuilt_segments.append(
                _chapter_ink_segment("〝", opening_group, quote=True)
            )
        if index == closing_boundary:
            rebuilt_segments.append(
                _chapter_ink_segment("〟", closing_group, quote=True)
            )
        rebuilt_segments.append(_chapter_ink_segment(character, group))
    if closing_boundary == len(target_chars):
        rebuilt_segments.append(
            _chapter_ink_segment("〟", closing_group, quote=True)
        )

    result = dict(piece)
    result["text"] = repaired_text
    result["raw_text"] = repaired_text
    result["segments"] = rebuilt_segments
    result["word_geometry"] = "mapped_segments"
    result["geometry_source"] = (
        f"{str(piece.get('geometry_source') or '')}+chapter-main-ink-v1"
    ).strip("+")
    result["chapter_quote_ink_consensus"] = {
        "opening_boundary": opening_boundary,
        "closing_boundary": closing_boundary,
        "style_rows": style_rows,
        "source": "page-style-main-ink-v1",
        "quote_segment_geometry": "observed-page-ink-v1",
    }
    return result


def _repair_page_chapter_stability_consensus(
    image: Image.Image,
    pieces: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Stabilize ruby-heavy TOC rows using OCR votes plus deterministic page ink."""
    chapter_rows = [
        piece for piece in pieces
        if str(piece.get("orientation") or "") == "horizontal"
        and _chapter_prefix_surface(piece.get("text"))
    ]
    if len(chapter_rows) < 5:
        return pieces
    style_rows = 0
    for piece in chapter_rows:
        surfaces = [_normalize_line_surface(piece.get("text"))]
        surfaces.extend(_chapter_repeat_evidence_surfaces(piece))
        if any("〝" in surface and "〟" in surface for surface in surfaces):
            style_rows += 1
    page_tokens = _chapter_page_latin_tokens(pieces)

    output: list[dict[str, object]] = []
    for original in pieces:
        if (
            str(original.get("orientation") or "") != "horizontal"
            or not _chapter_prefix_surface(original.get("text"))
        ):
            output.append(original)
            continue

        piece = _repair_chapter_latin_from_page_duplicate(original, page_tokens)
        current = _normalize_line_surface(piece.get("text"))
        current_core = "".join(_chapter_quote_core_characters(current))
        target_core, core_votes = _chapter_consensus_core_from_repeat_evidence(
            piece, current_core
        )
        target_core, kana_repairs = _chapter_small_kana_core_consensus(
            piece, target_core
        )

        if "〝" in current and "〟" in current and target_core == current_core:
            segment_surface = _normalize_line_surface(
                "".join(
                    str(item.get("text") or "")
                    for item in piece.get("segments") or []
                    if isinstance(item, dict)
                )
            )
            if segment_surface == current:
                output.append(piece)
                continue

        repaired = _repair_chapter_quotes_from_page_ink(
            image, piece, target_core, style_rows=style_rows
        )
        if repaired is not piece:
            if core_votes >= 2:
                repaired["chapter_core_consensus"] = {
                    "from": current_core,
                    "to": target_core,
                    "votes": core_votes,
                    "source": "repeat-line-core-v1",
                }
            if kana_repairs:
                repaired["chapter_small_kana_repeat_consensus"] = kana_repairs
            output.append(repaired)
            continue
        output.append(piece)
    return output


def _repair_page_chapter_quote_style(
    model: object,
    image: Image.Image,
    pieces: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Recover ornamental TOC quote pairs from page style + local OCR evidence.

    Whole-line MangaOCR often turns the narrow 〝/〟 marks into comma/period
    punctuation.  We only infer a pair when the same page already contains at
    least three balanced chapter rows, a tight local crop independently sees
    an opening 〝, and the closing boundary is either independently seen or is
    the only later punctuation boundary in that row.
    """
    chapter_rows = [
        piece
        for piece in pieces
        if str(piece.get("orientation") or "") == "horizontal"
        and _chapter_prefix_surface(piece.get("text"))
    ]
    balanced = sum(
        "〝" in _normalize_line_surface(piece.get("text"))
        and "〟" in _normalize_line_surface(piece.get("text"))
        for piece in chapter_rows
    )
    if balanced < 3:
        return pieces

    output: list[dict[str, object]] = []
    for piece in pieces:
        current = _normalize_line_surface(piece.get("text"))
        if (
            str(piece.get("orientation") or "") != "horizontal"
            or not _chapter_prefix_surface(current)
            or ("〝" in current and "〟" in current)
        ):
            output.append(piece)
            continue

        candidates = _chapter_quote_candidate_boundaries(piece)
        if not candidates:
            output.append(piece)
            continue
        signals: dict[int, tuple[bool, bool, str]] = {}
        for boundary in candidates:
            signals[boundary] = _chapter_local_quote_signal(
                model, image, piece, boundary
            )

        openings = [
            boundary
            for boundary, (opening, _closing, _text) in signals.items()
            if opening
        ]
        if len(openings) != 1:
            output.append(piece)
            continue
        opening = openings[0]

        closings = [
            boundary
            for boundary, (_opening, closing, _text) in signals.items()
            if closing and boundary > opening
        ]
        closing: int | None = closings[0] if len(closings) == 1 else None
        if closing is None and not closings:
            later_punctuation = [
                boundary
                for boundary in _chapter_quote_boundary_punctuation(current)
                if boundary > opening
            ]
            if len(later_punctuation) == 1:
                closing = later_punctuation[0]
        if closing is None or closing <= opening:
            output.append(piece)
            continue

        repaired = _replace_chapter_boundary_mark(
            current, opening, "〝"
        )
        repaired = _replace_chapter_boundary_mark(
            repaired, closing, "〟"
        )
        if (
            _chapter_quote_core_characters(repaired)
            != _chapter_quote_core_characters(current)
        ):
            output.append(piece)
            continue

        result = dict(piece)
        result["text"] = repaired
        result["raw_text"] = repaired

        # If Vision already emitted a punctuation glyph at either recovered
        # quote boundary, keep that real glyph geometry and only relabel it.
        segments = [
            dict(item)
            for item in piece.get("segments") or []
            if isinstance(item, dict)
        ]
        if segments:
            core_seen = 0
            for segment in segments:
                chars = _chapter_quote_core_characters(segment.get("text"))
                if chars:
                    core_seen += len(chars)
                    continue
                segment_text = _normalize_line_surface(segment.get("text"))
                if segment_text not in _CHAPTER_QUOTE_BOUNDARY_PUNCTUATION:
                    continue
                if core_seen == opening:
                    segment["text"] = "〝"
                    segment["recognition_correction"] = "chapter-quote-boundary-v1"
                elif core_seen == closing:
                    segment["text"] = "〟"
                    segment["recognition_correction"] = "chapter-quote-boundary-v1"
            result["segments"] = segments

        result["chapter_quote_consensus"] = {
            "opening_boundary": opening,
            "closing_boundary": closing,
            "style_rows": balanced,
            "opening_ocr": signals[opening][2],
            "closing_ocr": signals.get(closing, (False, False, ""))[2],
            "source": "page-style-local-mangaocr-v1",
        }
        output.append(result)
    return output



def _repair_horizontal_trailing_punctuation_from_page_ink(
    image: Image.Image,
    piece: dict[str, object],
) -> dict[str, object]:
    """Attach observed geometry for a trusted trailing !/? punctuation glyph.

    MangaOCR can recover a terminal full-width exclamation/question mark that
    Accurate Vision omitted from its exact range boxes.  The text is already
    accepted upstream; this helper only fills the missing clickable geometry,
    and only when a compact, unclaimed ink component group is visible
    immediately to the right of the final observed glyph.
    """
    if str(piece.get("orientation") or "") != "horizontal":
        return piece

    target = _compact_surface(piece.get("text"))
    raw = _compact_surface(piece.get("raw_text"))
    segments = [
        dict(item)
        for item in piece.get("segments") or []
        if isinstance(item, dict)
    ]
    if not target or not segments:
        return piece
    segment_surface = _compact_surface(
        "".join(str(item.get("text") or "") for item in segments)
    )
    if not segment_surface or raw != segment_surface:
        return piece
    if not target.startswith(segment_surface):
        return piece
    suffix = target[len(segment_surface):]
    if len(suffix) != 1 or suffix not in {"！", "!", "？", "?"}:
        return piece
    if str(piece.get("selected_hypothesis_id") or "") != "manga-ocr":
        return piece
    if any(str(item.get("orientation") or "horizontal") != "horizontal" for item in segments):
        return piece

    page_width, page_height = image.size
    if page_width <= 0 or page_height <= 0:
        return piece
    widths_px = [float(item.get("width") or 0.0) * page_width for item in segments]
    heights_px = [float(item.get("height") or 0.0) * page_height for item in segments]
    if not widths_px or not heights_px or min(widths_px) <= 0 or min(heights_px) <= 0:
        return piece
    median_width = statistics.median(widths_px)
    median_height = statistics.median(heights_px)
    if median_width < 4.0 or median_height < 6.0:
        return piece

    last_right = max(
        (float(item.get("x") or 0.0) + float(item.get("width") or 0.0)) * page_width
        for item in segments
    )
    line_top = min(
        (1.0 - float(item.get("y") or 0.0) - float(item.get("height") or 0.0)) * page_height
        for item in segments
    )
    line_bottom = max(
        (1.0 - float(item.get("y") or 0.0)) * page_height
        for item in segments
    )
    left = max(0, int(math.floor(last_right)))
    right = min(
        page_width,
        int(math.ceil(last_right + max(4.0, median_width * 0.50))),
    )
    vertical_pad = max(1.0, median_height * 0.05)
    top = max(0, int(math.floor(line_top - vertical_pad)))
    bottom = min(page_height, int(math.ceil(line_bottom + vertical_pad)))
    if right - left < 3 or bottom - top < 6:
        return piece

    crop = image.crop((left, top, right, bottom)).convert("L")
    try:
        binary = crop.point(lambda value: 255 if value < 160 else 0)
        try:
            components = _binary_components(binary)
        finally:
            binary.close()
    finally:
        crop.close()

    # Residual antialiasing from the final observed glyph touches the left/top
    # edge.  Panel borders touch the bottom edge.  A real trailing punctuation
    # mark must be an interior component group in the narrow unclaimed strip.
    interior: list[tuple[int, int, int, int, int]] = []
    crop_width = right - left
    crop_height = bottom - top
    for x, y, width, height, area in components:
        if area < 6 or width < 2 or height < 2:
            continue
        if x <= 0 or y <= 0:
            continue
        if x + width >= crop_width or y + height >= crop_height:
            continue
        interior.append((x, y, width, height, area))
    if not 1 <= len(interior) <= 3:
        return piece

    ink_left = min(item[0] for item in interior)
    ink_top = min(item[1] for item in interior)
    ink_right = max(item[0] + item[2] for item in interior)
    ink_bottom = max(item[1] + item[3] for item in interior)
    ink_width = ink_right - ink_left
    ink_height = ink_bottom - ink_top
    ink_area = sum(item[4] for item in interior)
    if ink_left > max(5.0, median_width * 0.35):
        return piece
    if not (2.0 <= ink_width <= max(8.0, median_width * 0.55)):
        return piece
    if not (median_height * 0.35 <= ink_height <= median_height * 1.15):
        return piece
    if ink_area < 12:
        return piece

    global_left = left + ink_left
    global_top = top + ink_top
    global_right = left + ink_right
    global_bottom = top + ink_bottom
    punctuation_segment = {
        "text": suffix,
        "orientation": "horizontal",
        "x": global_left / page_width,
        "y": 1.0 - (global_bottom / page_height),
        "width": (global_right - global_left) / page_width,
        "height": (global_bottom - global_top) / page_height,
        "source": "horizontal-trailing-punctuation-ink-v1",
        "recognition_correction": "horizontal-trailing-punctuation-ink-v1",
    }

    result = dict(piece)
    result["segments"] = segments + [punctuation_segment]
    old_left = float(piece.get("x") or 0.0)
    old_right = old_left + float(piece.get("width") or 0.0)
    new_right = max(old_right, global_right / page_width)
    result["x"] = min(old_left, global_left / page_width)
    result["width"] = new_right - float(result["x"])
    result["geometry_source"] = (
        str(piece.get("geometry_source") or "vision-accurate-range-v2")
        + "+horizontal-trailing-punctuation-ink-v1"
    )
    result["trailing_punctuation_ink_consensus"] = {
        "text": suffix,
        "source": "observed-page-ink-v1",
        "component_count": len(interior),
        "ink_area": ink_area,
    }
    return result


def _relabel_nfkc_equivalent_segment_surfaces(
    piece: dict[str, object],
) -> dict[str, object]:
    """Relabel existing glyph boxes when text differs only by Unicode width.

    This is geometry-preserving: every observed segment keeps its bbox/source.
    It only changes a one-character segment label when that character and the
    accepted final-text character are compatibility-equivalent under NFKC.
    """
    target = _compact_surface(piece.get("text"))
    segments = [
        dict(item)
        for item in piece.get("segments") or []
        if isinstance(item, dict)
    ]
    if not target or not segments:
        return piece

    surfaces = [_compact_surface(item.get("text")) for item in segments]
    if not surfaces or any(len(surface) != 1 for surface in surfaces):
        return piece
    if len(surfaces) != len(target):
        return piece

    observed = "".join(surfaces)
    if observed == target:
        return piece
    if unicodedata.normalize("NFKC", observed) != unicodedata.normalize("NFKC", target):
        return piece

    changed: list[dict[str, object]] = []
    for index, (old, new) in enumerate(zip(surfaces, target)):
        if old == new:
            continue
        if unicodedata.normalize("NFKC", old) != unicodedata.normalize("NFKC", new):
            return piece
        old_nfkc = unicodedata.normalize("NFKC", old)
        new_nfkc = unicodedata.normalize("NFKC", new)
        if len(old_nfkc) != 1 or len(new_nfkc) != 1 or old_nfkc != new_nfkc:
            return piece
        if not (
            (old.isascii() and not new.isascii())
            or (new.isascii() and not old.isascii())
        ):
            return piece
        changed.append({"index": index, "from": old, "to": new})

    if not changed:
        return piece

    for change in changed:
        index = int(change["index"])
        segments[index]["text"] = str(change["to"])
        segments[index]["recognition_correction"] = "nfkc-segment-surface-relabel-v1"

    result = dict(piece)
    result["segments"] = segments
    result["nfkc_segment_relabel"] = True
    result["nfkc_segment_relabel_changes"] = changed
    return result




def _relabel_raw_verified_segment_surfaces(
    piece: dict[str, object],
) -> dict[str, object]:
    """Relabel existing glyph boxes when detector raw text confirms final text.

    This is a geometry-preserving final consistency repair. It applies only
    when detector raw text and accepted final text are exactly identical, the
    number of observed one-character glyph boxes already matches that text,
    and only the stale labels on those boxes disagree.
    """
    target = _compact_surface(piece.get("text"))
    raw = _compact_surface(piece.get("raw_text"))
    segments = [
        dict(item)
        for item in piece.get("segments") or []
        if isinstance(item, dict)
    ]
    if not target or raw != target or not segments:
        return piece
    if len(target) != len(segments):
        return piece

    surfaces = [_compact_surface(item.get("text")) for item in segments]
    if any(len(surface) != 1 for surface in surfaces):
        return piece
    observed = "".join(surfaces)
    if observed == target:
        return piece

    # This repair is only for already-observed glyph geometry, not synthetic
    # placeholders or geometry reconstruction.
    for item in segments:
        if _number(item.get("width"), 0.0) <= 0.0 or _number(item.get("height"), 0.0) <= 0.0:
            return piece
        source = str(item.get("source") or "")
        if not source.startswith("vision-"):
            return piece

    changes: list[dict[str, object]] = []
    for index, (old, new) in enumerate(zip(surfaces, target)):
        if old == new:
            continue
        changes.append({"index": index, "from": old, "to": new})
        segments[index]["text"] = new
        segments[index]["recognition_correction"] = "raw-verified-segment-relabel-v1"

    if not changes:
        return piece

    result = dict(piece)
    result["segments"] = segments
    result["raw_verified_segment_relabel"] = True
    result["raw_verified_segment_relabel_changes"] = changes
    return result


def _split_repeated_kana_union_segment_from_page_ink(
    image: Image.Image,
    piece: dict[str, object],
) -> dict[str, object]:
    """Split one union Vision box when final text confirms a repeated kana pair.

    Some tiny two-kana labels are returned by Vision as one real union box with
    a one-kana detector surface, while MangaOCR correctly reads two identical
    kana.  Recover per-glyph geometry only when the union-box pixels contain two
    similarly sized interior ink components.  No proportional/interpolated box
    is synthesized.
    """
    if str(piece.get("orientation") or "") != "horizontal":
        return piece
    if str(piece.get("selected_hypothesis_id") or "") != "manga-ocr":
        return piece

    target = _compact_surface(piece.get("text"))
    raw = _compact_surface(piece.get("raw_text"))
    if len(target) != 2 or len(raw) != 1 or target != raw + raw:
        return piece
    if not ("\u3040" <= raw <= "\u30ff"):
        return piece

    segments = [
        dict(item)
        for item in piece.get("segments") or []
        if isinstance(item, dict)
    ]
    if len(segments) != 1 or _compact_surface(segments[0].get("text")) != raw:
        return piece
    segment = segments[0]
    if not str(segment.get("source") or "").startswith("vision-"):
        return piece
    if _number(segment.get("width"), 0.0) <= 0.0 or _number(segment.get("height"), 0.0) <= 0.0:
        return piece

    page_width, page_height = image.size
    if page_width <= 0 or page_height <= 0:
        return piece
    x = max(0.0, min(1.0, _number(segment.get("x"), 0.0)))
    y = max(0.0, min(1.0, _number(segment.get("y"), 0.0)))
    width = max(0.0, min(1.0 - x, _number(segment.get("width"), 0.0)))
    height = max(0.0, min(1.0 - y, _number(segment.get("height"), 0.0)))
    left = max(0, min(page_width - 1, int(math.floor(x * page_width))))
    right = max(left + 1, min(page_width, int(math.ceil((x + width) * page_width))))
    top = max(0, min(page_height - 1, int(math.floor((1.0 - y - height) * page_height))))
    bottom = max(top + 1, min(page_height, int(math.ceil((1.0 - y) * page_height))))

    crop = image.crop((left, top, right, bottom)).convert("L")
    try:
        crop_width, crop_height = crop.size
        if crop_width < 8 or crop_height < 8:
            return piece
        pixels = [int(value) for value in crop.getdata()]
        if not pixels:
            return piece
        ordered = sorted(pixels)
        p20 = ordered[max(0, int(len(ordered) * 0.20) - 1)]
        p80 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.80))]
        threshold = min(180, max(90, int((p20 + p80) * 0.48)))
        binary = crop.point(lambda value: 255 if int(value) <= threshold else 0)
        try:
            components = _binary_components(binary)
        finally:
            binary.close()
    finally:
        crop.close()

    min_height = max(3, int(round(crop_height * 0.22)))
    min_area = max(4, int(round(crop_height * 0.30)))
    candidates: list[tuple[int, int, int, int, int]] = []
    for cx, cy, cw, ch, area in components:
        if cw < 2 or ch < min_height or area < min_area:
            continue
        # A clipped neighbouring glyph/art fragment often enters from the crop
        # edge.  The repeated kana must be represented by two interior blobs.
        if cx <= 0 or cy <= 0 or cx + cw >= crop_width or cy + ch >= crop_height:
            continue
        candidates.append((cx, cy, cw, ch, area))

    candidates.sort(key=lambda item: item[0])
    if len(candidates) != 2:
        return piece
    left_component, right_component = candidates
    if left_component[0] + left_component[2] > right_component[0] + 1:
        return piece

    heights = [item[3] for item in candidates]
    widths = [item[2] for item in candidates]
    areas = [item[4] for item in candidates]
    if min(heights) / max(heights) < 0.65:
        return piece
    if min(widths) / max(widths) < 0.55:
        return piece
    if min(areas) / max(areas) < 0.45:
        return piece

    output_segments: list[dict[str, object]] = []
    for char, (cx, cy, cw, ch, area) in zip(target, candidates):
        output_segments.append({
            "text": char,
            "orientation": "horizontal",
            "x": (left + cx) / page_width,
            "y": 1.0 - ((top + cy + ch) / page_height),
            "width": cw / page_width,
            "height": ch / page_height,
            "source": "repeated-kana-ink-split-v1",
            "recognition_correction": "repeated-kana-ink-split-v1",
            "ink_area": area,
        })

    result = dict(piece)
    result["segments"] = output_segments
    result["repeated_kana_ink_split"] = {
        "source": "observed-page-ink-v1",
        "text": target,
        "component_count": 2,
        "threshold": threshold,
    }
    result["geometry_source"] = (
        str(piece.get("geometry_source") or "")
        + ("+" if piece.get("geometry_source") else "")
        + "repeated-kana-ink-split-v1"
    )
    return result


def _short_raw_empty_rectangle_art_noise(item: dict[str, object]) -> bool:
    """Reject tiny vertical kana hallucinations from rectangle-only line art.

    This is deliberately narrower than the generic semantic-noise filters.  A
    real short reading can be tiny, but the false regions this guard targets
    have no detector surface, no glyph segment surface, only a low-confidence
    Vision rectangle, and are physically one-row tall despite being labelled
    vertical.  MangaOCR can turn panel/hair strokes in exactly that geometry
    into plausible two-kana SFX.
    """
    if str(item.get("orientation") or "") != "vertical":
        return False
    if str(item.get("orientation_reason") or "") != "japanese-multicolumn-geometry":
        return False
    if str(item.get("selected_hypothesis_id") or "") != "manga-ocr":
        return False
    if str(item.get("source") or "").strip():
        return False
    detector = str(item.get("detector") or "")
    if "vision-rectangles" not in detector:
        return False
    if _number(item.get("confidence"), 1.0) > 0.30:
        return False
    if _compact_surface(item.get("raw_text")):
        return False
    segment_surface = _compact_surface(
        "".join(
            str(segment.get("text") or "")
            for segment in item.get("segments") or []
            if isinstance(segment, dict)
        )
    )
    if segment_surface:
        return False
    text = unicodedata.normalize("NFKC", _compact_surface(item.get("text")))
    if not (2 <= len(text) <= 3):
        return False
    if not all("\u3040" <= character <= "\u30ff" for character in text):
        return False
    # These candidates are only ~one small text row tall.  A genuine vertical
    # two/three-glyph lane should either be taller or have detector-backed glyph
    # geometry; without either signal we prefer to reject the art hallucination.
    if _number(item.get("height"), 1.0) > 0.025:
        return False
    return True


def _suppress_short_raw_empty_rectangle_art_noise(
    regions: list[dict[str, object]],
) -> list[dict[str, object]]:
    return [region for region in regions if not _short_raw_empty_rectangle_art_noise(region)]


def _finalize_worker_output_regions(
    regions: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Apply guards that must hold at the worker JSON serialization boundary.

    Most cleanup belongs inside `_recognize_regions`, but worker output is the
    durable contract consumed by the manga service and cache.  Re-apply narrow
    monotone suppression rules here so a later recognition/refactor stage
    cannot accidentally serialize a candidate that already satisfies a final
    rejection predicate.
    """
    return _suppress_short_raw_empty_rectangle_art_noise(regions)


def _recover_short_fullwidth_digit_geometry_from_page_ink(
    image: Image.Image,
    piece: dict[str, object],
) -> dict[str, object]:
    """Attach real glyph boxes to short full-width digit labels with no geometry.

    Some low-confidence rectangle detections have a valid MangaOCR surface but
    only an empty placeholder segment.  For short horizontal full-width digit
    labels, recover geometry only when the page pixels contain exactly one
    strong connected ink component per accepted digit.
    """
    if str(piece.get("orientation") or "") != "horizontal":
        return piece

    target = _compact_surface(piece.get("text"))
    if not (1 <= len(target) <= 4):
        return piece
    if not all(
        char.isdigit()
        and not char.isascii()
        and unicodedata.normalize("NFKC", char).isascii()
        and unicodedata.normalize("NFKC", char).isdigit()
        for char in target
    ):
        return piece
    if _compact_surface(piece.get("raw_text")):
        return piece
    if str(piece.get("selected_hypothesis_id") or "") != "manga-ocr":
        return piece
    if "vision-rectangles" not in str(piece.get("detector") or ""):
        return piece
    confidence = float(piece.get("confidence") or 0.0)
    if confidence > 0.40:
        return piece

    segments = [
        dict(item)
        for item in piece.get("segments") or []
        if isinstance(item, dict)
    ]
    segment_surface = _compact_surface(
        "".join(str(item.get("text") or "") for item in segments)
    )
    if segment_surface:
        return piece

    page_width, page_height = image.size
    if page_width <= 0 or page_height <= 0:
        return piece
    x = max(0.0, min(1.0, float(piece.get("x") or 0.0)))
    y = max(0.0, min(1.0, float(piece.get("y") or 0.0)))
    width = max(0.0, min(1.0 - x, float(piece.get("width") or 0.0)))
    height = max(0.0, min(1.0 - y, float(piece.get("height") or 0.0)))
    left = max(0, min(page_width - 1, int(math.floor(x * page_width))))
    right = max(left + 1, min(page_width, int(math.ceil((x + width) * page_width))))
    top = max(
        0,
        min(page_height - 1, int(math.floor((1.0 - y - height) * page_height))),
    )
    bottom = max(
        top + 1,
        min(page_height, int(math.ceil((1.0 - y) * page_height))),
    )
    crop = image.crop((left, top, right, bottom)).convert("L")
    try:
        crop_width, crop_height = crop.size
        if crop_width < 10 or crop_height < 12:
            return piece
        pixels = [int(value) for value in crop.getdata()]
        if not pixels:
            return piece
        ordered = sorted(pixels)
        p20 = ordered[max(0, int(len(ordered) * 0.20) - 1)]
        p80 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.80))]
        threshold = min(175, max(80, int((p20 + p80) * 0.48)))
        binary = crop.point(lambda value: 255 if int(value) <= threshold else 0)
        try:
            components = _binary_components(binary)
        finally:
            binary.close()
    finally:
        crop.close()

    minimum_height = max(5, int(round(crop_height * 0.35)))
    minimum_area = max(10, int(round(crop_height * 0.80)))
    candidates: list[tuple[int, int, int, int, int]] = []
    for cx, cy, cw, ch, area in components:
        if ch < minimum_height or area < minimum_area or cw < 2:
            continue
        # Keep the main horizontal digit row. Small ruby/adjacent text below it
        # is intentionally excluded.
        if cy > crop_height * 0.55:
            continue
        candidates.append((cx, cy, cw, ch, area))

    candidates.sort(key=lambda item: item[0])
    if len(candidates) != len(target):
        return piece

    heights = [item[3] for item in candidates]
    widths = [item[2] for item in candidates]
    if min(heights) / max(heights) < 0.72:
        return piece
    if min(widths) / max(widths) < 0.55:
        return piece

    output_segments: list[dict[str, object]] = []
    for char, (cx, cy, cw, ch, area) in zip(target, candidates):
        output_segments.append({
            "text": char,
            "orientation": "horizontal",
            "x": (left + cx) / page_width,
            "y": 1.0 - ((top + cy + ch) / page_height),
            "width": cw / page_width,
            "height": ch / page_height,
            "source": "short-fullwidth-digit-ink-v1",
            "recognition_correction": "short-fullwidth-digit-ink-v1",
            "ink_area": area,
        })

    result = dict(piece)
    result["segments"] = output_segments
    result["short_fullwidth_digit_ink_geometry"] = {
        "source": "observed-page-ink-v1",
        "component_count": len(candidates),
        "threshold": threshold,
    }
    result["geometry_source"] = (
        str(piece.get("geometry_source") or "")
        + ("+" if piece.get("geometry_source") else "")
        + "short-fullwidth-digit-ink-v1"
    )
    return result


def _sync_chapter_core_segments_to_text(
    piece: dict[str, object],
) -> dict[str, object]:
    """Keep clickable chapter glyph labels aligned with the accepted row text."""
    if (
        str(piece.get("orientation") or "") != "horizontal"
        or not _chapter_prefix_surface(piece.get("text"))
    ):
        return piece
    target = _chapter_quote_core_characters(piece.get("text"))
    segments = [
        dict(item)
        for item in piece.get("segments") or []
        if isinstance(item, dict)
    ]
    indexed: list[tuple[int, str]] = []
    for index, segment in enumerate(segments):
        chars = _chapter_quote_core_characters(segment.get("text"))
        if len(chars) == 1:
            indexed.append((index, chars[0]))
    if len(indexed) != len(target) or not target:
        return piece

    observed = [character for _index, character in indexed]
    similarity = difflib.SequenceMatcher(
        a="".join(target),
        b="".join(observed),
        autojunk=False,
    ).ratio()
    if similarity < 0.75:
        return piece

    repairs: list[dict[str, object]] = []
    for study_index, ((segment_index, old), new) in enumerate(
        zip(indexed, target)
    ):
        if old == new:
            continue
        segment = dict(segments[segment_index])
        original_text = str(segment.get("text") or "")
        normalized = _normalize_line_surface(original_text)
        if len(_chapter_quote_core_characters(normalized)) != 1:
            continue
        # Chapter range segments are single-glyph boxes; replacing the label
        # keeps the geometry while fixing dictionary/Jiten lookup identity.
        segment["text"] = new
        segment["recognition_correction"] = "chapter-final-core-sync-v1"
        segments[segment_index] = segment
        repairs.append(
            {
                "study_index": study_index,
                "segment_index": segment_index,
                "from": old,
                "to": new,
            }
        )
    if not repairs:
        return piece
    result = dict(piece)
    result["segments"] = segments
    result["chapter_segment_sync"] = repairs
    return result


def _refresh_short_mixed_case_latin_label(
    model: object,
    image: Image.Image,
    piece: dict[str, object],
) -> dict[str, object]:
    """Normalize a short mixed-case Latin label from an exact local OCR crop.

    Accurate Vision occasionally flips the case of one glyph in compact labels
    (for example ``VOl.1``).  Accept a local MangaOCR surface only when it is
    ASCII after NFKC, differs solely by letter case, and the detector surface is
    itself suspiciously mixed-case while the local OCR is uniformly cased.
    """
    old = _normalize_line_surface(piece.get("text"))
    if not (3 <= len(old) <= 10):
        return piece
    if not re.fullmatch(r"[A-Za-z0-9._-]+", old):
        return piece
    letters = [character for character in old if character.isascii() and character.isalpha()]
    if len(letters) < 2:
        return piece
    if not (any(character.islower() for character in letters) and any(character.isupper() for character in letters)):
        return piece

    crop = _crop_region(image, piece)
    try:
        target = _normalize_line_surface(str(model(crop) or "").strip())  # type: ignore[operator]
    finally:
        crop.close()
    if not target or not re.fullmatch(r"[A-Za-z0-9._-]+", target):
        return piece
    if target.casefold() != old.casefold() or len(target) != len(old):
        return piece
    target_letters = [character for character in target if character.isalpha()]
    if not target_letters:
        return piece
    if not (all(character.islower() for character in target_letters) or all(character.isupper() for character in target_letters)):
        return piece
    if target == old:
        return piece

    segments = [dict(item) for item in piece.get("segments") or [] if isinstance(item, dict)]
    if len(segments) != len(old) or len(target) != len(old):
        return piece
    segment_surface = _normalize_line_surface(
        "".join(str(item.get("text") or "") for item in segments)
    )
    if segment_surface.casefold() != old.casefold():
        return piece
    relabeled: list[dict[str, object]] = []
    exact = 0
    for segment, old_char, target_char in zip(segments, old, target):
        item = dict(segment)
        if old_char == target_char:
            exact += 1
        item["text"] = target_char
        item["recognition_correction"] = "local-mangaocr-case-v1"
        relabeled.append(_refine_horizontal_segment_ink(image, item))
    exact_ratio = exact / max(1, len(old))
    normalized_cost = 1.0 - exact_ratio

    result = dict(piece)
    result["text"] = target
    result["raw_text"] = target
    result["segments"] = relabeled
    result["geometry_source"] = "vision-accurate-ink-v4+mangaocr-case-consensus-v1"
    result["line_ocr_text"] = target
    result["line_ocr_alignment"] = round(exact_ratio, 4)
    result["line_ocr_cost"] = round(normalized_cost, 4)
    result["latin_case_consensus"] = {
        "from": old,
        "to": target,
        "source": "local-mangaocr-case-v1",
    }
    return result


def _refresh_horizontal_line_with_mangaocr(
    model: object, image: Image.Image, piece: dict[str, object]
) -> dict[str, object]:
    source = str(piece.get("source") or "")
    if "/line-split-v2" not in source or str(piece.get("orientation") or "") != "horizontal":
        return piece
    old_surface = _normalize_line_surface(piece.get("text"))
    old_japanese = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff々〆ヶ]", old_surface))
    if old_japanese < 2:
        return _refresh_short_mixed_case_latin_label(model, image, piece)
    if (
        bool(piece.get("full_region_ocr_relabel"))
        or bool(piece.get("caption_region_consensus"))
        or bool(piece.get("clipped_horizontal_sfx_recovery"))
    ):
        preserved = dict(piece)
        preserved["segments"] = [
            _refine_horizontal_segment_ink(image, dict(item))
            for item in piece.get("segments") or []
            if isinstance(item, dict)
        ]
        preserved["geometry_source"] = "vision-accurate-ink-v4+full-region-mangaocr-v1"
        return preserved
    crop = _crop_region(image, piece)
    try:
        line_text = str(model(crop) or "").strip()  # type: ignore[operator]
    finally:
        crop.close()
    target = _normalize_line_surface(line_text)
    if not target:
        return piece
    target_japanese = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff々〆ヶ]", target))
    if target_japanese < max(2, int(old_japanese * 0.55)):
        return piece
    # Page numbers were deliberately removed from geometry. Strip only a
    # trailing numeric navigation suffix from MangaOCR before alignment.
    if piece.get("page_number_tail_removed"):
        target = re.sub(r"[-—–ー―]*\d{1,4}$", "", target)
    segments = [dict(item) for item in piece.get("segments") or [] if isinstance(item, dict)]
    aligned, exact_ratio, normalized_cost = _align_horizontal_segments_to_ocr(segments, target)
    if exact_ratio < 0.55 or normalized_cost > 0.72:
        piece["line_ocr_text"] = target
        piece["line_ocr_alignment"] = round(exact_ratio, 4)
        piece["line_ocr_cost"] = round(normalized_cost, 4)
        return piece
    aligned = [_refine_horizontal_segment_ink(image, item) for item in aligned]
    item = dict(piece)
    item["text"] = target
    item["raw_text"] = target
    item["segments"] = aligned
    item["geometry_source"] = "vision-accurate-aligned-v4+mangaocr-line-v1"
    item["line_ocr_alignment"] = round(exact_ratio, 4)
    item["line_ocr_cost"] = round(normalized_cost, 4)
    return item

def _clean_horizontal_line_segments(group: list[dict[str, object]]) -> list[dict[str, object]]:
    """Keep main printed glyph rows; drop ruby and duplicate enclosing observations."""
    if not group:
        return []
    rows = [dict(item) for item in group]
    max_height = max(float(item.get("height") or 0.0) for item in rows)
    # Furigana on the supplied One Piece contents page is ~25-40% of the main
    # glyph height. Page numbers and normal title observations are >=55%.
    minimum_height = max(0.006, max_height * 0.52)
    main = [item for item in rows if float(item.get("height") or 0.0) >= minimum_height]
    if main:
        rows = main

    def bounds(item: dict[str, object]) -> tuple[float, float]:
        x1 = float(item.get("x") or 0.0)
        return x1, x1 + float(item.get("width") or 0.0)

    # Vision can return both one enclosing observation for a whole TOC row and
    # finer title/page-number observations. Prefer the finer set when at least
    # two children cover most of the enclosing observation's horizontal span.
    drop: set[int] = set()
    for index, outer in enumerate(rows):
        ox1, ox2 = bounds(outer)
        owidth = max(1e-9, ox2 - ox1)
        children: list[dict[str, object]] = []
        for other_index, inner in enumerate(rows):
            if other_index == index:
                continue
            ix1, ix2 = bounds(inner)
            if ix1 < ox1 - 0.004 or ix2 > ox2 + 0.004:
                continue
            if float(inner.get("width") or 0.0) >= float(outer.get("width") or 0.0) * 0.92:
                continue
            if _horizontal_overlap_ratio(outer, inner) < 0.55:
                continue
            children.append(inner)
        if len(children) < 2:
            continue
        intervals = sorted(bounds(child) for child in children)
        covered = 0.0
        cursor_start = cursor_end = None
        for x1, x2 in intervals:
            x1 = max(ox1, x1); x2 = min(ox2, x2)
            if x2 <= x1:
                continue
            if cursor_start is None:
                cursor_start, cursor_end = x1, x2
            elif x1 <= float(cursor_end) + 0.01:
                cursor_end = max(float(cursor_end), x2)
            else:
                covered += float(cursor_end) - float(cursor_start)
                cursor_start, cursor_end = x1, x2
        if cursor_start is not None:
            covered += float(cursor_end) - float(cursor_start)
        if covered >= owidth * 0.62:
            drop.add(index)
    if drop and len(drop) < len(rows):
        rows = [item for index, item in enumerate(rows) if index not in drop]

    rows, _page_tail_removed = _strip_horizontal_page_number_tail(rows)
    rows.sort(key=lambda item: float(item.get("x") or 0.0))
    return rows


def _relabel_horizontal_line_pieces_from_full_ocr(
    region: dict[str, object],
    pieces: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Project a strong full-region MangaOCR surface onto exact Vision rows.

    Accurate Vision is excellent at horizontal glyph boxes but can misread a
    whole title row (e.g. `特のが挙`). MangaOCR may already have the correct full
    surface (`村の少年...`). If normalized character counts are identical and a
    majority of the exact stream still agrees, keep Vision geometry and merely
    relabel its characters from the stronger full OCR hypothesis.
    """
    if len(pieces) < 2:
        return pieces
    target = _normalize_line_surface(region.get("text"))
    if _japanese_character_count(target) < 4:
        return pieces

    counts: list[int] = []
    current_parts: list[str] = []
    for piece in pieces:
        segments = [item for item in piece.get("segments") or [] if isinstance(item, dict)]
        if not segments or not all(
            str(item.get("source") or "").startswith("vision-accurate")
            for item in segments
        ):
            return pieces
        surface = "".join(_normalize_line_surface(item.get("text")) for item in segments)
        if not surface:
            return pieces
        counts.append(len(surface))
        current_parts.append(surface)

    current = "".join(current_parts)
    if len(target) != len(current):
        return pieces
    exact = sum(left == right for left, right in zip(current, target)) / max(1, len(target))
    similarity = difflib.SequenceMatcher(a=current, b=target, autojunk=False).ratio()
    if exact < 0.55 or similarity < 0.68:
        return pieces

    output: list[dict[str, object]] = []
    cursor = 0
    for piece, count in zip(pieces, counts):
        surface = target[cursor : cursor + count]
        cursor += count
        chars = list(surface)
        segments = [dict(item) for item in piece.get("segments") or [] if isinstance(item, dict)]
        if len(chars) != len(segments):
            return pieces
        for segment, character in zip(segments, chars):
            segment["text"] = character
            segment["recognition_correction"] = "full-region-mangaocr-line-relabel-v1"
        updated = dict(piece)
        updated["text"] = surface
        updated["raw_text"] = surface
        updated["segments"] = segments
        updated["full_region_ocr_relabel"] = True
        updated["full_region_ocr_text"] = target
        updated["geometry_source"] = "vision-accurate-range-v2+full-region-mangaocr-v1"
        output.append(updated)
    return output



def _project_caption_surface_to_segments(
    segments: list[dict[str, object]],
    target_text: str,
    *,
    parent: dict[str, object],
    correction: str,
) -> list[dict[str, object]] | None:
    """Project a near-identical caption surface onto observed horizontal boxes.

    Caption Vision can merge exactly one printed glyph into a neighbour.  Keep
    all observed boxes where possible and synthesize at most one additional
    horizontal box.  This is deliberately much narrower than proportional
    whole-line geometry.
    """
    target = _study_surface_characters(target_text)
    source_segments = [dict(item) for item in segments if isinstance(item, dict)]
    source_chars: list[str] = []
    for item in source_segments:
        chars = _study_surface_characters(item.get("text"))
        if len(chars) != 1:
            return None
        source_chars.append(chars[0])
    if not target or not source_chars:
        return None
    if len(target) < len(source_chars) or len(target) > len(source_chars) + 1:
        return None
    current = "".join(source_chars)
    desired = "".join(target)
    similarity = difflib.SequenceMatcher(a=current, b=desired, autojunk=False).ratio()
    if similarity < 0.45:
        return None

    matcher = difflib.SequenceMatcher(a=source_chars, b=target, autojunk=False)
    output: list[dict[str, object]] = []
    synthetic_count = 0
    widths = sorted(float(item.get("width") or 0.0) for item in source_segments if float(item.get("width") or 0.0) > 0)
    reference_width = statistics.median(widths) if widths else 0.02

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        source_span = source_segments[i1:i2]
        target_span = target[j1:j2]
        if tag == "equal":
            for item, character in zip(source_span, target_span):
                copied = dict(item)
                copied["text"] = character
                output.append(copied)
            continue
        if tag == "delete":
            return None
        if tag == "replace":
            if len(source_span) == len(target_span):
                for item, character in zip(source_span, target_span):
                    copied = dict(item)
                    copied["text"] = character
                    copied["recognition_correction"] = correction
                    output.append(copied)
                continue
            if len(source_span) == 1 and len(target_span) == 2 and synthetic_count == 0:
                original = dict(source_span[0])
                width = float(original.get("width") or 0.0)
                if width <= 0:
                    return None
                half = width / 2.0
                first = dict(original)
                second = dict(original)
                first.update({
                    "text": target_span[0],
                    "width": round(half, 6),
                    "source": "caption-consensus-synthetic-v1",
                    "geometry_status": "approximate",
                    "recognition_correction": correction,
                })
                second.update({
                    "text": target_span[1],
                    "x": round(float(original.get("x") or 0.0) + half, 6),
                    "width": round(width - half, 6),
                    "source": "caption-consensus-synthetic-v1",
                    "geometry_status": "approximate",
                    "recognition_correction": correction,
                })
                output.extend((first, second))
                synthetic_count += 1
                continue
            return None
        if tag == "insert":
            if len(target_span) != 1 or synthetic_count != 0:
                return None
            if output:
                previous = output[-1]
                x = float(previous.get("x") or 0.0) + float(previous.get("width") or 0.0)
                width = reference_width
                parent_right = float(parent.get("x") or 0.0) + float(parent.get("width") or 0.0)
                width = min(width, max(0.0, parent_right - x))
                if width < reference_width * 0.45:
                    return None
                synthetic = dict(previous)
                synthetic.update({
                    "text": target_span[0],
                    "x": round(x, 6),
                    "width": round(width, 6),
                    "source": "caption-consensus-synthetic-v1",
                    "geometry_status": "approximate",
                    "recognition_correction": correction,
                })
            elif source_segments:
                following = source_segments[0]
                width = min(reference_width, float(following.get("x") or 0.0) - float(parent.get("x") or 0.0))
                if width < reference_width * 0.45:
                    return None
                synthetic = dict(following)
                synthetic.update({
                    "text": target_span[0],
                    "x": round(float(following.get("x") or 0.0) - width, 6),
                    "width": round(width, 6),
                    "source": "caption-consensus-synthetic-v1",
                    "geometry_status": "approximate",
                    "recognition_correction": correction,
                })
            else:
                return None
            output.append(synthetic)
            synthetic_count += 1
            continue
        return None

    if "".join(str(item.get("text") or "") for item in output) != desired:
        return None
    return output


def _caption_second_row_anchor(target: str, detector_row: str) -> int | None:
    """Find a unique >=3-character prefix anchor for caption row two."""
    normalized = _normalize_line_surface(detector_row)
    if len(normalized) < 3:
        return None
    for length in range(min(len(normalized), 10), 2, -1):
        anchor = normalized[:length]
        if _japanese_character_count(anchor) < 2:
            continue
        if target.count(anchor) == 1:
            return target.index(anchor)
    return None


def _relabel_horizontal_caption_pieces_from_region_consensus(
    region: dict[str, object],
    pieces: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Preserve complementary whole-caption Vision/MangaOCR evidence.

    Vision often gets caption row boundaries and a mixed-script name right while
    MangaOCR gets a Japanese descriptor right only with full-card context.
    Combine them only for a compact exact multi-row observation.  At most one
    missing glyph box per row may be synthesized.
    """
    if not _coherent_horizontal_multiline_exact_observation(region):
        return pieces
    if not (2 <= len(pieces) <= 4):
        return pieces

    detector_rows = [
        _normalize_line_surface(part)
        for part in re.split(r"\s+", str(region.get("raw_text") or "").strip())
        if _normalize_line_surface(part)
    ]
    if len(detector_rows) != len(pieces):
        return pieces

    projected: list[dict[str, object]] = []
    for piece, detector_text in zip(pieces, detector_rows):
        segments = [dict(item) for item in piece.get("segments") or [] if isinstance(item, dict)]
        current = "".join(_normalize_line_surface(item.get("text")) for item in segments)
        similarity = difflib.SequenceMatcher(a=current, b=detector_text, autojunk=False).ratio()
        if similarity < 0.55:
            return pieces
        mapped = _project_caption_surface_to_segments(
            segments,
            detector_text,
            parent=region,
            correction="detector-multiline-caption-v1",
        )
        if mapped is None:
            return pieces
        updated = dict(piece)
        updated["text"] = detector_text
        updated["raw_text"] = detector_text
        updated["segments"] = mapped
        updated["caption_region_consensus"] = True
        updated["caption_region_consensus_kind"] = "observed-rows+detector-surface-v1"
        updated["geometry_source"] = (
            f"{str(piece.get('geometry_source') or 'vision-accurate-range-v2')}"
            "+caption-consensus-v1"
        )
        updated["word_geometry"] = "mapped_segments"
        projected.append(updated)

    target = _normalize_line_surface(region.get("text"))
    detector_surface = "".join(detector_rows)
    if (
        target
        and target != detector_surface
        and len(projected) >= 2
    ):
        anchor_index = _caption_second_row_anchor(target, detector_rows[1])
        if anchor_index is not None and anchor_index > 0:
            first_target = target[:anchor_index]
            first_detector = detector_rows[0]
            if (
                2 <= len(first_target) <= len(first_detector) + 2
                and difflib.SequenceMatcher(
                    a=first_detector, b=first_target, autojunk=False
                ).ratio() >= 0.45
            ):
                first_segments = [
                    dict(item)
                    for item in projected[0].get("segments") or []
                    if isinstance(item, dict)
                ]
                mapped = _project_caption_surface_to_segments(
                    first_segments,
                    first_target,
                    parent=region,
                    correction="full-region-caption-prefix-v1",
                )
                if mapped is not None:
                    updated = dict(projected[0])
                    updated["text"] = first_target
                    updated["raw_text"] = first_target
                    updated["segments"] = mapped
                    updated["caption_region_consensus_kind"] = (
                        "observed-rows+detector-surface+full-ocr-prefix-v1"
                    )
                    updated["full_region_ocr_text"] = target
                    projected[0] = updated
    return projected


def _split_horizontal_multiline_region(region: dict[str, object], image: Image.Image | None = None) -> list[dict[str, object]]:
    if str(region.get("orientation") or "") != "horizontal":
        return [region]
    segments = [
        dict(segment)
        for segment in region.get("segments") or []
        if isinstance(segment, dict)
        and float(segment.get("width") or 0.0) > 0
        and float(segment.get("height") or 0.0) > 0
        and str(segment.get("text") or "").strip()
    ]
    if len(segments) < 2:
        return [region]

    # Keep v13's exact-character path as a compatibility fallback, but the v14
    # detector no longer adds Fast Vision observations as standalone regions.
    exact_sources = {"vision-fast-character-v1", "vision-accurate-range-v2"}
    exact_chars = [
        segment for segment in segments
        if str(segment.get("source") or "") in exact_sources
    ]

    # Precision v14: exact Vision glyph geometry is useful for splitting only
    # when its observed text is compatible with the already-selected full-crop
    # OCR hypothesis.  On artwork, Vision can segment a logo into Latin glyphs
    # (e.g. "Maglis") while MangaOCR correctly recognizes Japanese ("でも").
    # Never let that Latin segment stream overwrite the stronger Japanese
    # full-region hypothesis.
    selected_full_text = str(region.get("text") or "").strip()
    exact_surface = "".join(
        str(segment.get("text") or "").strip()
        for segment in exact_chars
    )
    if (
        _japanese_character_count(selected_full_text) >= 1
        and exact_surface
        and _japanese_character_count(exact_surface) == 0
        and any("A" <= character.upper() <= "Z" for character in unicodedata.normalize("NFKC", exact_surface))
    ):
        preserved = dict(region)
        preserved["line_split_suppressed"] = "latin-segments-vs-japanese-full-v1"
        return [preserved]
    if len(exact_chars) >= 4:
        heights = sorted(float(segment.get("height") or 0.0) for segment in exact_chars)
        reference_height = heights[max(0, int(round((len(heights) - 1) * 0.72)))]
        minimum_main_height = max(0.004, reference_height * 0.52)
        main_chars = [
            segment for segment in exact_chars
            if float(segment.get("height") or 0.0) >= minimum_main_height
        ]
        small_chars = [
            segment for segment in exact_chars
            if float(segment.get("height") or 0.0) < minimum_main_height
        ]
        # Remove small exact observations only when they really behave like ruby
        # attached to a larger glyph.  Different-size title/logo rows (p2 cover)
        # are separate lines and must survive to the y-grouping stage.
        ruby_like = 0
        for small in small_chars:
            sx1, sx2 = _horizontal_segment_x_bounds(small)
            sc = _segment_center_y(small)
            for main in main_chars:
                mx1, mx2 = _horizontal_segment_x_bounds(main)
                overlap = max(0.0, min(sx2, mx2) - max(sx1, mx1))
                overlap_ratio = overlap / max(1e-9, min(sx2 - sx1, mx2 - mx1))
                vertical_limit = max(
                    float(main.get("height") or 0.0) * 0.55,
                    float(small.get("height") or 0.0) * 1.60,
                )
                if overlap_ratio >= 0.20 and abs(sc - _segment_center_y(main)) <= vertical_limit:
                    ruby_like += 1
                    break
        compact_small_count = len(small_chars)
        sparse_small_layer = compact_small_count <= max(6, int(math.floor(len(main_chars) * 0.75)))

        # A smaller-font baseline is not automatically ruby.  Covers/title
        # pages often put a real Japanese subtitle below a much larger Latin
        # masthead.  Preserve only coherent small rows that are clearly outside
        # the vertical envelope of the main rows and are still substantial in
        # height.  Tiny/interleaved furigana remains removable.
        standalone_small: list[dict[str, object]] = []
        if small_chars and main_chars:
            small_groups: list[list[dict[str, object]]] = []
            for segment in sorted(small_chars, key=_segment_center_y, reverse=True):
                center = _segment_center_y(segment)
                if small_groups:
                    group_height = statistics.median(
                        float(item.get("height") or 0.0) for item in small_groups[-1]
                    )
                    tolerance = max(group_height * 0.85, 0.006)
                    if abs(_segment_center_y(small_groups[-1][0]) - center) <= tolerance:
                        small_groups[-1].append(segment)
                        continue
                small_groups.append([segment])

            main_centers = [_segment_center_y(segment) for segment in main_chars]
            main_low = min(main_centers)
            main_high = max(main_centers)
            for group in small_groups:
                if len(group) < 3:
                    continue
                group_height = statistics.median(
                    float(item.get("height") or 0.0) for item in group
                )
                if group_height < reference_height * 0.44:
                    continue
                group_center = statistics.median(
                    _segment_center_y(item) for item in group
                )
                if main_low <= group_center <= main_high:
                    continue
                nearest_main = min(abs(group_center - center) for center in main_centers)
                if nearest_main < reference_height * 0.55:
                    continue
                left = min(float(item.get("x") or 0.0) for item in group)
                right = max(
                    float(item.get("x") or 0.0) + float(item.get("width") or 0.0)
                    for item in group
                )
                if right - left < max(0.08, group_height * 2.5):
                    continue
                standalone_small.extend(group)

        if (
            len(main_chars) >= 2
            and small_chars
            and (
                ruby_like >= max(1, int(math.ceil(compact_small_count * 0.80)))
                or sparse_small_layer
            )
        ):
            segments = main_chars + standalone_small

    max_height = max(float(segment.get("height") or 0.0) for segment in segments)
    region_height = float(region.get("height") or 0.0)
    if not (max_height > 0 and region_height >= max_height * 1.8):
        return [region]

    groups: list[list[dict[str, object]]] = []
    for segment in sorted(segments, key=_segment_center_y, reverse=True):
        center = _segment_center_y(segment)
        if groups:
            group_height = statistics.median(float(item.get("height") or 0.0) for item in groups[-1])
            current_height = float(segment.get("height") or 0.0)
            tolerance = max(min(group_height, current_height) * 0.80, 0.008)
            if abs(_segment_center_y(groups[-1][0]) - center) <= tolerance:
                groups[-1].append(segment)
                continue
        groups.append([segment])
    if len(groups) < 2:
        return [region]

    pieces: list[dict[str, object]] = []
    for line_index, raw_group in enumerate(groups):
        raw_sorted = sorted((dict(item) for item in raw_group), key=lambda item: float(item.get("x") or 0.0))
        group = _clean_horizontal_line_segments(raw_group)
        if not group:
            continue
        _raw_without_tail, page_tail_removed = _strip_horizontal_page_number_tail(raw_sorted)
        geometry = _geometry_union(group)
        if geometry is None:
            continue
        piece = dict(region)
        piece.update(geometry)
        piece["segments"] = group
        parts = [str(item.get("text") or "").strip() for item in group if str(item.get("text") or "").strip()]
        exact_line = bool(parts) and all(
            str(item.get("source") or "") in exact_sources
            for item in group
        )
        line_text = ("".join(parts) if exact_line else " ".join(parts)).strip()
        if line_text:
            piece["text"] = line_text
            piece["raw_text"] = line_text
        source = str(region.get("source") or "manga-ocr")
        suffix = "/line-split-v2" if exact_line else "/line-split-v3"
        # Replace previous line-split suffix instead of stacking v1/v3 forever.
        for old_suffix in ("/line-split-v1", "/line-split-v2", "/line-split-v3"):
            if source.endswith(old_suffix):
                source = source[:-len(old_suffix)]
                break
        piece["source"] = f"{source}{suffix}"
        piece["geometry_source"] = "vision-accurate-range-v2" if exact_line else "horizontal-main-segments-v3"
        piece["line_index"] = line_index
        if page_tail_removed:
            piece["page_number_tail_removed"] = True
        pieces.append(piece)
    if pieces:
        pieces = _relabel_horizontal_line_pieces_from_full_ocr(region, pieces)
        pieces = _relabel_horizontal_caption_pieces_from_region_consensus(region, pieces)
    return pieces or [region]

def _split_horizontal_multiline_regions(regions: list[dict[str, object]], image: Image.Image | None = None) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for region in regions:
        output.extend(_split_horizontal_multiline_region(region, image=image))
    for index, region in enumerate(output):
        region["order"] = index
    return output


def _layout_line_slot_boundaries(
    row_counts: list[int],
    start_y: int,
    end_y: int,
    compact: str,
) -> list[int]:
    """Place vertical-layout boundaries near ink valleys with glyph-aware spacing.

    Pure valley optimization can split a large kana at an internal white gap.
    Normal glyphs should consume roughly one pitch, while small kana and emphatic
    punctuation are allowed to consume less vertical space.
    """
    if not compact:
        return [start_y, end_y + 1]
    small = set("っッゃゅょャュョぁぃぅぇぉァィゥェォゎヮ")
    punctuation = set("！!？?。…‥〜～")
    weights = [0.68 if char in small else 0.55 if char in punctuation else 1.0 for char in compact]
    span = max(1, end_y + 1 - start_y)
    unit = span / max(1e-6, sum(weights))
    peak = max(row_counts[start_y : end_y + 1] or [1])
    boundaries = [start_y]
    cumulative = 0.0
    minimum_step = max(2, int(round(unit * 0.34)))
    tail_step = max(2, int(round(unit * 0.28)))
    radius = max(2, int(round(unit * 0.30)))
    for index, weight in enumerate(weights[:-1]):
        cumulative += weight
        target = start_y + cumulative * unit
        remaining = len(weights) - index - 2
        first = max(boundaries[-1] + minimum_step, int(round(target - radius)))
        last = min(end_y - remaining * tail_step, int(round(target + radius)))
        if first > last:
            position = max(boundaries[-1] + 1, min(end_y, int(round(target))))
        else:
            best: tuple[float, int] | None = None
            for position in range(first, last + 1):
                local = row_counts[max(start_y, position - 1) : min(end_y + 1, position + 2)]
                valley = min(local or [0]) / max(1.0, float(peak))
                distance = abs(position - target) / max(1.0, unit)
                candidate = (valley * 3.0 + distance * 0.45, position)
                if best is None or candidate < best:
                    best = candidate
            position = best[1] if best is not None else int(round(target))
        boundaries.append(position)
    boundaries.append(end_y + 1)
    return boundaries


def _vertical_leading_ink_character_segments(
    image: Image.Image,
    region: dict[str, object],
    text: object,
) -> list[dict[str, object]]:
    """Map recovered single-column text to observed row-ink glyph spans.

    This path is used only after `_vertical_leading_ink_geometry` has proved
    that the detector clipped the start of a lane.  It avoids re-introducing a
    one-character shift by uniformly redistributing the longer OCR string over
    the old box.
    """
    if not bool(region.get("leading_ink_geometry")):
        return []
    if str(region.get("orientation") or "") != "vertical":
        return []
    compact = _compact_surface(text)
    if len(compact) < 2:
        return []

    page_width, page_height = image.size
    x = max(0.0, min(1.0, _number(region.get("x"))))
    y = max(0.0, min(1.0, _number(region.get("y"))))
    width = max(0.0, min(1.0 - x, _number(region.get("width"))))
    height = max(0.0, min(1.0 - y, _number(region.get("height"))))
    left = max(0, min(page_width - 1, round(x * page_width)))
    right = max(left + 1, min(page_width, round((x + width) * page_width)))
    top = max(0, min(page_height - 1, round((1.0 - y - height) * page_height)))
    bottom = max(top + 1, min(page_height, round((1.0 - y) * page_height)))
    crop_width, crop_height = right - left, bottom - top
    if crop_width < 6 or crop_height < 12:
        return []

    crop = image.crop((left, top, right, bottom)).convert("L")
    try:
        pixels = list(crop.getdata())
    finally:
        crop.close()
    if not pixels:
        return []
    row_counts = [
        sum(1 for value in pixels[row * crop_width : (row + 1) * crop_width] if int(value) <= 165)
        for row in range(crop_height)
    ]
    minimum_row_ink = max(2, int(round(crop_width * 0.055)))
    raw_spans = _contiguous_spans([count >= minimum_row_ink for count in row_counts])
    minimum_span_height = max(3, int(round(crop_width * 0.10)))
    spans = [
        span for span in raw_spans
        if span[1] - span[0] + 1 >= minimum_span_height
    ]
    if not spans:
        return []

    # A kana can create several disconnected row spans. Treating the first N
    # spans as N characters compresses tokens such as うるせ into the top of
    # the lane. Fit exactly one glyph-aware slot per OCR character across the
    # complete observed ink extent instead.
    start_y = spans[0][0]
    end_y = spans[-1][1]
    boundaries = _layout_line_slot_boundaries(row_counts, start_y, end_y, compact)
    if len(boundaries) != len(compact) + 1:
        return []

    segments: list[dict[str, object]] = []
    for index, character in enumerate(compact):
        start = max(0, boundaries[index])
        stop = min(crop_height, boundaries[index + 1])
        if stop <= start:
            return []
        page_top = top + start
        page_bottom = top + stop
        seg_height = (page_bottom - page_top) / page_height
        seg_y = 1.0 - page_bottom / page_height
        segments.append(
            {
                "text": character,
                "orientation": "vertical",
                "x": round(x, 6),
                "y": round(max(0.0, min(1.0 - seg_height, seg_y)), 6),
                "width": round(width, 6),
                "height": round(seg_height, 6),
                "source": "vertical-leading-ink-v2",
                "geometry_status": "approximate",
            }
        )
    return segments


def _layout_line_ink_character_segments(
    image: Image.Image,
    region: dict[str, object],
    text: object,
) -> list[dict[str, object]]:
    """Split an observed vertical lane at real row-ink valleys.

    Layout recovery owns a tight line bbox, but punctuation and small kana do
    not consume equal vertical space. Equal-height slots therefore make the
    preceding Jiten token stop before the last visible glyph. Use page pixels
    to place only the internal character boundaries; the detector bbox remains
    authoritative for the lane itself.
    """
    if str(region.get("source") or "") != _LAYOUT_LINE_SOURCE:
        return []
    if str(region.get("orientation") or "") != "vertical":
        return []
    compact = _compact_surface(text)
    if len(compact) < 2:
        return []

    page_width, page_height = image.size
    x = max(0.0, min(1.0, _number(region.get("x"))))
    y = max(0.0, min(1.0, _number(region.get("y"))))
    width = max(0.0, min(1.0 - x, _number(region.get("width"))))
    height = max(0.0, min(1.0 - y, _number(region.get("height"))))
    left = max(0, min(page_width - 1, round(x * page_width)))
    right = max(left + 1, min(page_width, round((x + width) * page_width)))
    top = max(0, min(page_height - 1, round((1.0 - y - height) * page_height)))
    bottom = max(top + 1, min(page_height, round((1.0 - y) * page_height)))
    crop_width, crop_height = right - left, bottom - top
    if crop_width < 5 or crop_height < len(compact) * 3:
        return []

    crop = image.crop((left, top, right, bottom)).convert("L")
    try:
        pixels = list(crop.getdata())
    finally:
        crop.close()
    if not pixels:
        return []

    histogram = [0] * 256
    for pixel in pixels:
        histogram[int(pixel)] += 1
    total = len(pixels)
    weighted_total = sum(value * count for value, count in enumerate(histogram))
    background_weight = 0
    background_sum = 0
    best_variance = -1.0
    threshold = 145
    for value, count in enumerate(histogram):
        background_weight += count
        if background_weight <= 0:
            continue
        foreground_weight = total - background_weight
        if foreground_weight <= 0:
            break
        background_sum += value * count
        background_mean = background_sum / background_weight
        foreground_mean = (weighted_total - background_sum) / foreground_weight
        variance = background_weight * foreground_weight * (background_mean - foreground_mean) ** 2
        if variance > best_variance:
            best_variance = variance
            threshold = value
    threshold = max(55, min(205, int(threshold)))

    dark_count = sum(1 for pixel in pixels if int(pixel) <= threshold)
    dark_is_ink = dark_count <= total - dark_count
    ink = bytearray(
        1 if (int(pixel) <= threshold if dark_is_ink else int(pixel) > threshold) else 0
        for pixel in pixels
    )
    row_counts = [
        sum(ink[row * crop_width : (row + 1) * crop_width])
        for row in range(crop_height)
    ]
    minimum_row_ink = max(1, int(round(crop_width * 0.05)))
    spans = _contiguous_spans([count >= minimum_row_ink for count in row_counts])
    spans = _merge_small_gaps(spans, max(2, int(round(crop_height * 0.025))))
    spans = [span for span in spans if span[1] - span[0] + 1 >= 2]
    if not spans:
        return []
    start_y = min(start for start, _end in spans)
    end_y = max(end for _start, end in spans)
    if end_y - start_y + 1 < crop_height * 0.55:
        return []

    boundaries = _layout_line_slot_boundaries(
        row_counts,
        start_y,
        end_y,
        compact,
    )
    if len(boundaries) != len(compact) + 1:
        return []
    average = (end_y + 1 - start_y) / max(1, len(compact))
    intervals = [boundaries[index + 1] - boundaries[index] for index in range(len(compact))]
    if any(interval < max(2, average * 0.20) or interval > average * 2.45 for interval in intervals):
        return []

    segments: list[dict[str, object]] = []
    for index, character in enumerate(compact):
        slot_top = max(0, min(crop_height - 1, boundaries[index]))
        slot_bottom = max(slot_top + 1, min(crop_height, boundaries[index + 1]))
        page_top = top + slot_top
        page_bottom = top + slot_bottom
        seg_height = (page_bottom - page_top) / page_height
        seg_y = 1.0 - page_bottom / page_height
        segments.append(
            {
                "text": character,
                "orientation": "vertical",
                "x": round(x, 6),
                "y": round(max(0.0, min(1.0 - seg_height, seg_y)), 6),
                "width": round(width, 6),
                "height": round(seg_height, 6),
                "source": "layout-line-ink-v2",
                "geometry_status": "approximate",
            }
        )
    return segments


def _layout_line_character_segments(
    region: dict[str, object],
    text: object,
    image: Image.Image | None = None,
) -> list[dict[str, object]]:
    """Give one observed vertical lane usable per-character geometry.

    With the page image available, prefer ink-valley boundaries. The old
    proportional path remains the compatibility fallback for tests, synthetic
    callers and ambiguous artwork.
    """
    if str(region.get("source") or "") != _LAYOUT_LINE_SOURCE:
        return []
    if str(region.get("orientation") or "") != "vertical":
        return []
    compact = _compact_surface(text)
    if not compact:
        return []
    if image is not None:
        refined = _layout_line_ink_character_segments(image, region, compact)
        if refined:
            return refined

    x = max(0.0, min(1.0, _number(region.get("x"))))
    y = max(0.0, min(1.0, _number(region.get("y"))))
    width = max(0.0, min(1.0 - x, _number(region.get("width"))))
    height = max(0.0, min(1.0 - y, _number(region.get("height"))))
    if width <= 0.001 or height <= 0.001:
        return []

    count = len(compact)
    unit = height / count
    if unit <= 0.0005:
        return []
    segments: list[dict[str, object]] = []
    for index, character in enumerate(compact):
        seg_y = y + height - unit * (index + 1)
        segments.append(
            {
                "text": character,
                "orientation": "vertical",
                "x": round(x, 6),
                "y": round(max(0.0, min(1.0 - unit, seg_y)), 6),
                "width": round(width, 6),
                "height": round(unit, 6),
                "source": "layout-line-proportional-v1",
                "geometry_status": "approximate",
            }
        )
    return segments


def _caption_full_mangaocr_surface(region: dict[str, object]) -> str:
    """Return the shared whole-region MangaOCR hypothesis on a split caption row."""
    for hypothesis in region.get("hypotheses") or []:
        if not isinstance(hypothesis, dict):
            continue
        if str(hypothesis.get("id") or "") != "manga-ocr":
            continue
        surface = _normalize_line_surface(hypothesis.get("text"))
        if surface:
            return surface
    return ""


def _caption_split_group_key(region: dict[str, object]) -> tuple[str, str] | None:
    if str(region.get("orientation") or "") != "horizontal":
        return None
    if str(region.get("source") or "") != "manga-ocr/line-split-v2":
        return None
    surface = _caption_full_mangaocr_surface(region)
    if not surface:
        return None
    detector = str(region.get("detector") or "")
    if not detector:
        return None
    return detector, surface


def _repair_multiline_caption_from_full_region_suffix(
    regions: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Use a whole-card MangaOCR hypothesis only when a later observed row anchors it.

    Some compact character cards have exact per-row Vision geometry, but the
    detector surface on the first row is noisy.  If every split row shares the
    same whole-region MangaOCR hypothesis, the final observed row exactly matches
    the suffix of that hypothesis, and the total row lengths already partition
    the whole surface exactly, the remaining prefix can be projected back onto
    the existing row geometry without inventing new layout.

    This is intentionally a post-recognition repair: it does not make arbitrary
    whole-region MangaOCR stronger than Vision and cannot run without an exact
    suffix row already owned by the split geometry.
    """
    output = [dict(region) for region in regions]
    groups: dict[tuple[str, str], list[int]] = {}
    for index, region in enumerate(output):
        key = _caption_split_group_key(region)
        if key is None:
            continue
        groups.setdefault(key, []).append(index)

    for (_detector, full_surface), indices in groups.items():
        if not (2 <= len(indices) <= 4):
            continue
        indices.sort(
            key=lambda idx: (
                int(output[idx].get("line_index"))
                if isinstance(output[idx].get("line_index"), int)
                else 999,
                idx,
            )
        )
        pieces = [output[idx] for idx in indices]
        if any(
            not isinstance(piece.get("line_index"), int)
            for piece in pieces
        ):
            continue
        expected_indices = list(range(len(pieces)))
        if [int(piece.get("line_index")) for piece in pieces] != expected_indices:
            continue

        surfaces = [_normalize_line_surface(piece.get("text")) for piece in pieces]
        if any(not surface for surface in surfaces):
            continue
        lengths = [len(surface) for surface in surfaces]
        total = sum(lengths)
        if total != len(full_surface):
            continue
        current_surface = "".join(surfaces)
        if current_surface == full_surface:
            continue

        last_length = lengths[-1]
        if last_length < 2 or surfaces[-1] != full_surface[-last_length:]:
            continue

        prefix_current = "".join(surfaces[:-1])
        prefix_target = full_surface[:-last_length]
        similarity = difflib.SequenceMatcher(
            a=prefix_current,
            b=prefix_target,
            autojunk=False,
        ).ratio()
        if not (0.35 <= similarity < 0.95):
            continue
        if _japanese_character_count(prefix_target) < max(2, len(prefix_target) - 1):
            continue

        cursor = 0
        repaired_rows: list[dict[str, object]] = []
        valid = True
        for piece, length in zip(pieces, lengths):
            target = full_surface[cursor : cursor + length]
            cursor += length
            segments = [
                dict(segment)
                for segment in piece.get("segments") or []
                if isinstance(segment, dict)
            ]
            target_chars = _study_surface_characters(target)
            observed_single_boxes = all(
                str(segment.get("text") or "").strip()
                and _number(segment.get("width")) > 0
                and _number(segment.get("height")) > 0
                for segment in segments
            )
            if len(segments) == len(target_chars) and observed_single_boxes:
                mapped = []
                for segment, character in zip(segments, target_chars):
                    copied = dict(segment)
                    copied["text"] = character
                    copied["recognition_correction"] = (
                        "full-region-caption-suffix-consensus-v1"
                    )
                    mapped.append(copied)
            else:
                mapped = _project_caption_surface_to_segments(
                    segments,
                    target,
                    parent=piece,
                    correction="full-region-caption-suffix-consensus-v1",
                )
            if mapped is None:
                valid = False
                break
            updated = dict(piece)
            updated["text"] = target
            updated["raw_text"] = target
            updated["segments"] = mapped
            updated["caption_full_region_suffix_consensus"] = True
            updated["caption_full_region_suffix_consensus_kind"] = (
                "exact-final-row-anchor-v1"
            )
            updated["caption_full_region_suffix_consensus_full_text"] = full_surface
            updated["caption_full_region_suffix_consensus_similarity"] = round(
                similarity, 4
            )
            updated["selected_hypothesis_id"] = (
                "manga-ocr-caption-suffix-consensus-v1"
            )
            hypotheses = [
                dict(item)
                for item in updated.get("hypotheses") or []
                if isinstance(item, dict)
            ]
            for hypothesis in hypotheses:
                hypothesis["selected"] = False
            hypotheses.append(
                {
                    "id": "manga-ocr-caption-suffix-consensus-v1",
                    "text": target,
                    "source": "manga-ocr+observed-final-row-anchor",
                    "selected": True,
                }
            )
            updated["hypotheses"] = hypotheses
            repaired_rows.append(updated)
        if not valid or len(repaired_rows) != len(indices):
            continue
        for idx, row in zip(indices, repaired_rows):
            output[idx] = row

    return output


def _suppress_nested_caption_vertical_fragments(
    regions: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Drop short pseudo-vertical lanes whose centre lies inside a proven caption.

    The caption must already consist of 2-4 horizontal line-split rows that share
    one whole-region MangaOCR hypothesis and collectively reproduce that surface.
    This lets the final exact caption own its observed card while leaving normal
    vertical dialogue untouched.
    """
    groups: dict[tuple[str, str], list[dict[str, object]]] = {}
    for region in regions:
        key = _caption_split_group_key(region)
        if key is None:
            continue
        groups.setdefault(key, []).append(region)

    caption_boxes: list[tuple[float, float, float, float]] = []
    for (_detector, full_surface), pieces in groups.items():
        if not (2 <= len(pieces) <= 4):
            continue
        pieces = sorted(
            pieces,
            key=lambda piece: (
                int(piece.get("line_index"))
                if isinstance(piece.get("line_index"), int)
                else 999
            ),
        )
        if any(
            not (
                piece.get("caption_region_consensus")
                or piece.get("caption_full_region_suffix_consensus")
            )
            for piece in pieces
        ):
            continue
        combined = "".join(
            _normalize_line_surface(piece.get("text")) for piece in pieces
        )
        if combined != full_surface:
            continue
        xs = [_number(piece.get("x")) for piece in pieces]
        ys = [_number(piece.get("y")) for piece in pieces]
        x2s = [
            _number(piece.get("x")) + _number(piece.get("width"))
            for piece in pieces
        ]
        y2s = [
            _number(piece.get("y")) + _number(piece.get("height"))
            for piece in pieces
        ]
        caption_boxes.append((min(xs), min(ys), max(x2s), max(y2s)))

    if not caption_boxes:
        return regions

    output: list[dict[str, object]] = []
    for region in regions:
        text = _normalize_line_surface(region.get("text"))
        if (
            str(region.get("orientation") or "") == "vertical"
            and str(region.get("source") or "") == _LAYOUT_LINE_SOURCE
            and 1 <= len(text) <= 3
        ):
            x = _number(region.get("x"))
            y = _number(region.get("y"))
            width = _number(region.get("width"))
            height = _number(region.get("height"))
            cx = x + width / 2.0
            cy = y + height / 2.0
            if any(
                left <= cx <= right and bottom <= cy <= top
                for left, bottom, right, top in caption_boxes
            ):
                continue
        output.append(region)
    return output


def _recognize_regions(
    model: object,
    image: Image.Image,
    regions: list[dict[str, object]],
) -> list[dict[str, object]]:
    prepared = _prepare_regions_for_ocr(regions, image)
    layout_proposals = _manga_layout_line_proposals(image, prepared)
    # `_prepare_regions_for_ocr` may replace a tiny source-less Vision seed with
    # an expanded crop. Re-attach weak-lane corroboration from the original
    # detector observations after proposal blocking, so geometry validation sees
    # the independent raw component count without changing blocker semantics.
    layout_proposals = _attach_partial_weak_raw_component_support(
        image, regions, layout_proposals
    )
    cluster_member_proposals = _augment_cluster_members_with_raw_gaps(image, layout_proposals)
    layout_cluster_proposals = _layout_cluster_proposals(image, cluster_member_proposals)
    pipeline_fingerprint = _pipeline_fingerprint(image, model)
    recognized: list[dict[str, object]] = []
    for region in [*prepared, *layout_proposals, *layout_cluster_proposals]:
        item = dict(region)
        if bool(item.get("layout_search_only")) or str(item.get("detector") or "") == "layout-search-sentinel":
            continue
        item["pipeline_fingerprint"] = dict(pipeline_fingerprint)
        item["raw_text"] = str(region.get("raw_text") or region.get("text") or "").strip()
        is_layout = str(item.get("source") or "") == _LAYOUT_LINE_SOURCE
        if str(item.get("detector") or "") == _RAW_LAYOUT_DETECTOR:
            item["recognizer_crop_policy"] = _OCR_CROP_POLICY
        try:
            crop = _crop_region(image, region)
            ocr_crop = crop
            inverted_crop: Image.Image | None = None
            cluster_square_crop: Image.Image | None = None
            try:
                if str(item.get("source") or "") in {"layout-cluster-v1", "layout-cluster-v2"}:
                    cluster_square_crop = _square_pad_dark_column_crop(crop)
                    ocr_crop = cluster_square_crop
                    item["recognizer_preprocess"] = "square-pad-layout-cluster-v1"
                if str(item.get("source") or "") == "dark-block-proposal":
                    sample = crop.convert("L").resize((24, 24), Image.Resampling.BOX)
                    try:
                        mean = sum(int(value) for value in sample.getdata()) / (24 * 24)
                    finally:
                        sample.close()
                    if mean < 118:
                        inverted_crop = ImageOps.invert(crop.convert("RGB"))
                        ocr_crop = inverted_crop
                        item["recognizer_preprocess"] = "invert-dark-block-v1"
                manga_text = str(model(ocr_crop) or "").strip()  # type: ignore[operator]
                selected_text = manga_text
                selected_id = "manga-ocr"
                if _prefer_detector_recognition(item, manga_text):
                    selected_text = str(item.get("raw_text") or "").strip()
                    selected_id = "detector-recognition"
                    item["recognition_selection"] = (
                        "detector-exact-geometry-v2"
                        if _exact_vision_segment_surface(item) == _normalize_line_surface(item.get("raw_text"))
                        else "detector-preferred-v1"
                    )
                item["text"] = selected_text
                _recognition_hypotheses(item, manga_text, selected_id=selected_id)
                # Narrow vertical lines are the dominant manga case. First retry
                # them on a square white canvas; this fixes recall without
                # widening into neighbouring columns. Geometry plausibility is
                # part of acceptance so a one-glyph box cannot steal a 3-5 glyph
                # phrase from the next lane.
                if (
                    is_layout
                    and not _layout_retry_acceptable(item, selected_text)
                    and _layout_square_retry_supported(item)
                ):
                    try:
                        square_text = _recognize_layout_square_retry(model, image, region)
                    except Exception:
                        square_text = ""
                    _record_tall_merged_ocr_consensus(item, selected_text, square_text)
                    if _layout_retry_acceptable(item, square_text):
                        hypotheses = item.get("hypotheses")
                        if isinstance(hypotheses, list):
                            hypotheses.append(
                                {
                                    "id": "manga-ocr-square-retry",
                                    "text": square_text,
                                    "source": "manga-ocr",
                                    "selected": True,
                                }
                            )
                            for hypothesis in hypotheses:
                                if isinstance(hypothesis, dict) and hypothesis.get("id") != "manga-ocr-square-retry":
                                    hypothesis["selected"] = False
                        item["selected_hypothesis_id"] = "manga-ocr-square-retry"
                        item["recognizer_retry"] = "vertical-square-pad-v1"
                        selected_text = square_text
                        item["text"] = square_text

                # Context retry is deliberately failure-only. A wider crop can
                # steal adjacent manga columns, so never replace a hypothesis
                # that already passed the geometry-aware acceptance rule.
                if is_layout and not _layout_retry_acceptable(item, selected_text):
                    retry_crop = _crop_region_with_extra_y(image, region, extra_y_ratio=0.020)
                    try:
                        retry_text = str(model(retry_crop) or "").strip()  # type: ignore[operator]
                    finally:
                        retry_crop.close()
                    if _layout_retry_acceptable(item, retry_text):
                        hypotheses = item.get("hypotheses")
                        if isinstance(hypotheses, list):
                            hypotheses.append(
                                {
                                    "id": "manga-ocr-context-retry",
                                    "text": retry_text,
                                    "source": "manga-ocr",
                                    "selected": True,
                                }
                            )
                            for hypothesis in hypotheses:
                                if isinstance(hypothesis, dict) and hypothesis.get("id") != "manga-ocr-context-retry":
                                    hypothesis["selected"] = False
                        item["selected_hypothesis_id"] = "manga-ocr-context-retry"
                        item["recognizer_retry"] = "vertical-y-context-v1"
                        selected_text = retry_text
                        item["text"] = retry_text
                if is_layout and not _layout_retry_acceptable(item, selected_text):
                    xy_crop = _crop_region_with_extra_context(
                        image,
                        region,
                        extra_x_ratio=min(0.012, max(0.005, _number(region.get("width")) * 0.35)),
                        extra_y_ratio=0.030,
                    )
                    try:
                        xy_text = str(model(xy_crop) or "").strip()  # type: ignore[operator]
                    finally:
                        xy_crop.close()
                    if (
                        (
                            _layout_context_candidate_plausible(region, xy_text)
                            or _supported_short_layout_text(item, xy_text)
                        )
                        and _layout_text_geometry_plausible(item, xy_text)
                    ):
                        hypotheses = item.get("hypotheses")
                        if isinstance(hypotheses, list):
                            hypotheses.append(
                                {
                                    "id": "manga-ocr-xy-context-retry",
                                    "text": xy_text,
                                    "source": "manga-ocr",
                                    "selected": True,
                                }
                            )
                            for hypothesis in hypotheses:
                                if isinstance(hypothesis, dict) and hypothesis.get("id") != "manga-ocr-xy-context-retry":
                                    hypothesis["selected"] = False
                        item["selected_hypothesis_id"] = "manga-ocr-xy-context-retry"
                        item["recognizer_retry"] = "vertical-xy-context-v1"
                        selected_text = xy_text
                        item["text"] = xy_text
            finally:
                if inverted_crop is not None:
                    inverted_crop.close()
                if cluster_square_crop is not None:
                    cluster_square_crop.close()
                crop.close()
            item = _recover_vertical_edge_context(model, image, item)
            item["recognizer"] = "manga-ocr"
            if is_layout:
                # Phase2.5: the line bbox itself is observed ink geometry. Attach
                # an explicitly approximate per-character stream inside that one
                # tight vertical lane so multi-token Jiten parsing can create
                # hitboxes. The old contract dropped segments entirely, which is
                # why only single-token lines were clickable in the reader.
                layout_segments = _vertical_leading_ink_character_segments(
                    image, item, item.get("text")
                ) or _layout_line_character_segments(item, item.get("text"), image=image)
                if layout_segments:
                    layout_segments = _tighten_vertical_slot_ink_segments(image, layout_segments)
                    item["segments"] = layout_segments
                    item["geometry_status"] = "approximate"
                    item["geometry_source"] = "layout-line-proportional-v1"
                    item["word_geometry"] = "proportional-single-column-v1"
                    item["region_geometry_status"] = "observed"
                else:
                    item.pop("segments", None)
                    item["geometry_status"] = "observed"
                    item["geometry_source"] = str(item.get("detector") or _LAYOUT_DETECTOR)
                    item["word_geometry"] = "unavailable"
            elif not item.get("segments"):
                inferred_segments = _vertical_leading_ink_character_segments(
                    image, item, item.get("text")
                ) or _infer_vertical_character_segments(image, item, str(item["text"]))
                if inferred_segments and str(item.get("source") or "") == "dark-block-proposal":
                    column_text, column_surfaces = _recognize_dark_column_surfaces(
                        model,
                        image,
                        inferred_segments,
                        str(item.get("text") or ""),
                    )
                    if column_text:
                        item["dark_column_surfaces"] = column_surfaces
                        if column_text != _compact_surface(item.get("text")):
                            full_crop_text = str(item.get("text") or "")
                            item["text"] = column_text
                            item["raw_text"] = column_text
                            item["full_crop_text"] = full_crop_text
                            item["recognition_selection"] = "dark-column-consensus-v3"
                            hypotheses = item.get("hypotheses")
                            if isinstance(hypotheses, list):
                                for hypothesis in hypotheses:
                                    if isinstance(hypothesis, dict):
                                        hypothesis["selected"] = False
                                hypotheses.append(
                                    {
                                        "id": "dark-column-consensus",
                                        "text": column_text,
                                        "source": "manga-ocr-column-crops",
                                        "selected": True,
                                        "columns": list(column_surfaces),
                                    }
                                )
                            item["selected_hypothesis_id"] = "dark-column-consensus"
                        inferred_segments = _infer_vertical_character_segments(
                            image,
                            item,
                            column_text,
                        )
                if inferred_segments:
                    inferred_segments = _tighten_vertical_slot_ink_segments(image, inferred_segments)
                    item["segments"] = inferred_segments
                    item["geometry_source"] = str(inferred_segments[0].get("source") or "ink-grid-v1")
                    _strip_dark_block_decorative_suffix(item)
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"
            item["text"] = str(region.get("text") or "").strip()
        if not str(item.get("text") or "").strip():
            continue
        if is_layout and not _layout_retry_acceptable(item, item.get("text")):
            continue
        if _non_japanese_latin_noise(item):
            continue
        if _semantic_empty_noise(item):
            continue
        if _detector_script_conflict_noise(item):
            continue
        if _weak_synthetic_vertical_region(item):
            continue
        if _synthetic_vertical_texture_noise(image, item):
            continue
        recognized.append(item)
    recognized = _promote_wide_vertical_text_to_layout_lanes(
        image, recognized, _wide_vertical_geometry_candidates(image)
    )
    cluster_expanded: list[dict[str, object]] = []
    for recognized_item in recognized:
        cluster_expanded.extend(_split_layout_cluster_region(image, recognized_item, model=model))
    recognized = _merge_layout_cluster_donors(cluster_expanded)
    # Some title/splash pages intentionally stay as one large horizontal
    # region and therefore never reach the line-split repair path. Apply the
    # same conservative two-kanji local consensus once before splitting so
    # 管険/皆険 can become 冒険 without rewriting the surrounding title/logo.
    pre_split_repaired: list[dict[str, object]] = []
    for recognized_item in recognized:
        if str(recognized_item.get("orientation") or "") == "horizontal":
            try:
                recognized_item = _repair_short_kanji_pairs_with_mangaocr(
                    model, image, recognized_item
                )
            except Exception:
                pass
        pre_split_repaired.append(recognized_item)
    recognized = _suppress_nested_ruby_echo_layout_regions(pre_split_repaired)
    recognized = _merge_layout_recovery_regions(recognized)
    # Merge/dedupe can reconstitute an unsourced wide Vision region after the
    # earlier geometry split. Run the geometry-only donor pass once more so
    # collapsed text such as 「まだ栓もあけてない」 ends as real per-lane boxes.
    recognized = _promote_wide_vertical_text_to_layout_lanes(
        image, recognized, _wide_vertical_geometry_candidates(image)
    )
    sfx_recovered: list[dict[str, object]] = []
    for recognized_item in recognized:
        if str(recognized_item.get("orientation") or "") == "horizontal":
            try:
                recognized_item = _recover_clipped_horizontal_sfx(
                    model, image, recognized_item, recognized
                )
            except Exception:
                pass
        sfx_recovered.append(recognized_item)
    recognized = sfx_recovered

    pieces = _split_horizontal_multiline_regions(recognized, image=image)
    refreshed: list[dict[str, object]] = []
    for piece in pieces:
        # Furigana-heavy horizontal TOC rows can shift Accurate Vision range
        # labels by one glyph. Anchor 第N話 on observed main ink before the
        # MangaOCR refresh, then permit one conservative kanji correction from
        # the line-level OCR hypothesis.
        try:
            anchored = _repair_chapter_horizontal_geometry(image, piece)
            refreshed_piece = _refresh_horizontal_line_with_mangaocr(model, image, anchored)
            refreshed_piece = _repair_horizontal_small_kana_consensus(refreshed_piece)
            refreshed_piece = _repair_wide_horizontal_segments_with_mangaocr(
                model, image, refreshed_piece
            )
            refreshed_piece = _repair_short_kanji_pairs_with_mangaocr(
                model, image, refreshed_piece
            )
            refreshed_piece = _repair_interpunct_latin_from_ruby(
                model, image, refreshed_piece
            )
            refreshed_piece = _expand_narrow_horizontal_piece_segments(refreshed_piece)
            refreshed_piece = _repair_chapter_text_from_line_ocr(refreshed_piece)
            refreshed_piece = _repair_chapter_latin_from_detector_consensus(refreshed_piece)
            refreshed_piece = _repair_chapter_from_repeated_mangaocr(
                model, image, refreshed_piece
            )
            refreshed.append(refreshed_piece)
        except Exception:
            refreshed.append(piece)

    refreshed = _repair_page_chapter_stability_consensus(image, refreshed)
    refreshed = _repair_page_chapter_quote_style(model, image, refreshed)
    refreshed = [
        _sync_chapter_core_segments_to_text(piece)
        for piece in refreshed
    ]

    # Horizontal refreshes are allowed to reconstruct/dedupe geometry.  Make
    # the wide-vertical split a final output invariant as well, then remove only
    # impossible donor lanes that are demonstrably duplicated by a sane peer.
    refreshed = _promote_wide_vertical_text_to_layout_lanes(
        image, refreshed, _wide_vertical_geometry_candidates(image)
    )
    refreshed = _suppress_redundant_implausible_wide_vertical_donors(refreshed)
    recalled = _post_recognition_cluster_recall(model, image, refreshed)
    repaired = _repair_post_cluster_truncated_members(image, recalled)
    repaired = _suppress_complete_post_cluster_amalgams(repaired)
    repaired = _trim_wide_donor_adjacent_tall_prefix(repaired)
    repaired = _suppress_supported_raw_same_lane_fragments(repaired)
    repaired = _repair_multiline_caption_from_full_region_suffix(repaired)
    repaired = _suppress_nested_caption_vertical_fragments(repaired)
    repaired = [
        _repair_horizontal_trailing_punctuation_from_page_ink(image, piece)
        for piece in repaired
    ]
    repaired = [
        _relabel_nfkc_equivalent_segment_surfaces(piece)
        for piece in repaired
    ]
    repaired = [
        _relabel_raw_verified_segment_surfaces(piece)
        for piece in repaired
    ]
    repaired = [
        _split_repeated_kana_union_segment_from_page_ink(image, piece)
        for piece in repaired
    ]
    repaired = [
        _recover_short_fullwidth_digit_geometry_from_page_ink(image, piece)
        for piece in repaired
    ]
    repaired = _suppress_short_raw_empty_rectangle_art_noise(repaired)
    return _suppress_nested_expanded_vertical_duplicates(repaired)


def _single(source: Path, output: Path) -> int:
    from manga_ocr import MangaOcr  # type: ignore[import-not-found]

    image = Image.open(source).convert("RGB")
    try:
        model = MangaOcr()
        text = str(model(image) or "").strip()
    finally:
        image.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"text": text}, ensure_ascii=False), encoding="utf-8")
    return 0


def _regions(source: Path, manifest_path: Path, output: Path) -> int:
    from manga_ocr import MangaOcr  # type: ignore[import-not-found]

    image = Image.open(source).convert("RGB")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        regions = payload.get("regions") if isinstance(payload, dict) else []
        normalized = [dict(item) for item in regions if isinstance(item, dict)]
        recognized = _recognize_regions(MangaOcr(), image, normalized)
        recognized = _finalize_worker_output_regions(recognized)
    finally:
        image.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"regions": recognized}, ensure_ascii=False), encoding="utf-8")
    return 0


def _batch(
    manifest_path: Path,
    output_path: Path,
    progress_path: Path,
    stop_path: Path | None = None,
) -> int:
    from manga_ocr import MangaOcr  # type: ignore[import-not-found]

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    archive_path = Path(str(manifest["archive"])).expanduser()
    pages = manifest.get("pages") or []
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("", encoding="utf-8")
    if stop_path is not None and stop_path.exists():
        return 75
    model = MangaOcr()
    failures = 0
    with zipfile.ZipFile(archive_path) as archive, output_path.open("a", encoding="utf-8") as output:
        for done, item in enumerate(pages, start=1):
            if stop_path is not None and stop_path.exists():
                return 75
            page_index = int(item["page_index"])
            name = str(item["name"])
            row: dict[str, object] = {"page_index": page_index}
            image: Image.Image | None = None
            try:
                image = Image.open(io.BytesIO(archive.read(name))).convert("RGB")
                regions = [
                    dict(region)
                    for region in item.get("regions") or []
                    if isinstance(region, dict)
                ]
                recognized = _recognize_regions(model, image, regions)
                recognized = _finalize_worker_output_regions(recognized)
                row["regions"] = recognized
                row["text"] = "\n".join(
                    str(region.get("text") or "") for region in recognized
                ).strip()
            except Exception as exc:
                failures += 1
                row["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                if image is not None:
                    image.close()
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
            output.flush()
            progress_path.write_text(
                json.dumps(
                    {"done": done, "total": len(pages), "page_index": page_index},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            if stop_path is not None and stop_path.exists():
                return 75
    return 1 if failures else 0


def main() -> int:
    try:
        if len(sys.argv) == 5 and sys.argv[1] == "--regions":
            return _regions(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
        if len(sys.argv) in {5, 6} and sys.argv[1] == "--batch":
            return _batch(
                Path(sys.argv[2]),
                Path(sys.argv[3]),
                Path(sys.argv[4]),
                Path(sys.argv[5]) if len(sys.argv) == 6 else None,
            )
        if len(sys.argv) == 3:
            return _single(Path(sys.argv[1]).expanduser(), Path(sys.argv[2]).expanduser())
        print(
            "usage: python -m pudge.manga_ocr_worker INPUT_IMAGE OUTPUT_JSON\n"
            "   or: python -m pudge.manga_ocr_worker --regions INPUT_IMAGE MANIFEST OUTPUT_JSON\n"
            "   or: python -m pudge.manga_ocr_worker --batch MANIFEST RESULTS_JSONL PROGRESS_JSON [STOP_FILE]",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
