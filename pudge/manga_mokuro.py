"""Validate Mokuro 0.2+ text blocks for a *known* CBZ/ZIP volume.

This module neither opens images nor guesses that position N means image N.
A caller must supply the actual archive page names in reader order.
"""

from __future__ import annotations

import math
import re
from pathlib import PurePosixPath
from typing import Any
from .i18n import err

# Per-character geometry split from a detected text line. Not observed glyph
# boxes, hence geometry_status "approximate"; the source must not start with
# "mokuro-" (the reader reserves that prefix for line/block-only hitboxes).
LINE_SPLIT_SOURCE = "ctd-line-proportional-v1"
# Speech bubbles hold short lines; three or more lines averaging this many
# characters are a paragraph of small print.
DENSE_LINE_CHARS = 18
# Box-shaped kanji that a mahjong tile, a grid or a fence turns into.
_PATTERN_GLYPHS = frozenset("日月目耳田皿口曰白四")
_PUNCTUATION = frozenset("．.・…、。！？!?ー〜~♡♥")


def _pattern_text(text: str) -> bool:
    """Text read off a picture of repeated shapes (tiles, grids) rather than letters."""
    core = [character for character in text if not character.isspace() and character not in _PUNCTUATION]
    return len(core) >= 4 and sum(character in _PATTERN_GLYPHS for character in core) / len(core) >= 0.7


def _overlap_share(first: dict[str, Any], second: dict[str, Any]) -> float:
    """Overlap of two regions as a share of the smaller one."""
    width = min(first["x"] + first["width"], second["x"] + second["width"]) - max(first["x"], second["x"])
    height = min(first["y"] + first["height"], second["y"] + second["height"]) - max(first["y"], second["y"])
    if width <= 0 or height <= 0:
        return 0.0
    smaller = min(first["width"] * first["height"], second["width"] * second["height"])
    return (width * height) / smaller if smaller > 0 else 0.0


_KANA_ONLY = re.compile(r"^[\u3041-\u309f\u30a0-\u30ffー・･]+$")
_KANJI = re.compile(r"[\u3400-\u9fff々〆]")
_DOT_RUN = re.compile(r"[．.・･]{2,}")
_LATIN = re.compile(r"[0-9A-Za-z\uff10-\uff19\uff21-\uff3a\uff41-\uff5a]")


class MokuroImportError(ValueError):
    """The sidecar is invalid or does not match the selected manga volume."""


def _page_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MokuroImportError(err("manga.mokuroNoImgPath"))
    name = value.replace("\\", "/").removeprefix("./")
    if name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/")):
        raise MokuroImportError(err("manga.mokuroInvalidImgPath", value=repr(value)))
    return name


def _size(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MokuroImportError(err("manga.mokuroInvalidField", field=field))
    if not math.isfinite(value) or not 0 < value <= 100000 or int(value) != value:
        raise MokuroImportError(err("manga.mokuroInvalidField", field=field))
    return int(value)


def _rect(value: object, width: int, height: int, where: str, *, clamp: bool = False) -> dict[str, float]:
    if not isinstance(value, list) or len(value) != 4:
        raise MokuroImportError(err("manga.mokuroRectShape", where=where))
    if any(isinstance(n, bool) or not isinstance(n, (float, int)) or not math.isfinite(n) for n in value):
        raise MokuroImportError(err("manga.mokuroRectNonFinite", where=where))
    x1, y1, x2, y2 = (float(n) for n in value)
    if clamp and x1 < x2 and y1 < y2:
        # The detector's block boxes may overhang the page edge a little.
        # Keep the on-page part when most of the block is on the page.
        cx1, cy1 = max(0.0, x1), max(0.0, y1)
        cx2, cy2 = min(float(width), x2), min(float(height), y2)
        inside = max(0.0, cx2 - cx1) * max(0.0, cy2 - cy1)
        if inside >= 0.5 * (x2 - x1) * (y2 - y1):
            x1, y1, x2, y2 = cx1, cy1, cx2, cy2
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        raise MokuroImportError(err("manga.mokuroRectOutside", where=where))
    # Mokuro uses image pixel coordinates with the origin at top-left;
    # Pudge's normalized page geometry uses the origin at bottom-left.
    return {
        "x": round(x1 / width, 6),
        "y": round(1.0 - y2 / height, 6),
        "width": round((x2 - x1) / width, 6),
        "height": round((y2 - y1) / height, 6),
    }


def _line_pixels(polygon: object, width: int, height: int) -> tuple[float, float, float, float] | None:
    """Bounding box of a Mokuro line polygon, clamped to the page."""
    if not isinstance(polygon, list) or len(polygon) < 2:
        return None
    try:
        xs = [float(point[0]) for point in polygon]
        ys = [float(point[1]) for point in polygon]
    except (TypeError, ValueError, IndexError):
        return None
    if not all(math.isfinite(value) for value in xs + ys):
        return None
    x1, x2 = max(0.0, min(xs)), min(float(width), max(xs))
    y1, y2 = max(0.0, min(ys)), min(float(height), max(ys))
    if x2 - x1 < 1.0 or y2 - y1 < 1.0:
        return None
    return x1, y1, x2, y2


def _thickness(box: tuple[float, float, float, float], vertical: bool) -> float:
    return (box[2] - box[0]) if vertical else (box[3] - box[1])


def _ruby_lines(
    lines: list[str],
    boxes: list[tuple[float, float, float, float] | None],
    vertical: bool,
    font_size: float,
) -> set[int]:
    """Indices of furigana lines the detector reported as separate text lines.

    A ruby line is kana only, clearly thinner than the block's font, and hugs
    the ruby side of a much thicker line with kanji along most of its length.
    """
    dropped: set[int] = set()
    for index, (text, box) in enumerate(zip(lines, boxes)):
        surface = text.strip()
        if box is None or not surface or not _KANA_ONLY.match(surface):
            continue
        thick = _thickness(box, vertical)
        if font_size > 0 and thick >= 0.62 * font_size:
            continue
        length = (box[3] - box[1]) if vertical else (box[2] - box[0])
        for other_index, (other, other_box) in enumerate(zip(lines, boxes)):
            if (
                other_index == index
                or other_index in dropped
                or other_box is None
                or not _KANJI.search(other)
            ):
                continue
            other_thick = _thickness(other_box, vertical)
            if thick > 0.55 * other_thick:
                continue
            # Ruby sits right of a vertical line and above a horizontal one;
            # the next line of dialogue sits on the other side.
            if vertical:
                if box[0] + box[2] <= other_box[0] + other_box[2]:
                    continue
                gap = max(other_box[0] - box[2], box[0] - other_box[2])
                overlap = min(box[3], other_box[3]) - max(box[1], other_box[1])
            else:
                if box[1] + box[3] >= other_box[1] + other_box[3]:
                    continue
                gap = max(other_box[1] - box[3], box[1] - other_box[3])
                overlap = min(box[2], other_box[2]) - max(box[0], other_box[0])
            if gap < 0.5 * other_thick and overlap > 0.6 * length:
                dropped.add(index)
                break
    return dropped


def _character_weights(text: str) -> list[tuple[str, float]]:
    """Characters of a line with their share of the line length.

    Each printed character takes one cell. MangaOCR writes an ellipsis cell as
    three full-width dots, so a run of dots counts a third of a cell per dot.
    """
    dotted = {index for match in _DOT_RUN.finditer(text) for index in range(match.start(), match.end())}
    return [
        (character, 1 / 3 if index in dotted else 1.0)
        for index, character in enumerate(text)
        if not character.isspace()
    ]


def _character_segments(
    text: str,
    box: tuple[float, float, float, float],
    vertical: bool,
    width: int,
    height: int,
) -> list[dict[str, Any]]:
    weighted = _character_weights(text)
    total = sum(weight for _character, weight in weighted)
    if not weighted or total <= 0:
        return []
    x1, y1, x2, y2 = box
    span = (y2 - y1) if vertical else (x2 - x1)
    segments: list[dict[str, Any]] = []
    position = 0.0
    for character, weight in weighted:
        start = position / total * span
        position += weight
        end = position / total * span
        if vertical:
            left, top, right, bottom = x1, y1 + start, x2, y1 + end
        else:
            left, top, right, bottom = x1 + start, y1, x1 + end, y2
        segments.append(
            {
                "text": character,
                "orientation": "vertical" if vertical else "horizontal",
                "x": round(left / width, 6),
                "y": round(1.0 - bottom / height, 6),
                "width": round((right - left) / width, 6),
                "height": round((bottom - top) / height, 6),
                "source": LINE_SPLIT_SOURCE,
                "geometry_status": "approximate",
            }
        )
    return segments


def _join_lines(lines: list[str], vertical: bool) -> str:
    """Japanese lines join directly; a space only separates Latin words."""
    text = ""
    for line in (line.strip() for line in lines):
        if not line:
            continue
        if text and not vertical and _LATIN.match(text[-1]) and _LATIN.match(line[0]):
            text += " "
        text += line
    return text


def mokuro_page_regions(
    raw_page: dict[str, Any],
    width: int,
    height: int,
    source_name: str,
) -> list[dict[str, Any]]:
    """Pudge regions for one validated Mokuro page (blocks in Mokuro order)."""
    blocks = raw_page.get("blocks")
    if not isinstance(blocks, list) or len(blocks) > 5000:
        raise MokuroImportError(err("manga.mokuroInvalidBlocks", name=source_name))
    regions: list[dict[str, Any]] = []
    for position, block in enumerate(blocks):
        if not isinstance(block, dict) or not isinstance(block.get("lines"), list):
            raise MokuroImportError(err("manga.mokuroInvalidBlock", name=source_name))
        if not isinstance(block.get("vertical"), bool):
            raise MokuroImportError(err("manga.mokuroNoDirection", name=source_name))
        lines = block["lines"]
        if len(lines) > 500 or any(not isinstance(s, str) or len(s) > 4096 for s in lines):
            raise MokuroImportError(err("manga.mokuroInvalidLines", name=source_name))
        vertical = bool(block["vertical"])
        geometry = _rect(block.get("box"), width, height, f"block {position} of {source_name}", clamp=True)
        coords = block.get("lines_coords")
        boxes: list[tuple[float, float, float, float] | None] = (
            [_line_pixels(polygon, width, height) for polygon in coords]
            if isinstance(coords, list) and len(coords) == len(lines)
            else [None] * len(lines)
        )
        font_size = block.get("font_size")
        font = (
            float(font_size)
            if isinstance(font_size, (int, float))
            and not isinstance(font_size, bool)
            and math.isfinite(font_size)
            else 0.0
        )
        ruby = _ruby_lines(lines, boxes, vertical, font)
        kept = [
            (line, box)
            for index, (line, box) in enumerate(zip(lines, boxes))
            if index not in ruby and line.strip()
        ]
        text = _join_lines([line for line, _box in kept], vertical)
        if not text:
            continue
        orientation = "vertical" if vertical else "horizontal"
        region: dict[str, Any] = {
            **geometry,
            "text": text,
            "raw_text": text,
            "orientation": orientation,
            "detector": "mokuro-import",
            "recognizer": "mokuro-import",
            "source": "mokuro-import-v1",
            "geometry_status": "approximate",
            "provenance": {"source": "mokuro", "img_path": source_name},
        }
        if ruby:
            region["provenance"]["ruby_lines"] = [lines[index].strip() for index in sorted(ruby)]
        kept_lengths = [len(line.strip()) for line, _box in kept]
        if len(kept_lengths) >= 3 and sum(kept_lengths) / len(kept_lengths) >= DENSE_LINE_CHARS:
            # Paragraphs of small print (author notes, summaries, credits): MangaOCR
            # often invents fluent text there, so the reader marks it as unreliable.
            region["ocr_quality"] = "dense_text"
        elif _pattern_text(text):
            region["ocr_quality"] = "pattern"
        if kept and all(box is not None for _line, box in kept):
            region["provenance"]["line_boxes"] = [
                {
                    **_rect([box[0], box[1], box[2], box[3]], width, height, "line"),
                    "text": line,
                    "orientation": orientation,
                    "source": "mokuro-line",
                }
                for line, box in kept
                if box is not None
            ]
            # Lines are observed; characters inside a line are spread evenly
            # (Japanese type sits on a one-character grid).
            segments = [
                segment
                for line, box in kept
                if box is not None
                for segment in _character_segments(line.strip(), box, vertical, width, height)
            ]
            if len(segments) >= 1:
                region["segments"] = segments
                region["word_geometry"] = "mapped_segments"
                region["geometry_source"] = LINE_SPLIT_SOURCE
        # The detector sometimes reports one bubble twice as nested boxes.
        duplicate = next(
            (
                index
                for index, other in enumerate(regions)
                if other["text"] == region["text"] and _overlap_share(other, region) >= 0.5
            ),
            None,
        )
        if duplicate is not None:
            if (
                region["width"] * region["height"]
                > regions[duplicate]["width"] * regions[duplicate]["height"]
            ):
                regions[duplicate] = region
            continue
        regions.append(region)
    return regions


def convert_mokuro(
    payload: object,
    archive_pages: list[str],
    *,
    image_sizes: dict[int, tuple[int, int]] | None = None,
) -> dict[int, list[dict[str, Any]]]:
    """Return validated page-index -> Pudge OCR regions, without publishing anything.

    Missing pages are left untouched, not counted as verified-empty. ``img_path``
    must uniquely identify an archive member, or have a unique basename match.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("pages"), list):
        raise MokuroImportError(err("manga.mokuroNoPages"))
    entries = payload["pages"]
    if not entries or len(entries) > 5000:
        raise MokuroImportError(err("manga.mokuroPageCount"))
    exact: dict[str, int] = {}
    basename: dict[str, list[int]] = {}
    for i, archive_name in enumerate(archive_pages):
        name = _page_name(archive_name)
        if name in exact:
            raise MokuroImportError(err("manga.mokuroDuplicatePage", name=name))
        exact[name] = i
        basename.setdefault(PurePosixPath(name).name, []).append(i)

    parsed: dict[int, list[dict[str, Any]]] = {}
    for raw_page in entries:
        if not isinstance(raw_page, dict):
            raise MokuroImportError(err("manga.mokuroInvalidPage"))
        source_name = _page_name(raw_page.get("img_path"))
        index = exact.get(source_name)
        if index is None:
            candidates = basename.get(PurePosixPath(source_name).name, [])
            if len(candidates) != 1:
                raise MokuroImportError(err("manga.mokuroAmbiguousImage", name=source_name))
            index = candidates[0]
        if index in parsed:
            raise MokuroImportError(err("manga.mokuroRepeatedPage", name=source_name))
        width = _size(raw_page.get("img_width"), "img_width")
        height = _size(raw_page.get("img_height"), "img_height")
        if image_sizes is not None and image_sizes.get(index) != (width, height):
            raise MokuroImportError(err("manga.mokuroSizeMismatch", name=source_name))
        parsed[index] = mokuro_page_regions(raw_page, width, height, source_name)
    return parsed
