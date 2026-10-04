from __future__ import annotations

import base64
import difflib
import hashlib
import io
import json
import math
import os
import signal
import mimetypes
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import zipfile
from pathlib import Path
from typing import Any, Callable

import httpx
from PIL import Image, ImageEnhance, ImageOps

from .cover_assets import CoverRef
from .database import Database
from .manga_mokuro import MokuroImportError, convert_mokuro
from .manga_ocr_artifact import (
    artifact_page,
    build_artifact,
    normalize_page,
    read_artifact,
    write_artifact,
)
from .runtime import python_executable
from .work_scheduler import WorkPriority


_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
_REGION_CACHE_KEY = "pudge-manga-regions-v96p27-orphan-vertical-ink"
_READER_PREFS_KEY = "manga_reader_preferences:v1"
_MANUAL_OCR_CORRECTIONS_PREFIX = "manga_ocr_manual_corrections:v1:"
_MANUAL_OCR_ORDER_PREFIX = "manga_ocr_manual_order:v1:"
_MANUAL_OCR_GEOMETRY_PREFIX = "manga_ocr_manual_geometry:v1:"
_MANUAL_OCR_SUPPRESSIONS_PREFIX = "manga_ocr_manual_suppressions:v1:"
_MANUAL_OCR_ADDITIONS_PREFIX = "manga_ocr_manual_additions:v1:"
_READER_PREF_DEFAULTS: dict[str, Any] = {
    "statusHighlight": False,
    "statusOpacity": 35,
    "statusNew": True,
    "statusLearning": True,
    "statusKnown": True,
    "statusDue": True,
    "statusNewColor": "#ffdc46",
    "statusLearningColor": "#4aa9ff",
    "statusKnownColor": "#45cf80",
    "statusDueColor": "#ef8d3d",
}



def _stop_ocr_process_tree(
    process: subprocess.Popen[str], stop_path: Path, *, grace_seconds: float = 5.0,
) -> None:
    """Stop a batch coordinator and every model it spawned before deleting its stop file.

    OCR subprocesses are started in their own POSIX session, so their group
    contains only this job. Killing the coordinator PID alone may orphan
    model processes that are still processing a page.
    """
    if process.poll() is not None:
        # The coordinator can exit before a child has reaped itself; the job
        # still owns its dedicated POSIX process group at this point. Do not
        # rely on coordinator returncode as proof that every model has stopped.
        group_id = getattr(process, "pid", None) if os.name == "posix" else None
        if group_id is not None:
            try:
                os.killpg(group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return
    try:
        stop_path.write_text("stop\n", encoding="utf-8")
    except OSError:
        pass
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    pgid = getattr(process, "pid", None) if os.name == "posix" else None
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:
        process.terminate()
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        if pgid is None:
            process.kill()
            process.wait(timeout=grace_seconds)
            return
        # The coordinator may still be handling SIGTERM. Kill the entire
        # session, including workers currently stuck inside model inference.
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=grace_seconds)
        return
    if pgid is not None:
        # SIGTERM may have stopped the coordinator before its children exited.
        # Only this OCR job belongs to its dedicated process group.
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _validated_batch_ocr_rows(
    lines: list[str], expected_pages: set[int],
) -> tuple[dict[int, dict[str, Any]], set[int], list[str]]:
    """Reject corrupt, duplicate or foreign worker rows before cache publication.

    A malformed row must never turn an unprocessed page into a verified-empty
    page; duplicate rows invalidate the affected page instead of letting the
    last write win. Valid rows for other pages remain usable after a crash.
    """
    rows: dict[int, dict[str, Any]] = {}
    invalid: set[int] = set()
    errors: list[str] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError("not an object")
            page_index = item["page_index"]
            if type(page_index) is not int:
                raise ValueError("non-integer page index")
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            errors.append(f"invalid_ocr_worker_row:line_{line_number}")
            continue
        if page_index not in expected_pages:
            errors.append(f"unexpected_ocr_worker_page:{page_index}")
            continue
        if page_index in rows or page_index in invalid:
            rows.pop(page_index, None)
            invalid.add(page_index)
            errors.append(f"duplicate_ocr_worker_page:{page_index}")
            continue
        if not str(item.get("error") or "").strip() and (
            not isinstance(item.get("regions"), list)
            or any(not isinstance(region, dict) for region in item["regions"])
        ):
            invalid.add(page_index)
            errors.append(f"invalid_ocr_worker_regions:{page_index}")
            continue
        rows[page_index] = item
    return rows, invalid, errors


def _natural_key(value: str) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


def _strip_manga_release_metadata(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    # pudge-v0.7.23-nested-manga-import-v1
    # Common raw-manga mirrors prefix every folder with their site name.
    text = re.sub(r"(?i)^Mang-Zip\.info(?:[_\s-]+)?", "", text).strip()
    # Square-bracket groups at filename edges are release-group metadata in
    # manga archives, e.g. "[Group] Title 2" or "Title 2 [aKraa]". Strip
    # repeatedly so they cannot hide a bare trailing volume number.
    text = re.sub(r"^\s*(?:\[[^\]\r\n]{1,80}\]\s*)+", "", text)
    text = re.sub(r"(?:\s+\[[^\]\r\n]{1,80}\])+$", "", text)
    # Parentheses can be part of a real title, so only strip well-known
    # release descriptors there.
    text = re.sub(r"\s*\((?:digital|official|scan|raw|retail|web|color(?:ed)?|complete)[^)]{0,60}\)\s*$", "", text, flags=re.I)
    return text.strip()


def _manga_volume(value: str) -> int | None:
    text = _strip_manga_release_metadata(value)
    patterns = (
        r"(?i)(?:^|[\s._\-\[(])(?:vol(?:ume)?|v)\s*[._ -]*0*(\d{1,3})(?:\.\d+)?(?:$|[\s._\-\])])",
        r"第\s*0*(\d{1,3})(?:\.\d+)?\s*巻",
        r"(?:^|[\s._\-])0*(\d{1,3})(?:\.\d+)?\s*巻(?:$|[\s._\-])",
        # Archive names frequently use just "Series - 108". Requiring a
        # separator keeps numeric series titles such as "86" intact.
        r"(?:[\s._\-])0*(\d{1,3})(?:\.\d+)?\s*$",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        number = int(match.group(1))
        if 0 < number <= 300:
            return number
    return None


def _manga_series_title(value: str) -> str:
    text = _strip_manga_release_metadata(value)
    text = re.sub(r"(?i)(?:^|[\s._\-\[(])(?:vol(?:ume)?|v)\s*[._ -]*0*\d{1,3}(?:\.\d+)?(?:$|[\s._\-\])])", " ", text)
    text = re.sub(r"第\s*0*\d{1,3}(?:\.\d+)?\s*巻", " ", text)
    text = re.sub(r"(?:^|[\s._\-])0*\d{1,3}(?:\.\d+)?\s*巻(?:$|[\s._\-])", " ", text)
    # Bare trailing volume numbers are common in CBZ names. Do not strip a
    # title that consists only of that number (e.g. "86").
    if re.search(r"[^\d\s._-]", text):
        text = re.sub(r"[\s._\-]+0*\d{1,3}(?:\.\d+)?\s*$", "", text)
    text = re.sub(r"[\s._-]+$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _manga_series_key(value: str) -> str:
    text = _manga_series_title(value)
    text = re.sub(r"[\s\[\](){}._・･:：!！?？'\"“”‘’—–-]+", "", text)
    return text.casefold()


def _boxes_near(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_x1 = float(left["x"])
    left_y1 = float(left["y"])
    left_x2 = left_x1 + float(left["width"])
    left_y2 = left_y1 + float(left["height"])
    right_x1 = float(right["x"])
    right_y1 = float(right["y"])
    right_x2 = right_x1 + float(right["width"])
    right_y2 = right_y1 + float(right["height"])
    horizontal_gap = max(0.0, max(left_x1, right_x1) - min(left_x2, right_x2))
    vertical_gap = max(0.0, max(left_y1, right_y1) - min(left_y2, right_y2))
    vertical_overlap = max(0.0, min(left_y2, right_y2) - max(left_y1, right_y1))
    horizontal_overlap = max(0.0, min(left_x2, right_x2) - max(left_x1, right_x1))
    # Vertical Japanese lines belonging to one bubble sit next to each other;
    # horizontal lines usually stack. Keep the threshold conservative so two
    # neighbouring bubbles do not become one giant OCR crop.
    vertical_lines = float(left["height"]) > float(left["width"]) * 1.05 or float(
        right["height"]
    ) > float(right["width"]) * 1.05
    if vertical_lines:
        # Vision often returns a vertical bubble as several narrow columns, or
        # splits one column into two stacked observations. The old 2.2% page
        # gap only joined almost-touching glyph boxes and left most Japanese
        # bubbles as tiny, hard-to-hit strips.
        neighbouring_columns = (
            horizontal_gap <= 0.065
            and vertical_overlap
            >= min(float(left["height"]), float(right["height"])) * 0.12
        )
        split_column = (
            vertical_gap <= 0.045
            and horizontal_overlap
            >= min(float(left["width"]), float(right["width"])) * 0.18
        )
        return neighbouring_columns or split_column
    # A detector fallback can return one almost-square box per glyph.  Those
    # boxes are still a vertical line when they stack on the same x coordinate.
    vertical_glyph_stack = (
        vertical_gap <= 0.032
        and horizontal_overlap
        >= min(float(left["width"]), float(right["width"])) * 0.35
    )
    horizontal_line = (
        horizontal_gap <= 0.020
        and vertical_overlap
        >= min(float(left["height"]), float(right["height"])) * 0.35
    )
    return vertical_glyph_stack or horizontal_line


def _box_overlap(left: dict[str, Any], right: dict[str, Any]) -> float:
    """Intersection divided by the smaller box, useful for detector dedupe."""

    left_x1, left_y1 = float(left["x"]), float(left["y"])
    right_x1, right_y1 = float(right["x"]), float(right["y"])
    left_x2 = left_x1 + float(left["width"])
    left_y2 = left_y1 + float(left["height"])
    right_x2 = right_x1 + float(right["width"])
    right_y2 = right_y1 + float(right["height"])
    width = max(0.0, min(left_x2, right_x2) - max(left_x1, right_x1))
    height = max(0.0, min(left_y2, right_y2) - max(left_y1, right_y1))
    intersection = width * height
    smaller = min(
        float(left["width"]) * float(left["height"]),
        float(right["width"]) * float(right["height"]),
    )
    return intersection / max(smaller, 1e-9)


def _looks_like_vertical_japanese_region(region: dict[str, Any]) -> bool:
    """Recover multi-column vertical bubbles merged into a wide rectangle.

    Vision frequently reports each vertical column separately. After merging,
    the union can be wider than it is tall, so aspect ratio alone incorrectly
    labels the final selectable overlay as horizontal.
    """

    text = re.sub(r"\s+", "", str(region.get("text") or region.get("raw_text") or ""))
    japanese = re.findall(r"[\u3040-\u30ff\u3400-\u9fff々〆ヶ]", text)
    width = max(0.0, float(region.get("width") or 0.0))
    height = max(0.0, float(region.get("height") or 0.0))
    detector = str(region.get("detector") or "")
    japanese_ratio = len(japanese) / max(1, len(text))
    # VNDetectTextRectangles gives geometry but only placeholder confidence. In
    # vertical manga it commonly reports a thin horizontal slice for each
    # vertical line. Once MangaOCR fills that crop with Japanese text, treating
    # the slice as horizontal lays every Jiten token left-to-right over a
    # top-to-bottom speech bubble. Prefer vertical for these low-confidence
    # rectangle-derived Japanese regions; explicit high-confidence recognition
    # and true horizontal captions keep their normal orientation.
    confidence = float(region.get("confidence") or 0.0)
    if (
        "vision-rectangles" in str(region.get("detector") or "")
        and confidence <= 0.35
        and len(japanese) >= 2
        and japanese_ratio >= 0.55
    ):
        return True
    if len(japanese) < 6 or japanese_ratio < 0.65:
        return False
    # Long horizontal captions are normally one shallow line. A speech bubble
    # containing several vertical columns has meaningful height even when the
    # merged union is wider than tall. Rectangle geometry is strong evidence,
    # but the fallback also covers recognition-only detections.
    classic_geometry = (
        height >= 0.065
        and width <= 0.34
        and height >= width * 0.34
    )
    wide_multicolumn_geometry = (
        height >= 0.08
        and width <= 0.48
        and height >= width * 0.20
        and "vision-rectangles" in detector
    )
    return (classic_geometry or wide_multicolumn_geometry) and (
        "vision-rectangles" in detector or height >= width * 0.45
    )


def _normalize_region_orientation(region: dict[str, Any]) -> dict[str, Any]:
    item = dict(region)
    if str(item.get("orientation") or "") != "vertical" and _looks_like_vertical_japanese_region(item):
        item["orientation"] = "vertical"
        item["orientation_reason"] = "japanese-multicolumn-geometry"
    return item


def _finalize_recognized_regions(regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply output invariants after the OCR worker has returned.

    Worker-side cluster cleanup runs before the result crosses the subprocess
    boundary.  Some recalled clusters become fully self-describing only in the
    final worker payload, so run the deterministic post-cluster amalgam cleanup
    once more on exactly the list that will be cached/serialized by MangaService.
    This pass performs no OCR and never needs manga-ocr in the app process.
    """
    normalized = [
        _normalize_region_orientation(item)
        for item in regions
        if str(item.get("text") or "").strip()
    ]
    if not normalized:
        return normalized

    # Import lazily: manga_ocr_worker itself only imports MangaOCR inside its CLI
    # entry points, so this keeps the GUI process free of model initialization.
    from .manga_ocr_worker import (
        _suppress_complete_post_cluster_amalgams,
        _suppress_clipped_bottom_vertical_fragment_art,
        _suppress_detector_giant_oneglyph_art,
        _suppress_empty_horizontal_rectangle_mangaocr_art,
        _suppress_empty_multicolumn_sentence_art,
        _suppress_empty_vertical_rectangle_mangaocr_art,
        _suppress_margin_page_number_regions,
        _suppress_page_edge_synthetic_ink_grid_art,
        _suppress_short_raw_empty_rectangle_art_noise,
        _suppress_tiny_empty_horizontal_oneglyph_art,
    )

    normalized = _suppress_complete_post_cluster_amalgams(normalized)
    normalized = _suppress_short_raw_empty_rectangle_art_noise(normalized)
    normalized = _suppress_tiny_empty_horizontal_oneglyph_art(normalized)
    normalized = _suppress_margin_page_number_regions(normalized)
    normalized = _suppress_detector_giant_oneglyph_art(normalized)
    normalized = _suppress_empty_vertical_rectangle_mangaocr_art(normalized)
    normalized = _suppress_page_edge_synthetic_ink_grid_art(normalized)
    normalized = _suppress_empty_multicolumn_sentence_art(normalized)
    normalized = _suppress_clipped_bottom_vertical_fragment_art(normalized)
    return _suppress_empty_horizontal_rectangle_mangaocr_art(normalized)




def _latin_only_title_surface(value: object) -> str:
    """Return a compact Latin-only title candidate, or empty for mixed text."""
    surface = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not surface:
        return ""
    letters = "".join(character for character in surface if character.isascii() and character.isalpha())
    if len(letters) < 8:
        return ""
    residue = re.sub(r"[A-Za-z\s\-—–ー―_.:·・]+", "", surface)
    if residue:
        return ""
    return letters.upper()


def _latin_title_candidates_from_region(value: object) -> list[str]:
    """Extract long Latin title phrases from an arbitrary OCR region."""
    surface = unicodedata.normalize("NFKC", str(value or ""))
    output: list[str] = []
    for match in re.finditer(r"[A-Za-z]{3,}(?:[\s\-—–ー―]+[A-Za-z]{2,})*", surface):
        compact = "".join(character for character in match.group(0) if character.isascii() and character.isalpha()).upper()
        if len(compact) >= 8:
            output.append(compact)
    return output


def _union_segment_geometry(items: list[dict[str, Any]]) -> dict[str, float]:
    left = min(float(item.get("x") or 0.0) for item in items)
    bottom = min(float(item.get("y") or 0.0) for item in items)
    right = max(float(item.get("x") or 0.0) + float(item.get("width") or 0.0) for item in items)
    top = max(float(item.get("y") or 0.0) + float(item.get("height") or 0.0) for item in items)
    return {
        "x": round(left, 6),
        "y": round(bottom, 6),
        "width": round(max(0.0, right - left), 6),
        "height": round(max(0.0, top - bottom), 6),
    }


def _remap_latin_consensus_segments(
    region: dict[str, Any],
    current: str,
    target: str,
) -> list[dict[str, Any]] | None:
    segments = [dict(item) for item in region.get("segments") or [] if isinstance(item, dict)]
    if not segments:
        return None
    stream = "".join(
        unicodedata.normalize("NFKC", str(item.get("text") or ""))
        for item in segments
    )
    stream = "".join(character for character in stream if character.isascii() and character.isalpha()).upper()
    if stream != current or len(segments) != len(current):
        return None

    mapped: list[dict[str, Any]] = []
    matcher = difflib.SequenceMatcher(a=current, b=target, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        source = segments[i1:i2]
        target_part = target[j1:j2]
        if tag == "equal":
            for segment, character in zip(source, target_part):
                updated = dict(segment)
                updated["text"] = character
                mapped.append(updated)
            continue
        if tag == "replace":
            if len(source) == len(target_part):
                for segment, character in zip(source, target_part):
                    updated = dict(segment)
                    updated["text"] = character
                    updated["recognition_correction"] = "book-latin-consensus-v1"
                    mapped.append(updated)
                continue
            if target_part and len(target_part) == 1 and source:
                merged = dict(source[0])
                merged.update(_union_segment_geometry(source))
                merged["text"] = target_part
                merged["source"] = "book-latin-consensus-v1"
                merged["recognition_correction"] = "book-latin-consensus-v1"
                mapped.append(merged)
                continue
            return None
        if tag == "delete":
            continue
        # Inserting a new glyph without observed geometry would fabricate a
        # hitbox, so reject the whole repair.
        return None
    return mapped if "".join(str(item.get("text") or "") for item in mapped) == target else None


def _repair_repeated_latin_page_titles(
    regions: list[dict[str, Any]],
    peer_pages: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Repair one noisy Latin-only title from repeated same-book page evidence.

    A candidate must be independently present on at least two peer pages.  This
    intentionally does not use a title dictionary and refuses repairs that
    would require inventing glyph geometry.
    """
    support: dict[str, set[int]] = {}
    for page_number, peer_regions in enumerate(peer_pages):
        seen: set[str] = set()
        for peer in peer_regions:
            for candidate in _latin_title_candidates_from_region(peer.get("text")):
                seen.add(candidate)
        for candidate in seen:
            support.setdefault(candidate, set()).add(page_number)

    repeated = {
        candidate: len(pages)
        for candidate, pages in support.items()
        if len(pages) >= 2
    }
    if not repeated:
        return [dict(region) for region in regions]

    output: list[dict[str, Any]] = []
    for original in regions:
        region = dict(original)
        if str(region.get("orientation") or "") != "horizontal":
            output.append(region)
            continue
        current = _latin_only_title_surface(region.get("text"))
        if not current:
            output.append(region)
            continue

        scored: list[tuple[float, int, str]] = []
        for candidate, count in repeated.items():
            if candidate == current or abs(len(candidate) - len(current)) > 2:
                continue
            similarity = difflib.SequenceMatcher(a=current, b=candidate, autojunk=False).ratio()
            minimum_similarity = 0.78 if count >= 3 else 0.84
            if similarity >= minimum_similarity:
                scored.append((similarity, count, candidate))
        if not scored:
            output.append(region)
            continue
        scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
        best_similarity, best_count, target = scored[0]
        if len(scored) > 1 and best_similarity - scored[1][0] < 0.06:
            output.append(region)
            continue

        mapped = _remap_latin_consensus_segments(region, current, target)
        if mapped is None:
            output.append(region)
            continue
        region["text"] = target
        region["raw_text"] = target
        region["segments"] = mapped
        region["book_latin_consensus"] = {
            "from": current,
            "to": target,
            "peer_pages": best_count,
            "similarity": round(best_similarity, 4),
            "source": "same-book-repeated-latin-v1",
        }
        output.append(region)
    return output



def _normalized_vision_rect(rect: Any) -> tuple[float, float, float, float] | None:
    try:
        width = max(0.0, min(1.0, float(rect.size.width)))
        height = max(0.0, min(1.0, float(rect.size.height)))
        x = max(0.0, min(1.0 - width, float(rect.origin.x)))
        y = max(0.0, min(1.0 - height, float(rect.origin.y)))
    except (AttributeError, TypeError, ValueError):
        return None
    return (x, y, width, height) if width > 0.001 and height > 0.001 else None


def _vision_character_box_rows(observation: Any) -> list[tuple[float, float, float, float]]:
    getter = getattr(observation, "characterBoxes", None)
    if not callable(getter):
        return []
    try:
        characters = getter() or []
    except Exception:
        return []
    rows: list[tuple[float, float, float, float]] = []
    for character in characters:
        try:
            rect = character.boundingBox()
        except (AttributeError, TypeError):
            continue
        row = _normalized_vision_rect(rect)
        if row is not None:
            rows.append(row)
    return rows


def _vision_observation_bounds(observation: Any) -> tuple[float, float, float, float, str]:
    base = _normalized_vision_rect(observation.boundingBox())
    if base is None:
        return 0.0, 0.0, 0.0, 0.0, "invalid"
    character_rows = _vision_character_box_rows(observation)
    if len(character_rows) < 2:
        return *base, "observation-bounds"
    x1 = min(row[0] for row in character_rows)
    y1 = min(row[1] for row in character_rows)
    x2 = max(row[0] + row[2] for row in character_rows)
    y2 = max(row[1] + row[3] for row in character_rows)
    union = (x1, y1, min(1.0 - x1, x2 - x1), min(1.0 - y1, y2 - y1))
    if union[2] > base[2] * 1.18 or union[3] > base[3] * 1.18:
        return *union, "character-box-union"
    return *base, "observation-bounds"


# pudge-v0.7.27-manga-vision-character-geometry-v1
# pudge-v0.7.27-manga-horizontal-main-segments-v14
# pudge-v0.7.27-manga-accurate-range-geometry-v15
# pudge-v0.7.27-manga-horizontal-alignment-v16
# pudge-manga-recovery-phase2-layout-v1
# pudge-manga-recovery-phase2.1-dual-pass-v2
# pudge-manga-recovery-phase2.2-line-promotion-v11
# pudge-manga-recovery-phase2.3-precision-v15
# pudge-manga-recovery-phase2.4-geometry-v17
# pudge-manga-recovery-phase2.5-layout-token-geometry-v19
def _vision_recognized_text_segments(recognized_text: Any, text: str) -> list[dict[str, Any]]:
    """Return exact VNRecognizedText character boxes when Vision exposes them."""
    if recognized_text is None or not str(text or ""):
        return []
    getter = getattr(recognized_text, "boundingBoxForRange_error_", None)
    if not callable(getter):
        return []
    try:
        from Foundation import NSMakeRange  # type: ignore
    except ImportError:
        NSMakeRange = None  # type: ignore[assignment]

    segments: list[dict[str, Any]] = []
    utf16_offset = 0
    for character in str(text):
        utf16_length = max(1, len(character.encode("utf-16-le")) // 2)
        if character.isspace():
            utf16_offset += utf16_length
            continue
        ns_range = (
            NSMakeRange(utf16_offset, utf16_length)
            if NSMakeRange is not None
            else (utf16_offset, utf16_length)
        )
        try:
            result = getter(ns_range, None)
        except Exception:
            utf16_offset += utf16_length
            continue
        observation = result[0] if isinstance(result, tuple) else result
        if observation is None:
            utf16_offset += utf16_length
            continue
        try:
            rect = observation.boundingBox()
        except (AttributeError, TypeError):
            rect = observation
        row = _normalized_vision_rect(rect)
        if row is not None:
            segments.append({
                "text": character,
                "orientation": "horizontal",
                "x": round(row[0], 6),
                "y": round(row[1], 6),
                "width": round(row[2], 6),
                "height": round(row[3], 6),
                "source": "vision-accurate-range-v2",
            })
        utf16_offset += utf16_length
    return segments

def _vision_observation_segments(observation: Any) -> list[dict[str, Any]]:
    x, y, width, height, source = _vision_observation_bounds(observation)
    if source != "character-box-union":
        return []
    rows = _vision_character_box_rows(observation)
    vertical = height > width * 1.05
    rows.sort(
        key=(lambda row: (-row[0], -row[1]))
        if vertical
        else (lambda row: (-row[1], row[0]))
    )
    return [
        {
            "text": "",
            "orientation": "vertical" if vertical else "horizontal",
            "x": round(row[0], 6),
            "y": round(row[1], 6),
            "width": round(row[2], 6),
            "height": round(row[3], 6),
            "source": "vision-character-box",
        }
        for row in rows
    ]


def _merge_text_regions(regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[list[dict[str, Any]]] = []
    for region in regions:
        matches = [index for index, group in enumerate(groups) if any(_boxes_near(region, item) for item in group)]
        if not matches:
            groups.append([region])
            continue
        target = groups[matches[0]]
        target.append(region)
        for index in reversed(matches[1:]):
            target.extend(groups.pop(index))

    merged: list[dict[str, Any]] = []
    for group in groups:
        x1 = min(float(item["x"]) for item in group)
        y1 = min(float(item["y"]) for item in group)
        x2 = max(float(item["x"]) + float(item["width"]) for item in group)
        y2 = max(float(item["y"]) + float(item["height"]) for item in group)
        vertical = (
            sum(
                float(item["height"]) > float(item["width"]) * 1.05
                for item in group
            )
            >= len(group) / 2
        ) or (y2 - y1) > (x2 - x1) * 1.15
        ordered = sorted(
            group,
            key=(lambda item: (-float(item["x"]), -float(item["y"])))
            if vertical
            else (lambda item: (-float(item["y"]), float(item["x"]))),
        )
        text = "".join(str(item.get("text") or "") for item in ordered) if vertical else " ".join(
            str(item.get("text") or "") for item in ordered
        )
        merged.append(
            {
                "text": text.strip(),
                "orientation": "vertical" if vertical else "horizontal",
                "x": round(max(0.0, x1 - 0.006), 6),
                "y": round(max(0.0, y1 - 0.006), 6),
                "width": round(min(1.0 - max(0.0, x1 - 0.006), x2 - x1 + 0.012), 6),
                "height": round(min(1.0 - max(0.0, y1 - 0.006), y2 - y1 + 0.012), 6),
                "confidence": round(max(float(item.get("confidence") or 0.0) for item in group), 4),
                "detector": "+".join(sorted({str(item.get("detector") or "vision") for item in group})),
                "recognizer": "+".join(sorted({str(item.get("recognizer") or "apple-vision") for item in group})),
                "raw_text": text.strip(),
                "segments": [
                    {
                        "text": str(segment.get("text") or "").strip(),
                        "orientation": str(segment.get("orientation") or ("vertical" if float(segment.get("height") or 0.0) > float(segment.get("width") or 0.0) * 1.05 else "horizontal")),
                        "x": round(float(segment.get("x") or 0.0), 6),
                        "y": round(float(segment.get("y") or 0.0), 6),
                        "width": round(float(segment.get("width") or 0.0), 6),
                        "height": round(float(segment.get("height") or 0.0), 6),
                        "source": str(segment.get("source") or ""),
                    }
                    for item in ordered
                    for segment in (
                        item.get("segments")
                        if isinstance(item.get("segments"), list) and item.get("segments")
                        else [item]
                    )
                ],
            }
        )
    vertical_page = bool(merged) and sum(
        item.get("orientation") == "vertical" for item in merged
    ) >= len(merged) / 2
    merged.sort(
        key=(lambda item: (-float(item["x"]), -float(item["y"])))
        if vertical_page
        else (lambda item: (-float(item["y"]), float(item["x"]))),
    )
    return merged


class MangaOcrDeferred(RuntimeError):
    pass


class MangaOcrWorkerError(RuntimeError):
    pass


class MangaService:
    """CBZ reader backed exclusively by selectable region OCR artifacts."""

    def __init__(
        self,
        database: Database,
        *,
        cache_dir: Path | None = None,
        python: str | None = None,
        work_scheduler: Any | None = None,
    ) -> None:
        self.db = database
        self.cache_dir = Path(cache_dir or Path.home() / "Library" / "Caches" / "pudge")
        self.python = str(python or python_executable())
        self.work_scheduler = work_scheduler
        self._ocr_lock = threading.Lock()
        self._ocr_publish_locks_guard = threading.Lock()
        self._ocr_publish_locks: dict[int, threading.RLock] = {}
        self._ocr_available_cache: tuple[float, bool] | None = None
        self._cover_cache_lock = threading.Lock()
        self._cover_cache_inflight: set[str] = set()
        self._purge_legacy_ocr_cache()

    def _purge_legacy_ocr_cache(self) -> None:
        """Remove obsolete whole-page OCR rows from pre-region builds."""

        with self.db.connect() as conn:
            conn.execute("DELETE FROM manga_ocr_cache WHERE region_key='full'")

    def invalidate_ocr_availability(self) -> None:
        self._ocr_available_cache = None

    @staticmethod
    def _manual_corrections_state_key(book_id: int) -> str:
        return f"{_MANUAL_OCR_CORRECTIONS_PREFIX}{int(book_id)}"

    @staticmethod
    def _manual_order_state_key(book_id: int) -> str:
        return f"{_MANUAL_OCR_ORDER_PREFIX}{int(book_id)}"

    @staticmethod
    def _manual_geometry_state_key(book_id: int) -> str:
        return f"{_MANUAL_OCR_GEOMETRY_PREFIX}{int(book_id)}"

    @staticmethod
    def _manual_suppressions_state_key(book_id: int) -> str:
        return f"{_MANUAL_OCR_SUPPRESSIONS_PREFIX}{int(book_id)}"

    @staticmethod
    def _manual_additions_state_key(book_id: int) -> str:
        return f"{_MANUAL_OCR_ADDITIONS_PREFIX}{int(book_id)}"

    @staticmethod
    def _manual_region_anchor(region: dict[str, Any]) -> dict[str, Any]:
        def number(name: str) -> float:
            try:
                value = float(region.get(name) or 0.0)
            except (TypeError, ValueError):
                value = 0.0
            return round(max(0.0, min(1.0, value)), 6)

        return {
            "x": number("x"),
            "y": number("y"),
            "width": number("width"),
            "height": number("height"),
            "orientation": str(region.get("orientation") or ""),
        }

    @classmethod
    def _manual_stable_region_anchor(cls, region: dict[str, Any]) -> dict[str, Any]:
        manual_geometry = region.get("manual_geometry")
        if isinstance(manual_geometry, dict):
            original_anchor = manual_geometry.get("original_anchor")
            if isinstance(original_anchor, dict):
                anchored = dict(region)
                anchored.update(original_anchor)
                return cls._manual_region_anchor(anchored)
        return cls._manual_region_anchor(region)

    @staticmethod
    def _manual_anchor_iou(left: dict[str, Any], right: dict[str, Any]) -> float:
        try:
            lx1, ly1 = float(left["x"]), float(left["y"])
            lx2, ly2 = lx1 + float(left["width"]), ly1 + float(left["height"])
            rx1, ry1 = float(right["x"]), float(right["y"])
            rx2, ry2 = rx1 + float(right["width"]), ry1 + float(right["height"])
        except (KeyError, TypeError, ValueError):
            return 0.0
        intersection = max(0.0, min(lx2, rx2) - max(lx1, rx1)) * max(
            0.0, min(ly2, ry2) - max(ly1, ry1)
        )
        union = max(0.0, (lx2 - lx1) * (ly2 - ly1)) + max(
            0.0, (rx2 - rx1) * (ry2 - ry1)
        ) - intersection
        return intersection / union if union > 0 else 0.0

    def _load_manual_corrections(self, book_id: int) -> list[dict[str, Any]]:
        raw = self.db.get_state(self._manual_corrections_state_key(book_id), "").strip()
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        return [dict(item) for item in payload if isinstance(item, dict)]

    def _save_manual_corrections(self, book_id: int, rows: list[dict[str, Any]]) -> None:
        key = self._manual_corrections_state_key(book_id)
        if not rows:
            self.db.delete_state(key)
            return
        self.db.set_state(
            key,
            json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
        )

    def _load_manual_orders(self, book_id: int) -> list[dict[str, Any]]:
        raw = self.db.get_state(self._manual_order_state_key(book_id), "").strip()
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        return [dict(item) for item in payload if isinstance(item, dict)]

    def _save_manual_orders(self, book_id: int, rows: list[dict[str, Any]]) -> None:
        key = self._manual_order_state_key(book_id)
        if not rows:
            self.db.delete_state(key)
            return
        self.db.set_state(
            key,
            json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
        )

    def _load_manual_geometries(self, book_id: int) -> list[dict[str, Any]]:
        raw = self.db.get_state(self._manual_geometry_state_key(book_id), "").strip()
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        return [dict(item) for item in payload if isinstance(item, dict)]

    def _save_manual_geometries(self, book_id: int, rows: list[dict[str, Any]]) -> None:
        key = self._manual_geometry_state_key(book_id)
        if not rows:
            self.db.delete_state(key)
            return
        self.db.set_state(
            key,
            json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
        )

    def _load_manual_suppressions(self, book_id: int) -> list[dict[str, Any]]:
        raw = self.db.get_state(self._manual_suppressions_state_key(book_id), "").strip()
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        return [dict(item) for item in payload if isinstance(item, dict)]

    def _save_manual_suppressions(self, book_id: int, rows: list[dict[str, Any]]) -> None:
        key = self._manual_suppressions_state_key(book_id)
        if not rows:
            self.db.delete_state(key)
            return
        self.db.set_state(
            key,
            json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
        )

    def _load_manual_additions(self, book_id: int) -> list[dict[str, Any]]:
        raw = self.db.get_state(self._manual_additions_state_key(book_id), "").strip()
        try:
            payload = json.loads(raw) if raw else []
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return [dict(item) for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []

    def _save_manual_additions(self, book_id: int, rows: list[dict[str, Any]]) -> None:
        if not rows:
            self.db.delete_state(self._manual_additions_state_key(book_id))
        else:
            self.db.set_state(
                self._manual_additions_state_key(book_id),
                json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
            )

    @staticmethod
    def _validated_manual_geometry(geometry: dict[str, Any]) -> dict[str, float]:
        try:
            x = float(geometry.get("x"))
            y = float(geometry.get("y"))
            width = float(geometry.get("width"))
            height = float(geometry.get("height"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Manual OCR geometry must contain x/y/width/height") from exc
        values = (x, y, width, height)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Manual OCR geometry must be finite")
        if x < 0 or y < 0 or width < 0.002 or height < 0.002:
            raise ValueError("Manual OCR geometry is outside the page")
        if x + width > 1.000001 or y + height > 1.000001:
            raise ValueError("Manual OCR geometry is outside the page")
        return {
            "x": round(x, 6),
            "y": round(y, 6),
            "width": round(width, 6),
            "height": round(height, 6),
        }

    @staticmethod
    def _transform_manual_box(
        box: dict[str, Any],
        source: dict[str, Any],
        target: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            sx, sy = float(source["x"]), float(source["y"])
            sw, sh = float(source["width"]), float(source["height"])
            bx, by = float(box.get("x")), float(box.get("y"))
            bw, bh = float(box.get("width")), float(box.get("height"))
        except (KeyError, TypeError, ValueError):
            return dict(box)
        if sw <= 0 or sh <= 0 or bw <= 0 or bh <= 0:
            return dict(box)
        tx, ty = float(target["x"]), float(target["y"])
        tw, th = float(target["width"]), float(target["height"])
        transformed = dict(box)
        transformed["x"] = round(max(0.0, min(1.0, tx + ((bx - sx) / sw) * tw)), 6)
        transformed["y"] = round(max(0.0, min(1.0, ty + ((by - sy) / sh) * th)), 6)
        transformed["width"] = round(max(0.0, min(1.0 - transformed["x"], (bw / sw) * tw)), 6)
        transformed["height"] = round(max(0.0, min(1.0 - transformed["y"], (bh / sh) * th)), 6)
        return transformed

    def _manual_order_ref(self, region: dict[str, Any]) -> dict[str, Any]:
        return {
            "manual_region_id": str((region.get("manual_addition") or {}).get("id") or ""),
            "anchor": self._manual_stable_region_anchor(region),
            "original_text": str(
                region.get("ocr_text") or region.get("raw_text") or region.get("text") or ""
            ),
        }

    def _match_manual_correction(
        self,
        correction: dict[str, Any],
        regions: list[dict[str, Any]],
        used: set[int],
    ) -> tuple[int | None, str]:
        manual_id = str(correction.get("manual_region_id") or "")
        if manual_id:
            for index, region in enumerate(regions):
                if index in used:
                    continue
                if str((region.get("manual_addition") or {}).get("id") or "") == manual_id:
                    return index, ""
            return None, "manual_region_not_found"
        anchor = correction.get("anchor")
        if not isinstance(anchor, dict):
            return None, "missing_anchor"
        original = str(correction.get("original_text") or "")
        scored: list[tuple[float, int, float, float]] = []
        for index, region in enumerate(regions):
            if index in used or region.get("manual_addition"):
                continue
            candidate_anchor = self._manual_region_anchor(region)
            iou = self._manual_anchor_iou(anchor, candidate_anchor)
            ax = float(anchor.get("x") or 0.0) + float(anchor.get("width") or 0.0) / 2
            ay = float(anchor.get("y") or 0.0) + float(anchor.get("height") or 0.0) / 2
            bx = float(candidate_anchor.get("x") or 0.0) + float(candidate_anchor.get("width") or 0.0) / 2
            by = float(candidate_anchor.get("y") or 0.0) + float(candidate_anchor.get("height") or 0.0) / 2
            distance = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
            text = str(region.get("ocr_text") or region.get("raw_text") or region.get("text") or "")
            text_ratio = difflib.SequenceMatcher(None, original, text).ratio() if original or text else 1.0
            # Same source scan + strong geometry is authoritative. Text is only
            # a tie-breaker because the point of a rebuild is that OCR text may change.
            if iou < 0.35 and distance > 0.035:
                continue
            score = iou * 0.82 + text_ratio * 0.16 + max(0.0, 0.02 - distance) * 1.5
            scored.append((score, index, iou, distance))
        if not scored:
            return None, "region_not_found"
        scored.sort(reverse=True)
        best = scored[0]
        if best[2] < 0.45 and best[3] > 0.022:
            return None, "geometry_changed"
        if len(scored) > 1 and best[0] - scored[1][0] < 0.035:
            return None, "ambiguous_geometry"
        return best[1], ""

    def _apply_manual_corrections(
        self,
        book_id: int,
        page_index: int,
        regions: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        fingerprint, _generation, _revision = self._ocr_context(int(book_id))
        rows = []
        for item in self._load_manual_corrections(int(book_id)):
            try:
                correction_page = int(item.get("page_index"))
            except (TypeError, ValueError):
                continue
            if correction_page == int(page_index):
                rows.append(item)
        result = [dict(region) for region in regions]
        used: set[int] = set()
        conflicts: list[dict[str, Any]] = []
        for correction in rows:
            if str(correction.get("source_fingerprint") or "") != fingerprint:
                conflicts.append({"id": str(correction.get("id") or ""), "reason": "source_changed"})
                continue
            index, reason = self._match_manual_correction(correction, result, used)
            if index is None:
                conflicts.append({"id": str(correction.get("id") or ""), "reason": reason})
                continue
            used.add(index)
            region = dict(result[index])
            ocr_text = str(region.get("text") or "")
            corrected = str(correction.get("corrected_text") or "")
            region["ocr_text"] = ocr_text
            region["text"] = corrected
            region["manual_correction"] = {
                "id": str(correction.get("id") or ""),
                "revision": int(correction.get("revision") or 1),
                "original_text": str(correction.get("original_text") or ""),
                "updated_at": float(correction.get("updated_at") or 0.0),
                "provenance": "manual",
            }
            result[index] = region
        return result, conflicts

    def _apply_manual_suppressions(
        self,
        book_id: int,
        page_index: int,
        regions: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        fingerprint, _generation, _revision = self._ocr_context(int(book_id))
        rows: list[dict[str, Any]] = []
        for item in self._load_manual_suppressions(int(book_id)):
            try:
                suppression_page = int(item.get("page_index"))
            except (TypeError, ValueError):
                continue
            if suppression_page == int(page_index):
                rows.append(item)
        result = [dict(region) for region in regions]
        used: set[int] = set()
        suppressed: set[int] = set()
        conflicts: list[dict[str, Any]] = []
        for suppression in rows:
            suppression_id = str(suppression.get("id") or "")
            if str(suppression.get("source_fingerprint") or "") != fingerprint:
                conflicts.append({"id": suppression_id, "reason": "source_changed"})
                continue
            index, reason = self._match_manual_correction(suppression, result, used)
            if index is None:
                conflicts.append({"id": suppression_id, "reason": reason})
                continue
            used.add(index)
            suppressed.add(index)
        return [region for index, region in enumerate(result) if index not in suppressed], conflicts

    def _apply_manual_additions(
        self, book_id: int, page_index: int, regions: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        fingerprint, _generation, _revision = self._ocr_context(book_id)
        result = [dict(region) for region in regions]
        conflicts: list[dict[str, Any]] = []
        for item in self._load_manual_additions(book_id):
            try:
                if int(item.get("page_index", -1)) != int(page_index):
                    continue
            except (TypeError, ValueError):
                continue
            addition_id = str(item.get("id") or "")
            if str(item.get("source_fingerprint") or "") != fingerprint:
                conflicts.append({"id": addition_id, "reason": "source_changed"})
                continue
            try:
                geometry = self._validated_manual_geometry(item.get("geometry") or {})
            except ValueError:
                conflicts.append({"id": addition_id, "reason": "invalid_geometry"})
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                conflicts.append({"id": addition_id, "reason": "empty_text"})
                continue
            result.append({
                **geometry,
                "text": text,
                "raw_text": text,
                "ocr_text": text,
                "orientation": str(item.get("orientation") or "vertical"),
                "source": "manual",
                "detector": "manual",
                "geometry_source": "manual",
                "manual_addition": {
                    "id": addition_id,
                    "revision": int(item.get("revision") or 1),
                    "kind": str(item.get("kind") or "addition"),
                    "merge_source_count": len(item.get("merge_sources") or []),
                    "split_group_id": str(item.get("split_group_id") or ""),
                    "split_part_index": int(item.get("split_part_index") or 0),
                    "provenance": "manual",
                },
            })
        return result, conflicts

    def _apply_manual_reading_order(
        self,
        book_id: int,
        page_index: int,
        regions: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        fingerprint, _generation, _revision = self._ocr_context(int(book_id))
        record = None
        for item in self._load_manual_orders(int(book_id)):
            try:
                order_page = int(item.get("page_index"))
            except (TypeError, ValueError):
                continue
            if order_page == int(page_index):
                record = item
                break
        result = [dict(region) for region in regions]
        if record is None:
            return result, []
        record_id = str(record.get("id") or f"page-{int(page_index)}")
        if str(record.get("source_fingerprint") or "") != fingerprint:
            return result, [{"id": record_id, "reason": "source_changed"}]
        order = [dict(item) for item in record.get("order") or [] if isinstance(item, dict)]
        active_suppressions = [
            item
            for item in self._load_manual_suppressions(int(book_id))
            if int(item.get("page_index", -1)) == int(page_index)
            and str(item.get("source_fingerprint") or "") == fingerprint
        ]
        used: set[int] = set()
        matched: list[int] = []
        for reference in order:
            index, reason = self._match_manual_correction(reference, result, used)
            if index is None:
                # Reading-order records created before a region was hidden still
                # contain that region.  Skip only a reference that can be tied
                # safely to an active suppression; any other mismatch remains a
                # conflict instead of silently reordering the wrong bubble.
                hidden = False
                for suppression in active_suppressions:
                    anchor = suppression.get("anchor")
                    if not isinstance(anchor, dict):
                        continue
                    pseudo = dict(anchor)
                    pseudo["text"] = str(suppression.get("original_text") or "")
                    pseudo["raw_text"] = pseudo["text"]
                    pseudo["ocr_text"] = pseudo["text"]
                    hidden_index, _hidden_reason = self._match_manual_correction(
                        reference, [pseudo], set()
                    )
                    if hidden_index == 0:
                        hidden = True
                        break
                if hidden:
                    continue
                return result, [{"id": record_id, "reason": f"order_{reason}"}]
            used.add(index)
            matched.append(index)
        remaining = [index for index in range(len(result)) if index not in used]
        # New manual additions append to a pre-existing saved order without
        # invalidating it; deleting/rebuilding a real OCR region remains a conflict.
        if any(not isinstance(result[index].get("manual_addition"), dict) for index in remaining):
            return result, [{"id": record_id, "reason": "region_count_changed"}]
        matched.extend(remaining)
        ordered: list[dict[str, Any]] = []
        revision = int(record.get("revision") or 1)
        updated_at = float(record.get("updated_at") or 0.0)
        for order_index, index in enumerate(matched):
            region = dict(result[index])
            region["manual_reading_order"] = {
                "id": record_id,
                "revision": revision,
                "index": order_index,
                "updated_at": updated_at,
                "provenance": "manual",
            }
            ordered.append(region)
        return ordered, []

    def _apply_manual_geometry(
        self,
        book_id: int,
        page_index: int,
        regions: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        fingerprint, _generation, _revision = self._ocr_context(int(book_id))
        rows: list[dict[str, Any]] = []
        for item in self._load_manual_geometries(int(book_id)):
            try:
                geometry_page = int(item.get("page_index"))
            except (TypeError, ValueError):
                continue
            if geometry_page == int(page_index):
                rows.append(item)
        result = [dict(region) for region in regions]
        used: set[int] = set()
        conflicts: list[dict[str, Any]] = []
        for correction in rows:
            correction_id = str(correction.get("id") or "")
            if str(correction.get("source_fingerprint") or "") != fingerprint:
                conflicts.append({"id": correction_id, "reason": "source_changed"})
                continue
            index, reason = self._match_manual_correction(correction, result, used)
            if index is None:
                conflicts.append({"id": correction_id, "reason": reason})
                continue
            try:
                target = self._validated_manual_geometry(dict(correction.get("geometry") or {}))
            except ValueError:
                conflicts.append({"id": correction_id, "reason": "invalid_geometry"})
                continue
            used.add(index)
            region = dict(result[index])
            source = self._manual_region_anchor(region)
            transformed = dict(region)
            transformed.update(target)
            segments = region.get("segments")
            if isinstance(segments, list):
                transformed["segments"] = [
                    self._transform_manual_box(item, source, target)
                    if isinstance(item, dict)
                    else item
                    for item in segments
                ]
            provenance = region.get("provenance")
            if isinstance(provenance, dict):
                updated_provenance = dict(provenance)
                line_boxes = provenance.get("line_boxes")
                if isinstance(line_boxes, list):
                    updated_provenance["line_boxes"] = [
                        self._transform_manual_box(item, source, target)
                        if isinstance(item, dict)
                        else item
                        for item in line_boxes
                    ]
                transformed["provenance"] = updated_provenance
            transformed["manual_geometry"] = {
                "id": correction_id,
                "revision": int(correction.get("revision") or 1),
                "original_anchor": dict(correction.get("anchor") or {}),
                "updated_at": float(correction.get("updated_at") or 0.0),
                "provenance": "manual",
            }
            transformed["geometry_source"] = "manual"
            result[index] = transformed
        return result, conflicts

    def manual_ocr_corrections(self, book_id: int) -> dict[str, Any]:
        book_id = int(book_id)
        fingerprint, _generation, _revision = self._ocr_context(book_id)
        rows = self._load_manual_corrections(book_id)
        reading_orders = self._load_manual_orders(book_id)
        geometries = self._load_manual_geometries(book_id)
        suppressions = self._load_manual_suppressions(book_id)
        additions = self._load_manual_additions(book_id)
        return {
            "book_id": book_id,
            "source_fingerprint": fingerprint,
            "corrections": rows,
            "reading_orders": reading_orders,
            "geometries": geometries,
            "suppressions": suppressions,
            "additions": additions,
        }

    def _merge_manual_order_refs(
        self,
        refs: list[dict[str, Any]],
        source_regions: list[dict[str, Any]],
        merged_ref: dict[str, Any],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        insert_at: int | None = None
        for raw_ref in refs:
            ref = dict(raw_ref)
            matched_source = False
            for region in source_regions:
                matched, _reason = self._match_manual_correction(ref, [region], set())
                if matched == 0:
                    matched_source = True
                    break
            if matched_source:
                if insert_at is None:
                    insert_at = len(result)
                continue
            result.append(ref)
        if insert_at is not None:
            result.insert(insert_at, dict(merged_ref))
        return result

    @staticmethod
    def _unmerge_manual_order_refs(
        refs: list[dict[str, Any]],
        merged_region_id: str,
        source_refs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        replaced = False
        for raw_ref in refs:
            ref = dict(raw_ref)
            if str(ref.get("manual_region_id") or "") == merged_region_id:
                result.extend(dict(item) for item in source_refs)
                replaced = True
            else:
                result.append(ref)
        return result if replaced else [dict(item) for item in refs]

    def _split_manual_order_refs(
        self,
        refs: list[dict[str, Any]],
        source_region: dict[str, Any],
        split_refs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        replaced = False
        for raw_ref in refs:
            ref = dict(raw_ref)
            matched, _reason = self._match_manual_correction(ref, [source_region], set())
            if not replaced and matched == 0:
                result.extend(dict(item) for item in split_refs)
                replaced = True
            else:
                result.append(ref)
        return result if replaced else [dict(item) for item in refs]

    @staticmethod
    def _unsplit_manual_order_refs(
        refs: list[dict[str, Any]],
        split_region_ids: set[str],
        source_ref: dict[str, Any],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        inserted = False
        for raw_ref in refs:
            ref = dict(raw_ref)
            manual_id = str(ref.get("manual_region_id") or "")
            if manual_id in split_region_ids:
                if not inserted:
                    result.append(dict(source_ref))
                    inserted = True
                continue
            result.append(ref)
        return result if inserted else [dict(item) for item in refs]

    def split_manual_ocr_region(
        self,
        book_id: int,
        page_index: int,
        source_ref: dict[str, Any],
        parts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        book_id, page_index = int(book_id), int(page_index)
        requested = [dict(item) for item in parts if isinstance(item, dict)]
        if len(requested) != 2:
            raise ValueError("Split must contain exactly two OCR regions")
        normalized_parts: list[dict[str, Any]] = []
        for item in requested:
            text = str(item.get("text") or "").strip()
            if not text or len(text) > 2000:
                raise ValueError("Split OCR text must contain 1–2000 characters")
            orientation = str(item.get("orientation") or "vertical")
            if orientation not in {"vertical", "horizontal"}:
                raise ValueError("Invalid split OCR orientation")
            geometry = self._validated_manual_geometry(dict(item.get("geometry") or {}))
            normalized_parts.append({"text": text, "orientation": orientation, "geometry": geometry})

        with self._ocr_publish_lock(book_id):
            payload = self.text_regions(book_id, page_index, refresh=False, cached_only=True)
            regions = [dict(item) for item in payload.get("regions") or [] if isinstance(item, dict)]
            source_index, reason = self._match_manual_correction(dict(source_ref or {}), regions, set())
            if source_index is None:
                raise ValueError(f"OCR region cannot be matched safely: {reason}")
            source_region = regions[source_index]
            if source_region.get("manual_addition"):
                raise ValueError("Manually added OCR regions cannot be split yet")

            source_box = self._manual_region_anchor(source_region)
            sx1, sy1 = float(source_region.get("x") or 0), float(source_region.get("y") or 0)
            sx2 = sx1 + float(source_region.get("width") or 0)
            sy2 = sy1 + float(source_region.get("height") or 0)
            for item in normalized_parts:
                geometry = item["geometry"]
                x1, y1 = geometry["x"], geometry["y"]
                x2, y2 = x1 + geometry["width"], y1 + geometry["height"]
                tolerance = 0.002
                if x1 < sx1 - tolerance or y1 < sy1 - tolerance or x2 > sx2 + tolerance or y2 > sy2 + tolerance:
                    raise ValueError("Split OCR regions must stay inside the source hitbox")

            fingerprint, _generation, _revision = self._ocr_context(book_id)
            now = time.time()
            group_id = hashlib.sha256(
                f"{fingerprint}|{page_index}|split|{time.time_ns()}".encode("utf-8")
            ).hexdigest()[:20]
            original = str(source_region.get("ocr_text") or source_region.get("raw_text") or source_region.get("text") or "")
            source_order_ref = self._manual_order_ref(source_region)
            suppression_id = hashlib.sha256(
                f"{fingerprint}|{page_index}|split-suppress|{time.time_ns()}|{json.dumps(source_box, sort_keys=True)}".encode("utf-8")
            ).hexdigest()[:20]
            suppression = {
                "id": suppression_id,
                "page_index": page_index,
                "source_fingerprint": fingerprint,
                "anchor": source_box,
                "original_text": original,
                "created_at": now,
                "updated_at": now,
                "reason": "split",
                "split_group_id": group_id,
            }
            additions = self._load_manual_additions(book_id)
            split_additions: list[dict[str, Any]] = []
            split_refs: list[dict[str, Any]] = []
            for index, item in enumerate(normalized_parts):
                addition_id = hashlib.sha256(
                    f"{fingerprint}|{page_index}|split-part|{group_id}|{index}".encode("utf-8")
                ).hexdigest()[:20]
                addition = {
                    "id": addition_id,
                    "kind": "split",
                    "split_group_id": group_id,
                    "split_part_index": index,
                    "split_source": {"suppression_id": suppression_id, "ref": dict(source_order_ref)},
                    "page_index": page_index,
                    "source_fingerprint": fingerprint,
                    "geometry": dict(item["geometry"]),
                    "text": str(item["text"]),
                    "orientation": str(item["orientation"]),
                    "revision": 1,
                    "created_at": now,
                    "updated_at": now,
                }
                additions.append(addition)
                split_additions.append(addition)
                split_refs.append({
                    "manual_region_id": addition_id,
                    "anchor": {**dict(item["geometry"]), "orientation": str(item["orientation"])},
                    "original_text": str(item["text"]),
                })

            suppressions = self._load_manual_suppressions(book_id)
            existing_hidden = [dict(item) for item in suppressions]
            suppressions.append(suppression)
            orders = self._load_manual_orders(book_id)
            order = next((item for item in orders if int(item.get("page_index", -1)) == page_index
                          and str(item.get("source_fingerprint") or "") == fingerprint), None)
            if order is None:
                canonical: list[dict[str, Any]] = []
                for index, region in enumerate(regions):
                    if index == source_index:
                        canonical.extend(dict(item) for item in split_refs)
                    else:
                        canonical.append(self._manual_order_ref(region))
                for hidden in existing_hidden:
                    if int(hidden.get("page_index", -1)) != page_index or str(hidden.get("source_fingerprint") or "") != fingerprint:
                        continue
                    anchor = hidden.get("anchor")
                    if isinstance(anchor, dict):
                        canonical.append({"anchor": dict(anchor), "original_text": str(hidden.get("original_text") or "")})
                order = {
                    "id": hashlib.sha256(f"{fingerprint}|{page_index}|order|{time.time_ns()}".encode("utf-8")).hexdigest()[:20],
                    "page_index": page_index,
                    "source_fingerprint": fingerprint,
                    "order": canonical,
                    "revision": 1,
                    "history": [],
                    "created_at": now,
                    "updated_at": now,
                }
                orders.append(order)
            else:
                order["order"] = self._split_manual_order_refs(
                    [dict(item) for item in order.get("order") or [] if isinstance(item, dict)],
                    source_region, split_refs,
                )
                order["history"] = [
                    self._split_manual_order_refs(
                        [dict(item) for item in snapshot if isinstance(item, dict)], source_region, split_refs
                    )
                    for snapshot in order.get("history") or [] if isinstance(snapshot, list)
                ]
                order["revision"] = int(order.get("revision") or 1) + 1
                order["updated_at"] = now

            self._save_manual_suppressions(book_id, suppressions)
            self._save_manual_additions(book_id, additions)
            self._save_manual_orders(book_id, orders)

        page = self.text_regions(book_id, page_index, refresh=False, cached_only=True)
        return {"ok": True, "split_group_id": group_id, "additions": [dict(item) for item in split_additions], "page": page}

    def unsplit_manual_ocr_region(self, book_id: int, addition_id: str) -> dict[str, Any]:
        book_id = int(book_id)
        wanted = str(addition_id or "").strip()
        with self._ocr_publish_lock(book_id):
            additions = self._load_manual_additions(book_id)
            target = next((item for item in additions if str(item.get("id") or "") == wanted), None)
            if target is None:
                return {"ok": True, "changed": False}
            if str(target.get("kind") or "") != "split":
                raise ValueError("Manual OCR region is not a split")
            group_id = str(target.get("split_group_id") or "")
            group = [item for item in additions if str(item.get("kind") or "") == "split"
                     and str(item.get("split_group_id") or "") == group_id]
            split_ids = {str(item.get("id") or "") for item in group}
            source = dict(target.get("split_source") or {})
            suppression_id = str(source.get("suppression_id") or "")
            source_ref = dict(source.get("ref") or {})
            page_index = int(target.get("page_index") or 0)
            additions = [item for item in additions if str(item.get("id") or "") not in split_ids]
            suppressions = [item for item in self._load_manual_suppressions(book_id)
                            if str(item.get("id") or "") != suppression_id]
            orders = self._load_manual_orders(book_id)
            for order in orders:
                if int(order.get("page_index", -1)) != page_index:
                    continue
                order["order"] = self._unsplit_manual_order_refs(
                    [dict(item) for item in order.get("order") or [] if isinstance(item, dict)],
                    split_ids, source_ref,
                )
                order["history"] = [
                    self._unsplit_manual_order_refs(
                        [dict(item) for item in snapshot if isinstance(item, dict)], split_ids, source_ref
                    )
                    for snapshot in order.get("history") or [] if isinstance(snapshot, list)
                ]
            self._save_manual_additions(book_id, additions)
            self._save_manual_suppressions(book_id, suppressions)
            self._save_manual_orders(book_id, orders)
        return {
            "ok": True,
            "changed": True,
            "page": self.text_regions(book_id, page_index, refresh=False, cached_only=True),
        }

    def merge_manual_ocr_regions(
        self,
        book_id: int,
        page_index: int,
        first_ref: dict[str, Any],
        second_ref: dict[str, Any],
        text: str,
        orientation: str = "vertical",
    ) -> dict[str, Any]:
        book_id, page_index = int(book_id), int(page_index)
        corrected = str(text or "").strip()
        if not corrected or len(corrected) > 2000:
            raise ValueError("Merged OCR text must contain 1–2000 characters")
        if orientation not in {"vertical", "horizontal"}:
            raise ValueError("Invalid merged OCR orientation")
        with self._ocr_publish_lock(book_id):
            payload = self.text_regions(book_id, page_index, refresh=False, cached_only=True)
            regions = [dict(item) for item in payload.get("regions") or [] if isinstance(item, dict)]
            used: set[int] = set()
            first_index, first_reason = self._match_manual_correction(dict(first_ref or {}), regions, used)
            if first_index is None:
                raise ValueError(f"First OCR region cannot be matched safely: {first_reason}")
            used.add(first_index)
            second_index, second_reason = self._match_manual_correction(dict(second_ref or {}), regions, used)
            if second_index is None:
                raise ValueError(f"Second OCR region cannot be matched safely: {second_reason}")
            if first_index == second_index:
                raise ValueError("Choose two different OCR regions")
            source_regions = [regions[index] for index in sorted((first_index, second_index))]
            if any(region.get("manual_addition") for region in source_regions):
                raise ValueError("Manually added OCR regions cannot be merged yet")

            left = min(float(region.get("x") or 0.0) for region in source_regions)
            bottom = min(float(region.get("y") or 0.0) for region in source_regions)
            right = max(float(region.get("x") or 0.0) + float(region.get("width") or 0.0) for region in source_regions)
            top = max(float(region.get("y") or 0.0) + float(region.get("height") or 0.0) for region in source_regions)
            geometry = self._validated_manual_geometry({
                "x": left,
                "y": bottom,
                "width": right - left,
                "height": top - bottom,
            })
            fingerprint, _generation, _revision = self._ocr_context(book_id)
            now = time.time()
            suppression_rows = self._load_manual_suppressions(book_id)
            suppression_ids: list[str] = []
            source_refs: list[dict[str, Any]] = []
            for source_region in source_regions:
                anchor = self._manual_stable_region_anchor(source_region)
                original = str(
                    source_region.get("ocr_text")
                    or source_region.get("raw_text")
                    or source_region.get("text")
                    or ""
                )
                suppression_id = hashlib.sha256(
                    f"{fingerprint}|{page_index}|merge-suppress|{time.time_ns()}|{json.dumps(anchor, sort_keys=True)}".encode("utf-8")
                ).hexdigest()[:20]
                suppression_rows.append({
                    "id": suppression_id,
                    "page_index": page_index,
                    "source_fingerprint": fingerprint,
                    "anchor": anchor,
                    "original_text": original,
                    "created_at": now,
                    "updated_at": now,
                    "reason": "merge",
                })
                suppression_ids.append(suppression_id)
                source_refs.append(self._manual_order_ref(source_region))

            addition_id = hashlib.sha256(
                f"{fingerprint}|{page_index}|merge|{time.time_ns()}".encode("utf-8")
            ).hexdigest()[:20]
            addition = {
                "id": addition_id,
                "kind": "merge",
                "page_index": page_index,
                "source_fingerprint": fingerprint,
                "geometry": geometry,
                "text": corrected,
                "orientation": orientation,
                "merge_sources": [
                    {"suppression_id": suppression_id, "ref": dict(source_ref)}
                    for suppression_id, source_ref in zip(suppression_ids, source_refs)
                ],
                "revision": 1,
                "created_at": now,
                "updated_at": now,
            }
            additions = self._load_manual_additions(book_id)
            additions.append(addition)

            merged_ref = {
                "manual_region_id": addition_id,
                "anchor": {**geometry, "orientation": orientation},
                "original_text": corrected,
            }
            orders = self._load_manual_orders(book_id)
            for order in orders:
                if int(order.get("page_index", -1)) != page_index:
                    continue
                if str(order.get("source_fingerprint") or "") != fingerprint:
                    continue
                order["order"] = self._merge_manual_order_refs(
                    [dict(item) for item in order.get("order") or [] if isinstance(item, dict)],
                    source_regions,
                    merged_ref,
                )
                order["history"] = [
                    self._merge_manual_order_refs(
                        [dict(item) for item in snapshot if isinstance(item, dict)],
                        source_regions,
                        merged_ref,
                    )
                    for snapshot in order.get("history") or []
                    if isinstance(snapshot, list)
                ]

            self._save_manual_suppressions(book_id, suppression_rows)
            self._save_manual_additions(book_id, additions)
            self._save_manual_orders(book_id, orders)
        page = self.text_regions(book_id, page_index, refresh=False, cached_only=True)
        return {"ok": True, "addition": dict(addition), "page": page}

    def unmerge_manual_ocr_region(self, book_id: int, addition_id: str) -> dict[str, Any]:
        book_id = int(book_id)
        wanted = str(addition_id or "").strip()
        with self._ocr_publish_lock(book_id):
            additions = self._load_manual_additions(book_id)
            target = next((item for item in additions if str(item.get("id") or "") == wanted), None)
            if target is None:
                return {"ok": True, "changed": False}
            if str(target.get("kind") or "") != "merge":
                raise ValueError("Manual OCR region is not a merge")
            page_index = int(target.get("page_index") or 0)
            merge_sources = [dict(item) for item in target.get("merge_sources") or [] if isinstance(item, dict)]
            suppression_ids = {
                str(item.get("suppression_id") or "") for item in merge_sources
                if str(item.get("suppression_id") or "")
            }
            source_refs = [dict(item.get("ref") or {}) for item in merge_sources if isinstance(item.get("ref"), dict)]
            additions = [item for item in additions if item is not target]
            suppressions = [
                item for item in self._load_manual_suppressions(book_id)
                if str(item.get("id") or "") not in suppression_ids
            ]
            orders = self._load_manual_orders(book_id)
            for order in orders:
                if int(order.get("page_index", -1)) != page_index:
                    continue
                order["order"] = self._unmerge_manual_order_refs(
                    [dict(item) for item in order.get("order") or [] if isinstance(item, dict)],
                    wanted,
                    source_refs,
                )
                order["history"] = [
                    self._unmerge_manual_order_refs(
                        [dict(item) for item in snapshot if isinstance(item, dict)],
                        wanted,
                        source_refs,
                    )
                    for snapshot in order.get("history") or []
                    if isinstance(snapshot, list)
                ]
            self._save_manual_additions(book_id, additions)
            self._save_manual_suppressions(book_id, suppressions)
            self._save_manual_orders(book_id, orders)
        return {
            "ok": True,
            "changed": True,
            "page": self.text_regions(book_id, page_index, refresh=False, cached_only=True),
        }

    def add_manual_ocr_region(
        self, book_id: int, page_index: int, geometry: dict[str, Any],
        text: str, orientation: str = "vertical",
    ) -> dict[str, Any]:
        book_id, page_index = int(book_id), int(page_index)
        if page_index < 0 or page_index >= int(self._book(book_id)["page_count"]):
            raise IndexError("Invalid manga page index")
        corrected = str(text or "").strip()
        if not corrected or len(corrected) > 2000:
            raise ValueError("Manual OCR text must contain 1–2000 characters")
        if orientation not in {"vertical", "horizontal"}:
            raise ValueError("Invalid manual OCR orientation")
        target = self._validated_manual_geometry(dict(geometry or {}))
        with self._ocr_publish_lock(book_id):
            fingerprint, _generation, _revision = self._ocr_context(book_id)
            now = time.time()
            addition = {
                "id": hashlib.sha256(f"{fingerprint}|{page_index}|add|{time.time_ns()}".encode()).hexdigest()[:20],
                "page_index": page_index,
                "source_fingerprint": fingerprint,
                "geometry": target,
                "text": corrected,
                "orientation": orientation,
                "revision": 1,
                "created_at": now,
                "updated_at": now,
            }
            rows = self._load_manual_additions(book_id)
            rows.append(addition)
            self._save_manual_additions(book_id, rows)
        page = self.text_regions(book_id, page_index, refresh=False, cached_only=True)
        return {"ok": True, "addition": dict(addition), "page": page}

    def update_manual_ocr_region(
        self, book_id: int, addition_id: str, *, text: str | None = None,
        geometry: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        book_id = int(book_id)
        wanted = str(addition_id or "").strip()
        if text is None and geometry is None:
            raise ValueError("No manual OCR changes")
        corrected = None if text is None else str(text).strip()
        if corrected is not None and (not corrected or len(corrected) > 2000):
            raise ValueError("Manual OCR text must contain 1–2000 characters")
        target_geometry = None if geometry is None else self._validated_manual_geometry(dict(geometry))
        with self._ocr_publish_lock(book_id):
            rows = self._load_manual_additions(book_id)
            target = next((row for row in rows if str(row.get("id") or "") == wanted), None)
            if target is None:
                raise KeyError("Manual OCR region not found")
            fingerprint, _generation, _revision = self._ocr_context(book_id)
            if str(target.get("source_fingerprint") or "") != fingerprint:
                raise ValueError("Manual OCR region belongs to a different source scan")
            previous = {"text": target["text"], "geometry": dict(target["geometry"])}
            if corrected is not None:
                target["text"] = corrected
            if target_geometry is not None:
                target["geometry"] = target_geometry
            if previous != {"text": target["text"], "geometry": target["geometry"]}:
                target["history"] = [*(target.get("history") or []), previous][-20:]
                target["revision"] = int(target.get("revision") or 1) + 1
                target["updated_at"] = time.time()
                self._save_manual_additions(book_id, rows)
            page_index = int(target["page_index"])
        return {"ok": True, "addition": dict(target),
                "page": self.text_regions(book_id, page_index, refresh=False, cached_only=True)}

    def undo_manual_ocr_region(self, book_id: int, addition_id: str) -> dict[str, Any]:
        book_id = int(book_id)
        wanted = str(addition_id or "").strip()
        with self._ocr_publish_lock(book_id):
            rows = self._load_manual_additions(book_id)
            target = next((row for row in rows if str(row.get("id") or "") == wanted), None)
            if target is None:
                return {"ok": True, "changed": False}
            kind = str(target.get("kind") or "addition")
            if kind == "merge":
                raise ValueError("Undo the merged OCR region instead")
            if kind == "split":
                raise ValueError("Undo the split OCR region instead")
            fingerprint, _generation, _revision = self._ocr_context(book_id)
            stale = str(target.get("source_fingerprint") or "") != fingerprint
            page_index = int(target["page_index"])
            history = [] if stale else list(target.get("history") or [])
            if history:
                previous = history.pop()
                target["text"] = previous["text"]
                target["geometry"] = previous["geometry"]
                target["history"] = history
                target["revision"] = int(target.get("revision") or 1) + 1
                target["updated_at"] = time.time()
            else:
                rows = [row for row in rows if row is not target]
                # Remove obsolete references from durable reading order and undo history.
                orders = self._load_manual_orders(book_id)
                for order in orders:
                    if int(order.get("page_index", -1)) != page_index:
                        continue
                    order["order"] = [ref for ref in order.get("order") or []
                                      if str(ref.get("manual_region_id") or "") != wanted]
                    order["history"] = [[ref for ref in snapshot
                                         if str(ref.get("manual_region_id") or "") != wanted]
                                        for snapshot in order.get("history") or []]
                self._save_manual_orders(book_id, orders)
            self._save_manual_additions(book_id, rows)
        return {"ok": True, "changed": True,
                "page": self.text_regions(book_id, page_index, refresh=False, cached_only=True)}

    def suppress_manual_ocr_region(
        self, book_id: int, page_index: int, region_index: int
    ) -> dict[str, Any]:
        book_id, page_index, region_index = int(book_id), int(page_index), int(region_index)
        with self._ocr_publish_lock(book_id):
            payload = self.text_regions(book_id, page_index, refresh=False, cached_only=True)
            regions = [dict(item) for item in payload.get("regions") or [] if isinstance(item, dict)]
            if region_index < 0 or region_index >= len(regions):
                raise IndexError(f"Unknown manga OCR region index={region_index}")
            region = regions[region_index]
            if region.get("manual_addition"):
                raise ValueError("Use manual-region undo for a manually added OCR region")
            fingerprint, _generation, _revision = self._ocr_context(book_id)
            anchor = self._manual_stable_region_anchor(region)
            original = str(region.get("ocr_text") or region.get("raw_text") or region.get("text") or "")
            rows = self._load_manual_suppressions(book_id)
            # Do not stack duplicate suppressions for the same visible region.
            for existing in rows:
                if int(existing.get("page_index", -1)) != page_index:
                    continue
                if str(existing.get("source_fingerprint") or "") != fingerprint:
                    continue
                matched, _reason = self._match_manual_correction(existing, [region], set())
                if matched == 0:
                    refreshed = self.text_regions(book_id, page_index, refresh=False, cached_only=True)
                    return {"ok": True, "changed": False, "suppression": dict(existing), "page": refreshed}
            now = time.time()
            suppression_id = hashlib.sha256(
                f"{fingerprint}|{page_index}|suppress|{time.time_ns()}|{json.dumps(anchor, sort_keys=True)}".encode("utf-8")
            ).hexdigest()[:20]
            suppression = {
                "id": suppression_id,
                "page_index": page_index,
                "source_fingerprint": fingerprint,
                "anchor": anchor,
                "original_text": original,
                "created_at": now,
                "updated_at": now,
            }
            rows.append(suppression)
            self._save_manual_suppressions(book_id, rows)
        refreshed = self.text_regions(book_id, page_index, refresh=False, cached_only=True)
        return {"ok": True, "changed": True, "suppression": dict(suppression), "page": refreshed}

    def restore_manual_ocr_region(self, book_id: int, suppression_id: str) -> dict[str, Any]:
        book_id = int(book_id)
        wanted = str(suppression_id or "").strip()
        if not wanted:
            raise ValueError("Missing manual OCR suppression id")
        with self._ocr_publish_lock(book_id):
            rows = self._load_manual_suppressions(book_id)
            target = next((item for item in rows if str(item.get("id") or "") == wanted), None)
            if target is None:
                return {"ok": True, "changed": False}
            reason = str(target.get("reason") or "")
            if reason == "merge":
                raise ValueError("Undo the merged OCR region instead")
            if reason == "split":
                raise ValueError("Undo the split OCR region instead")
            try:
                page_index = int(target.get("page_index"))
            except (TypeError, ValueError):
                page_index = 0
            rows = [item for item in rows if str(item.get("id") or "") != wanted]
            self._save_manual_suppressions(book_id, rows)
        refreshed = self.text_regions(book_id, page_index, refresh=False, cached_only=True)
        return {"ok": True, "changed": True, "page_index": page_index, "page": refreshed}

    def set_manual_ocr_geometry(
        self,
        book_id: int,
        page_index: int,
        region_index: int,
        geometry: dict[str, Any],
    ) -> dict[str, Any]:
        book_id, page_index, region_index = int(book_id), int(page_index), int(region_index)
        target = self._validated_manual_geometry(dict(geometry or {}))
        with self._ocr_publish_lock(book_id):
            payload = self.text_regions(book_id, page_index, refresh=False, cached_only=True)
            regions = [dict(item) for item in payload.get("regions") or [] if isinstance(item, dict)]
            if region_index < 0 or region_index >= len(regions):
                raise IndexError(f"Unknown manga OCR region index={region_index}")
            region = regions[region_index]
            existing_meta = (
                region.get("manual_geometry")
                if isinstance(region.get("manual_geometry"), dict)
                else {}
            )
            current_id = str(existing_meta.get("id") or "")
            fingerprint, _generation, _revision = self._ocr_context(book_id)
            rows = self._load_manual_geometries(book_id)
            existing = next((item for item in rows if str(item.get("id") or "") == current_id), None)
            now = time.time()
            if existing is None:
                anchor = self._manual_region_anchor(region)
                original = str(region.get("ocr_text") or region.get("raw_text") or region.get("text") or "")
                correction_id = hashlib.sha256(
                    f"{fingerprint}|{page_index}|geometry|{time.time_ns()}".encode("utf-8")
                ).hexdigest()[:20]
                existing = {
                    "id": correction_id,
                    "page_index": page_index,
                    "source_fingerprint": fingerprint,
                    "anchor": anchor,
                    "original_text": original,
                    "geometry": target,
                    "revision": 1,
                    "history": [],
                    "created_at": now,
                    "updated_at": now,
                }
                rows.append(existing)
            else:
                previous = dict(existing.get("geometry") or {})
                history = [
                    dict(snapshot)
                    for snapshot in existing.get("history") or []
                    if isinstance(snapshot, dict)
                ]
                if previous and previous != target:
                    history.append(previous)
                existing["source_fingerprint"] = fingerprint
                existing["geometry"] = target
                existing["history"] = history[-20:]
                existing["revision"] = int(existing.get("revision") or 1) + 1
                existing["updated_at"] = now
            self._save_manual_geometries(book_id, rows)
        refreshed = self.text_regions(book_id, page_index, refresh=False, cached_only=True)
        return {"ok": True, "geometry": dict(existing), "page": refreshed}

    def undo_manual_ocr_geometry(
        self, book_id: int, page_index: int, region_index: int
    ) -> dict[str, Any]:
        book_id, page_index, region_index = int(book_id), int(page_index), int(region_index)
        with self._ocr_publish_lock(book_id):
            payload = self.text_regions(book_id, page_index, refresh=False, cached_only=True)
            regions = [dict(item) for item in payload.get("regions") or [] if isinstance(item, dict)]
            if region_index < 0 or region_index >= len(regions):
                raise IndexError(f"Unknown manga OCR region index={region_index}")
            meta = regions[region_index].get("manual_geometry")
            correction_id = str(meta.get("id") or "") if isinstance(meta, dict) else ""
            if not correction_id:
                return {"ok": True, "changed": False, "page": payload}
            rows = self._load_manual_geometries(book_id)
            target = next((item for item in rows if str(item.get("id") or "") == correction_id), None)
            if target is None:
                return {"ok": True, "changed": False, "page": payload}
            history = [
                dict(snapshot)
                for snapshot in target.get("history") or []
                if isinstance(snapshot, dict)
            ]
            if history:
                target["geometry"] = history.pop()
                target["history"] = history
                target["revision"] = int(target.get("revision") or 1) + 1
                target["updated_at"] = time.time()
            else:
                rows = [item for item in rows if item is not target]
            self._save_manual_geometries(book_id, rows)
        refreshed = self.text_regions(book_id, page_index, refresh=False, cached_only=True)
        return {"ok": True, "changed": True, "page": refreshed}

    def set_manual_ocr_reading_order(
        self,
        book_id: int,
        page_index: int,
        order: list[dict[str, Any]],
    ) -> dict[str, Any]:
        book_id, page_index = int(book_id), int(page_index)
        requested = [dict(item) for item in order if isinstance(item, dict)]
        with self._ocr_publish_lock(book_id):
            payload = self.text_regions(book_id, page_index, refresh=False, cached_only=True)
            regions = [dict(item) for item in payload.get("regions") or [] if isinstance(item, dict)]
            if len(requested) != len(regions) or not regions:
                raise ValueError("Reading order must contain every OCR region on the page")

            normalized: list[dict[str, Any]] = []
            for item in requested:
                anchor = item.get("anchor")
                if not isinstance(anchor, dict):
                    anchor = {name: item.get(name) for name in ("x", "y", "width", "height", "orientation")}
                normalized.append({
                    "anchor": {
                        "x": round(float(anchor.get("x") or 0.0), 6),
                        "y": round(float(anchor.get("y") or 0.0), 6),
                        "width": round(float(anchor.get("width") or 0.0), 6),
                        "height": round(float(anchor.get("height") or 0.0), 6),
                        "orientation": str(anchor.get("orientation") or item.get("orientation") or ""),
                    },
                    "original_text": str(item.get("original_text") or item.get("text") or ""),
                    "manual_region_id": str(item.get("manual_region_id") or ""),
                })

            used: set[int] = set()
            matched: list[int] = []
            for reference in normalized:
                index, reason = self._match_manual_correction(reference, regions, used)
                if index is None:
                    raise ValueError(f"Reading-order region cannot be matched safely: {reason}")
                used.add(index)
                matched.append(index)
            if len(set(matched)) != len(regions):
                raise ValueError("Reading order contains duplicate OCR regions")
            canonical = [self._manual_order_ref(regions[index]) for index in matched]

            fingerprint, _generation, _revision = self._ocr_context(book_id)
            # Keep currently hidden regions in the durable record.  They are
            # appended after the visible order and ignored while suppressed,
            # so restoring one later does not invalidate the user's ordering.
            for suppression in self._load_manual_suppressions(book_id):
                if int(suppression.get("page_index", -1)) != page_index:
                    continue
                if str(suppression.get("source_fingerprint") or "") != fingerprint:
                    continue
                anchor = suppression.get("anchor")
                if not isinstance(anchor, dict):
                    continue
                canonical.append({
                    "anchor": dict(anchor),
                    "original_text": str(suppression.get("original_text") or ""),
                })

            rows = self._load_manual_orders(book_id)
            existing = next(
                (item for item in rows if int(item.get("page_index", -1)) == page_index),
                None,
            )
            now = time.time()
            if existing is None:
                order_id = hashlib.sha256(
                    f"{fingerprint}|{page_index}|order|{time.time_ns()}".encode("utf-8")
                ).hexdigest()[:20]
                existing = {
                    "id": order_id,
                    "page_index": page_index,
                    "source_fingerprint": fingerprint,
                    "order": canonical,
                    "revision": 1,
                    "history": [],
                    "created_at": now,
                    "updated_at": now,
                }
                rows.append(existing)
            else:
                previous = [dict(item) for item in existing.get("order") or [] if isinstance(item, dict)]
                history = [
                    [dict(ref) for ref in snapshot if isinstance(ref, dict)]
                    for snapshot in existing.get("history") or []
                    if isinstance(snapshot, list)
                ]
                if previous and previous != canonical:
                    history.append(previous)
                existing["source_fingerprint"] = fingerprint
                existing["order"] = canonical
                existing["history"] = history[-20:]
                existing["revision"] = int(existing.get("revision") or 1) + 1
                existing["updated_at"] = now
            self._save_manual_orders(book_id, rows)

        refreshed = self.text_regions(book_id, page_index, refresh=False, cached_only=True)
        return {"ok": True, "reading_order": dict(existing), "page": refreshed}

    def undo_manual_ocr_reading_order(self, book_id: int, page_index: int) -> dict[str, Any]:
        book_id, page_index = int(book_id), int(page_index)
        with self._ocr_publish_lock(book_id):
            rows = self._load_manual_orders(book_id)
            target = next(
                (item for item in rows if int(item.get("page_index", -1)) == page_index),
                None,
            )
            if target is None:
                page = self.text_regions(book_id, page_index, refresh=False, cached_only=True)
                return {"ok": True, "changed": False, "page": page}
            history = [
                [dict(ref) for ref in snapshot if isinstance(ref, dict)]
                for snapshot in target.get("history") or []
                if isinstance(snapshot, list)
            ]
            if history:
                target["order"] = history.pop()
                target["history"] = history
                target["revision"] = int(target.get("revision") or 1) + 1
                target["updated_at"] = time.time()
            else:
                rows = [item for item in rows if item is not target]
            self._save_manual_orders(book_id, rows)
        refreshed = self.text_regions(book_id, page_index, refresh=False, cached_only=True)
        return {"ok": True, "changed": True, "page": refreshed}

    def set_manual_ocr_correction(
        self,
        book_id: int,
        page_index: int,
        region_index: int,
        text: str,
    ) -> dict[str, Any]:
        book_id, page_index, region_index = int(book_id), int(page_index), int(region_index)
        corrected = str(text or "").strip()
        if not corrected:
            raise ValueError("Corrected OCR text cannot be empty")
        with self._ocr_publish_lock(book_id):
            payload = self.text_regions(book_id, page_index, refresh=False, cached_only=True)
            regions = [dict(item) for item in payload.get("regions") or [] if isinstance(item, dict)]
            if region_index < 0 or region_index >= len(regions):
                raise IndexError(f"Unknown manga OCR region index={region_index}")
            region = regions[region_index]
            existing_meta = (
                region.get("manual_correction")
                if isinstance(region.get("manual_correction"), dict)
                else {}
            )
            current_id = str(existing_meta.get("id") or "")
            fingerprint, _generation, _revision = self._ocr_context(book_id)
            rows = self._load_manual_corrections(book_id)
            existing = next((item for item in rows if str(item.get("id") or "") == current_id), None)
            now = time.time()
            if existing is None:
                original = str(region.get("ocr_text") or region.get("raw_text") or region.get("text") or "")
                anchor = self._manual_stable_region_anchor(region)
                correction_id = hashlib.sha256(
                    f"{fingerprint}|{page_index}|{time.time_ns()}|{json.dumps(anchor, sort_keys=True)}".encode("utf-8")
                ).hexdigest()[:20]
                existing = {
                    "id": correction_id,
                    "page_index": page_index,
                    "source_fingerprint": fingerprint,
                    "anchor": anchor,
                    "original_text": original,
                    "corrected_text": corrected,
                    "revision": 1,
                    "history": [str(region.get("text") or original)],
                    "created_at": now,
                    "updated_at": now,
                }
                rows.append(existing)
            else:
                previous = str(existing.get("corrected_text") or "")
                history = [str(item) for item in existing.get("history") or []]
                if previous and (not history or history[-1] != previous):
                    history.append(previous)
                existing["history"] = history[-20:]
                existing["corrected_text"] = corrected
                existing["revision"] = int(existing.get("revision") or 1) + 1
                existing["updated_at"] = now
            self._save_manual_corrections(book_id, rows)
        refreshed = self.text_regions(book_id, page_index, refresh=False, cached_only=True)
        return {
            "ok": True,
            "correction": dict(existing),
            "page": refreshed,
        }

    def undo_manual_ocr_correction(
        self, book_id: int, page_index: int, region_index: int
    ) -> dict[str, Any]:
        book_id, page_index, region_index = int(book_id), int(page_index), int(region_index)
        with self._ocr_publish_lock(book_id):
            payload = self.text_regions(book_id, page_index, refresh=False, cached_only=True)
            regions = [dict(item) for item in payload.get("regions") or [] if isinstance(item, dict)]
            if region_index < 0 or region_index >= len(regions):
                raise IndexError(f"Unknown manga OCR region index={region_index}")
            meta = regions[region_index].get("manual_correction")
            correction_id = str(meta.get("id") or "") if isinstance(meta, dict) else ""
            if not correction_id:
                return {"ok": True, "changed": False, "page": payload}
            rows = self._load_manual_corrections(book_id)
            target = next((item for item in rows if str(item.get("id") or "") == correction_id), None)
            if target is None:
                return {"ok": True, "changed": False, "page": payload}
            history = [str(item) for item in target.get("history") or []]
            previous = history.pop() if history else str(target.get("original_text") or "")
            original = str(target.get("original_text") or "")
            if previous == original:
                rows = [item for item in rows if str(item.get("id") or "") != correction_id]
            else:
                target["corrected_text"] = previous
                target["history"] = history
                target["revision"] = int(target.get("revision") or 1) + 1
                target["updated_at"] = time.time()
            self._save_manual_corrections(book_id, rows)
        refreshed = self.text_regions(book_id, page_index, refresh=False, cached_only=True)
        return {"ok": True, "changed": True, "page": refreshed}

    @staticmethod
    def _sanitize_reader_preferences(values: dict[str, Any]) -> dict[str, Any]:
        result = dict(_READER_PREF_DEFAULTS)
        for key in ("statusHighlight", "statusNew", "statusLearning", "statusKnown", "statusDue"):
            if key in values:
                result[key] = bool(values[key])
        if "statusOpacity" in values:
            try:
                result["statusOpacity"] = max(10, min(65, int(values["statusOpacity"])))
            except (TypeError, ValueError):
                pass
        for key in ("statusNewColor", "statusLearningColor", "statusKnownColor", "statusDueColor"):
            value = str(values.get(key, "") or "")
            if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
                result[key] = value.lower()
        return result

    def reader_preferences(self) -> dict[str, Any]:
        raw = self.db.get_state(_READER_PREFS_KEY, "")
        if not raw:
            return {"configured": False, **_READER_PREF_DEFAULTS}
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"configured": False, **_READER_PREF_DEFAULTS}
        if not isinstance(parsed, dict):
            return {"configured": False, **_READER_PREF_DEFAULTS}
        return {"configured": True, **self._sanitize_reader_preferences(parsed)}

    def save_reader_preferences(self, values: dict[str, Any]) -> dict[str, Any]:
        current = self.reader_preferences()
        merged = {
            key: current.get(key, default)
            for key, default in _READER_PREF_DEFAULTS.items()
        }
        if isinstance(values, dict):
            for key in _READER_PREF_DEFAULTS:
                if key in values:
                    merged[key] = values[key]
        sanitized = self._sanitize_reader_preferences(merged)
        self.db.set_state(
            _READER_PREFS_KEY,
            json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")),
        )
        return {"configured": True, **sanitized}

    @staticmethod
    def archive_for_mokuro(sidecar: Path) -> Path:
        """Resolve one adjacent archive for a user-selected .mokuro sidecar.

        The mapping is deliberately narrow: exact sibling stem, explicit
        ``archive.cbz.mokuro``/``archive.zip.mokuro`` packaging, or the known
        sibling ``mokuro/`` directory. Never guess by title across a folder.
        """
        path = Path(sidecar).expanduser().resolve()
        if path.suffix.casefold() != ".mokuro" or not path.is_file():
            raise MokuroImportError("Select an existing .mokuro file")
        candidates: list[Path] = []
        without_mokuro = path.with_suffix("")
        if without_mokuro.suffix.casefold() in {".cbz", ".zip"} and without_mokuro.is_file():
            candidates.append(without_mokuro.resolve())
        else:
            for suffix in (".cbz", ".zip"):
                candidate = path.with_suffix(suffix)
                if candidate.is_file():
                    candidates.append(candidate.resolve())
            if path.parent.name.casefold() == "mokuro":
                for suffix in (".cbz", ".zip"):
                    candidate = path.parent.parent / f"{path.stem}{suffix}"
                    if candidate.is_file():
                        candidates.append(candidate.resolve())
        unique = list(dict.fromkeys(candidates))
        if not unique:
            raise MokuroImportError("No matching CBZ/ZIP archive was found next to this Mokuro file")
        if len(unique) != 1:
            raise MokuroImportError("Mokuro matches more than one archive; select the archive explicitly")
        return unique[0]

    def ocr_available(self, *, refresh: bool = False) -> bool:
        now = time.monotonic()
        if not refresh and self._ocr_available_cache is not None:
            checked_at, available = self._ocr_available_cache
            if now - checked_at < 30.0:
                return available
        try:
            completed = subprocess.run(
                [
                    self.python,
                    "-c",
                    "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('manga_ocr') else 1)",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=8,
            )
            available = completed.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            available = False
        self._ocr_available_cache = (now, available)
        return available

    @staticmethod
    def _pages(path: Path) -> list[str]:
        if path.suffix.casefold() not in {".cbz", ".zip"} or not path.is_file():
            raise ValueError("Manga v1 supports CBZ and ZIP archives")
        with zipfile.ZipFile(path) as archive:
            pages = [
                name
                for name in archive.namelist()
                if not name.endswith("/") and Path(name).suffix.casefold() in _IMAGE_EXTENSIONS
            ]
        return sorted(pages, key=_natural_key)

    @staticmethod
    def series_title(value: str) -> str:
        return _manga_series_title(value)

    @staticmethod
    def series_key(value: str) -> str:
        return _manga_series_key(value)

    def _inherit_series_anilist(self, book_id: int) -> bool:
        row = self._book(int(book_id))
        key = _manga_series_key(str(row["title"] or Path(str(row["path"] or "")).stem))
        if not key or row["anilist_id"] is not None:
            return False
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM manga_books WHERE id<>? AND anilist_id IS NOT NULL ORDER BY updated_at DESC,id DESC",
                (int(book_id),),
            ).fetchall()
            match = next(
                (item for item in rows if _manga_series_key(str(item["title"] or Path(str(item["path"] or "")).stem)) == key),
                None,
            )
            if match is None:
                return False
            conn.execute(
                "UPDATE manga_books SET anilist_id=?,cover_url=?,site_url=?,user_score=?,mean_score=?,updated_at=? "
                "WHERE id=?",
                (
                    match["anilist_id"],
                    match["cover_url"],
                    match["site_url"],
                    match["user_score"],
                    match["mean_score"],
                    time.time(),
                    int(book_id),
                ),
            )
        return True

    def _propagate_series_anilist(self, book_id: int) -> int:
        row = self._book(int(book_id))
        key = _manga_series_key(str(row["title"] or Path(str(row["path"] or "")).stem))
        if not key or row["anilist_id"] is None:
            return 0
        changed = 0
        with self.db.connect() as conn:
            siblings = conn.execute("SELECT * FROM manga_books WHERE id<>?", (int(book_id),)).fetchall()
            for sibling in siblings:
                sibling_key = _manga_series_key(str(sibling["title"] or Path(str(sibling["path"] or "")).stem))
                if sibling_key != key:
                    continue
                conn.execute(
                    "UPDATE manga_books SET anilist_id=?,cover_url=?,site_url=?,user_score=?,mean_score=?,updated_at=? "
                    "WHERE id=?",
                    (
                        row["anilist_id"],
                        row["cover_url"],
                        row["site_url"],
                        row["user_score"],
                        row["mean_score"],
                        time.time(),
                        int(sibling["id"]),
                    ),
                )
                changed += 1
        return changed

    def _reconcile_series_anilist(self) -> int:
        """Repair legacy unlinked volumes when their series has one clear link."""

        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM manga_books ORDER BY updated_at DESC,id DESC"
            ).fetchall()
            groups: dict[str, list[Any]] = {}
            for row in rows:
                key = _manga_series_key(
                    str(row["title"] or Path(str(row["path"] or "")).stem)
                )
                if key:
                    groups.setdefault(key, []).append(row)

            changed = 0
            now = time.time()
            for siblings in groups.values():
                linked_ids = {
                    int(row["anilist_id"])
                    for row in siblings
                    if row["anilist_id"] is not None
                }
                # Never guess between conflicting manual links.
                if len(linked_ids) != 1:
                    continue
                donor = next(row for row in siblings if row["anilist_id"] is not None)
                for sibling in siblings:
                    if sibling["anilist_id"] is not None:
                        continue
                    conn.execute(
                        "UPDATE manga_books SET anilist_id=?,cover_url=?,site_url=?,"
                        "user_score=?,mean_score=?,updated_at=? WHERE id=?",
                        (
                            donor["anilist_id"],
                            donor["cover_url"],
                            donor["site_url"],
                            donor["user_score"],
                            donor["mean_score"],
                            now,
                            int(sibling["id"]),
                        ),
                    )
                    changed += 1
        return changed

    def import_file(self, path: Path) -> dict[str, Any]:
        path = path.expanduser().resolve()
        pages = self._pages(path)
        if not pages:
            raise ValueError("The archive contains no readable image pages")
        stat = path.stat()
        fingerprint = hashlib.sha1(
            f"manga-v2:{path}:{stat.st_size}:{stat.st_mtime_ns}:{'|'.join(pages)}".encode("utf-8")
        ).hexdigest()[:24]
        now = time.time()
        stale_artifact_path: Path | None = None
        with self.db.connect() as conn:
            previous = conn.execute(
                "SELECT id,source_fingerprint FROM manga_books WHERE path=?", (str(path),)
            ).fetchone()
            previous_fingerprint = (
                str(previous["source_fingerprint"] or "") if previous is not None else ""
            )
            conn.execute(
                """
                INSERT INTO manga_books(path,title,page_count,position,reading_direction,source_fingerprint,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET title=excluded.title,page_count=excluded.page_count,
                    source_fingerprint=excluded.source_fingerprint,updated_at=excluded.updated_at
                """,
                (str(path), path.stem, len(pages), 0, "rtl", fingerprint, now, now),
            )
            row = conn.execute("SELECT * FROM manga_books WHERE path=?", (str(path),)).fetchone()
            if previous is not None and previous_fingerprint != fingerprint:
                book_id = int(previous["id"])
                conn.execute("DELETE FROM manga_ocr_cache WHERE book_id=?", (book_id,))
                conn.execute(
                    "DELETE FROM state WHERE key LIKE ?",
                    (f"manga_ocr_page_status:v18:{book_id}:%",),
                )
                self._bump_ocr_generation_in_conn(conn, book_id)
                if previous_fingerprint:
                    stale_artifact_path = (
                        self.cache_dir
                        / "manga-ocr"
                        / "artifacts"
                        / f"{previous_fingerprint}-regions-v96p25.json"
                    )
        if stale_artifact_path is not None:
            stale_artifact_path.unlink(missing_ok=True)
        assert row is not None
        self._inherit_series_anilist(int(row["id"]))
        return self._payload(self._book(int(row["id"])))

    @staticmethod
    def discover_image_files(root: Path, *, limit: int = 50000) -> list[Path]:
        """Find loose manga pages recursively without following unrelated files."""
        source = Path(root).expanduser().resolve()
        if not source.is_dir():
            return []
        found: list[Path] = []
        try:
            for item in source.rglob("*"):
                if not item.is_file() or item.suffix.casefold() not in _IMAGE_EXTENSIONS:
                    continue
                found.append(item.resolve())
                if len(found) >= max(1, int(limit)):
                    break
        except (OSError, PermissionError):
            pass
        return sorted(set(found), key=lambda path: _natural_key(str(path)))

    @staticmethod
    def _image_group_root(image: Path) -> Path:
        """Choose the nearest explicit volume folder, otherwise the page folder."""
        parent = image.parent
        for candidate in (parent, *list(parent.parents)[:4]):
            name = unicodedata.normalize("NFKC", candidate.name)
            if re.search(r"(?i)(?:vol(?:ume)?|v)\s*[._ -]*\d{1,3}\s*[-–]\s*\d{1,3}", name):
                continue
            if (
                re.search(r"(?i)(?:^|[\s._\-\[(])(?:vol(?:ume)?|v)\s*[._ -]*0*\d{1,3}(?:\.\d+)?(?:$|[\s._\-\])])", name)
                or re.search(r"第\s*0*\d{1,3}(?:\.\d+)?\s*巻", name)
                or re.search(r"(?:^|[\s._\-])0*\d{1,3}(?:\.\d+)?\s*巻(?:$|[\s._\-])", name)
            ):
                return candidate
        return parent

    @staticmethod
    def _image_group_volume(group_root: Path) -> int | None:
        direct = _manga_volume(group_root.name)
        if direct is not None:
            return direct
        text = unicodedata.normalize("NFKC", group_root.name)
        numbers = {int(match) for match in re.findall(r"(?<!\d)(\d{1,3})(?!\d)", text) if 0 < int(match) <= 300}
        return next(iter(numbers)) if len(numbers) == 1 else None

    @staticmethod
    def _image_series_hint(value: str) -> str:
        text = _strip_manga_release_metadata(value)
        text = re.sub(
            r"(?i)(?:^|[\s._\-\[(])(?:vol(?:ume)?|v)\s*[._ -]*0*\d{1,3}\s*[-–]\s*0*\d{1,3}(?:$|[\s._\-\])])",
            " ", text,
        )
        text = re.sub(r"[\s._-]+$", "", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _image_series_candidate(group_root: Path) -> str:
        candidate = _manga_series_title(_strip_manga_release_metadata(group_root.name)).strip()
        if not candidate or candidate.isdigit():
            return ""
        return candidate

    @staticmethod
    def _canonical_image_series(group_roots: list[Path], *, source_root: Path | None = None) -> str:
        counts: dict[str, int] = {}
        for group_root in group_roots:
            candidate = MangaService._image_series_candidate(group_root)
            if candidate:
                counts[candidate] = counts.get(candidate, 0) + 1
        if counts:
            candidate, count = max(counts.items(), key=lambda item: (item[1], len(item[0])))
            if count >= 2:
                return candidate
        if source_root is not None:
            hint = MangaService._image_series_hint(source_root.name)
            if hint:
                return hint
        if counts:
            return max(counts, key=lambda item: (counts[item], len(item)))
        return ""

    def import_image_groups(self, paths: list[Path], *, source_root: Path | None = None) -> list[dict[str, Any]]:
        """Import loose pages as one volume per folder and one stable series."""
        # pudge-v0.7.23-nested-manga-series-v2
        unique = sorted(
            {Path(path).expanduser().resolve() for path in paths if Path(path).expanduser().is_file() and Path(path).suffix.casefold() in _IMAGE_EXTENSIONS},
            key=lambda path: _natural_key(str(path)),
        )
        if not unique:
            raise ValueError("No readable image files were dropped")
        groups: dict[Path, list[Path]] = {}
        for image in unique:
            groups.setdefault(self._image_group_root(image), []).append(image)
        group_roots = sorted(groups, key=lambda path: _natural_key(str(path)))
        canonical = self._canonical_image_series(
            group_roots,
            source_root=Path(source_root).expanduser().resolve() if source_root is not None else None,
        )
        japanese_series = bool(re.search(r"[ぁ-ゟ゠-ヿ一-鿿]", canonical))
        books: list[dict[str, Any]] = []
        used_volumes: set[int] = set()
        for group_root in group_roots:
            volume = self._image_group_volume(group_root)
            if volume is not None and volume in used_volumes:
                volume = None
            if volume is not None:
                used_volumes.add(volume)
            if canonical and volume is not None:
                title = f"{canonical} 第{volume:02d}巻" if japanese_series else f"{canonical} Vol. {volume}"
            else:
                title = _strip_manga_release_metadata(group_root.name) or group_root.name
            books.append(self.import_images(groups[group_root], title=title))
        return books

    def import_images(self, paths: list[Path], *, title: str | None = None) -> dict[str, Any]:
        images = [Path(path).expanduser().resolve() for path in paths]
        images = [path for path in images if path.is_file() and path.suffix.casefold() in _IMAGE_EXTENSIONS]
        if not images:
            raise ValueError("No readable image files were dropped")
        images = sorted(set(images), key=lambda path: _natural_key(str(path)))
        signature_rows: list[str] = []
        for path in images:
            stat = path.stat()
            signature_rows.append(f"{path}:{stat.st_size}:{stat.st_mtime_ns}")
        digest = hashlib.sha1("|".join(signature_rows).encode("utf-8")).hexdigest()[:20]
        target_dir = self.cache_dir / "manga-imports"
        target_dir.mkdir(parents=True, exist_ok=True)
        logical_title = _strip_manga_release_metadata(str(title or images[0].parent.name)) or str(title or images[0].parent.name)
        safe_title = re.sub(r"[^\w .()\[\]-]+", "_", logical_title, flags=re.UNICODE).strip() or "Dropped manga"
        target = target_dir / f"{safe_title}-{digest}.cbz"
        legacy_targets = sorted(
            [candidate for candidate in target_dir.glob(f"*-{digest}.cbz") if candidate.resolve() != target.resolve()],
            key=lambda path: _natural_key(path.name),
        )
        if not target.exists() and legacy_targets:
            legacy = legacy_targets[0]
            with self.db.connect() as conn:
                legacy_row = conn.execute("SELECT id FROM manga_books WHERE path=?", (str(legacy.resolve()),)).fetchone()
                target_row = conn.execute("SELECT id FROM manga_books WHERE path=?", (str(target.resolve()),)).fetchone()
                if legacy_row is not None and target_row is None:
                    legacy.replace(target)
                    conn.execute(
                        "UPDATE manga_books SET path=?,title=?,updated_at=? WHERE id=?",
                        (str(target.resolve()), logical_title, time.time(), int(legacy_row["id"])),
                    )
        if not target.is_file():
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for index, image in enumerate(images, 1):
                    source_stem = re.sub(
                        r"[^\w.\-]+",
                        "_",
                        image.stem,
                        flags=re.UNICODE,
                    ).strip("._")[:120] or f"page-{index:04d}"
                    archive.write(
                        image,
                        arcname=f"{index:04d}__{source_stem}{image.suffix.casefold()}",
                    )
        book = self.import_file(target)
        book_id = int(book["id"])
        with self.db.connect() as conn:
            conn.execute("UPDATE manga_books SET title=?,updated_at=? WHERE id=?", (logical_title, time.time(), book_id))
        self._inherit_series_anilist(book_id)
        return self._payload(self._book(book_id))

    def remove_books(self, book_ids: list[int]) -> int:
        ids = sorted({int(value) for value in book_ids if int(value) > 0})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT id,path FROM manga_books WHERE id IN ({placeholders})", ids
            ).fetchall()
            existing_ids = [int(row["id"]) for row in rows]
            if not existing_ids:
                return 0
            artifact_paths = {
                int(row["id"]): self._ocr_artifact_path(int(row["id"])) for row in rows
            }
            placeholders = ",".join("?" for _ in existing_ids)
            conn.execute(f"DELETE FROM manga_ocr_cache WHERE book_id IN ({placeholders})", existing_ids)
            conn.executemany(
                "DELETE FROM state WHERE key=?",
                [(self._manual_corrections_state_key(book_id),) for book_id in existing_ids],
            )
            conn.executemany(
                "DELETE FROM state WHERE key=?",
                [(self._manual_order_state_key(book_id),) for book_id in existing_ids],
            )
            conn.executemany(
                "DELETE FROM state WHERE key=?",
                [(self._manual_geometry_state_key(book_id),) for book_id in existing_ids],
            )
            conn.executemany(
                "DELETE FROM state WHERE key=?",
                [(self._manual_suppressions_state_key(book_id),) for book_id in existing_ids],
            )
            conn.executemany(
                "DELETE FROM state WHERE key=?",
                [(self._manual_additions_state_key(book_id),) for book_id in existing_ids],
            )
            conn.execute(f"DELETE FROM manga_books WHERE id IN ({placeholders})", existing_ids)
            # P10: invalidate in-flight OCR so a late publish can never land in
            # a re-imported book that reuses this id with the same file.
            for book_id in existing_ids:
                self._bump_ocr_generation_in_conn(conn, book_id)
        generated_root = (self.cache_dir / "manga-imports").resolve()
        for row in rows:
            try:
                source = Path(str(row["path"])).expanduser().resolve()
                if generated_root in source.parents:
                    source.unlink(missing_ok=True)
            except OSError:
                pass
            artifact_paths[int(row["id"])].unlink(missing_ok=True)
        return len(existing_ids)

    def remove_series(self, book_id: int) -> int:
        selected = self._book(int(book_id))
        selected_key = _manga_series_key(
            str(selected["title"] or Path(str(selected["path"] or "")).stem)
        )
        with self.db.connect() as conn:
            rows = conn.execute("SELECT id,title,path FROM manga_books").fetchall()
            ids = [
                int(row["id"])
                for row in rows
                if _manga_series_key(
                    str(row["title"] or Path(str(row["path"] or "")).stem)
                ) == selected_key
            ]
        return self.remove_books(ids or [int(book_id)])

    def _cover_source(self, row: Any) -> tuple[Path, str, str]:
        path = Path(str(row["path"]))
        stat = path.stat()
        pages = self._pages(path)
        if not pages:
            raise ValueError("Manga has no pages")
        page_name = str(pages[0])
        revision = hashlib.sha1(
            f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{page_name}".encode("utf-8")
        ).hexdigest()[:20]
        return path, page_name, revision

    def _local_cover_data_uri(self, row: Any) -> str:
        try:
            path, page_name, revision = self._cover_source(row)
            cover_dir = self.cache_dir / "manga-covers"
            cover_dir.mkdir(parents=True, exist_ok=True)
            target = cover_dir / f"{revision}.jpg"
            if not target.is_file() or target.stat().st_size <= 0:
                with zipfile.ZipFile(path) as archive:
                    image = Image.open(io.BytesIO(archive.read(page_name))).convert("RGB")
                image.thumbnail((320, 480))
                image.save(target, format="JPEG", quality=82, optimize=True)
            data = target.read_bytes()
            return f"data:image/jpeg;base64,{base64.b64encode(data).decode('ascii')}"
        except (OSError, ValueError, zipfile.BadZipFile):
            return ""

    def cover_ref(self, book_id: int, *, asset_base: str) -> CoverRef:
        row = self._book(int(book_id))
        path, page_name, revision = self._cover_source(row)
        del path, page_name
        return CoverRef(
            asset_id=f"manga:{int(book_id)}",
            source_revision=revision,
            thumbnail_url="",
            preview_url=f"{asset_base}/api/covers/manga/{int(book_id)}/{revision}",
            source_kind="local_page",
        )

    def cover_preview_asset(
        self, book_id: int, *, expected_revision: str = ""
    ) -> tuple[Path, str, str]:
        row = self._book(int(book_id))
        path, page_name, revision = self._cover_source(row)
        if expected_revision and str(expected_revision) != revision:
            raise KeyError("stale manga cover revision")
        media_type = mimetypes.guess_type(page_name)[0] or "image/jpeg"
        suffix = Path(page_name).suffix.casefold()
        if suffix not in _IMAGE_EXTENSIONS:
            suffix = mimetypes.guess_extension(media_type) or ".img"
        preview_dir = self.cache_dir / "manga-cover-previews"
        preview_dir.mkdir(parents=True, exist_ok=True)
        target = preview_dir / f"v1-{revision}-original{suffix}"
        if not target.is_file() or target.stat().st_size <= 0:
            with zipfile.ZipFile(path) as archive:
                raw = archive.read(page_name)
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=preview_dir,
                    prefix=f".{target.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    handle.write(raw)
                    temporary = Path(handle.name)
                temporary.replace(target)
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink(missing_ok=True)
        return target, media_type, revision

    def _remote_cover_target(self, url: str) -> Path:
        digest = hashlib.sha256(str(url).encode("utf-8")).hexdigest()[:24]
        return self.cache_dir / "manga-covers" / f"anilist-{digest}.jpg"

    @staticmethod
    def _cover_data_uri(path: Path) -> str:
        try:
            data = path.read_bytes()
        except OSError:
            return ""
        if not data:
            return ""
        return f"data:image/jpeg;base64,{base64.b64encode(data).decode('ascii')}"

    def _cache_remote_cover(self, url: str) -> None:
        target = self._remote_cover_target(url)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            response = httpx.get(str(url), timeout=15, follow_redirects=True)
            response.raise_for_status()
            image = Image.open(io.BytesIO(response.content)).convert("RGB")
            image.thumbnail((480, 720))
            temporary = target.with_suffix(".tmp.jpg")
            image.save(temporary, format="JPEG", quality=86, optimize=True)
            temporary.replace(target)
        except (OSError, ValueError, httpx.HTTPError):
            pass
        finally:
            with self._cover_cache_lock:
                self._cover_cache_inflight.discard(str(url))

    def _cached_remote_cover_data_uri(self, url: str) -> str:
        if not url:
            return ""
        target = self._remote_cover_target(url)
        cached = self._cover_data_uri(target)
        if cached:
            return cached
        with self._cover_cache_lock:
            if url in self._cover_cache_inflight:
                return ""
            self._cover_cache_inflight.add(url)
        threading.Thread(
            target=self._cache_remote_cover,
            args=(url,),
            name="manga-cover-cache",
            daemon=True,
        ).start()
        return ""

    def _payload(self, row: Any) -> dict[str, Any]:
        remote_cover = str(row["cover_url"] or "")
        title = str(row["title"] or "")
        path = str(row["path"] or "")
        metadata_source = f"{title} {Path(path).stem}"
        series_title = _manga_series_title(title or Path(path).stem) or title or Path(path).stem
        volume = _manga_volume(metadata_source) or 1
        local_cover = self._local_cover_data_uri(row)
        # AniList exposes a clean series cover, which normally corresponds to
        # volume 1. Later volumes must keep their own physical cover instead of
        # inheriting that same image across the whole local series.
        cached_remote = self._cached_remote_cover_data_uri(remote_cover) if volume == 1 else ""
        selected_cover = cached_remote or local_cover
        return {
            "id": int(row["id"]),
            "path": path,
            "title": title,
            "series_title": series_title,
            "series_key": _manga_series_key(series_title) or f"book:{int(row['id'])}",
            "volume": volume,
            "page_count": int(row["page_count"] or 0),
            "position": int(row["position"] or 0),
            # pudge-v0.7.23-manga-read-pages-v1
            "read_pages": int(row["read_pages"] or 0),
            "reading_direction": str(row["reading_direction"] or "rtl"),
            "anilist_id": int(row["anilist_id"]) if row["anilist_id"] is not None else None,
            "site_url": str(row["site_url"] or ""),
            "user_score": float(row["user_score"]) if row["user_score"] is not None else None,
            # pudge-v0.7.23-manga-mean-score-v1
            "mean_score": float(row["mean_score"]) if row["mean_score"] is not None else None,
            "cover_url": selected_cover,
            "remote_cover_url": remote_cover,
            "cover_source": "anilist_cache" if cached_remote else "first_page",
            "updated_at": float(row["updated_at"] or 0),
        }

    def bind_anilist(
        self,
        book_id: int,
        media_id: int,
        *,
        cover_url: str = "",
        site_url: str = "",
        user_score: float | None = None,
        mean_score: float | None = None,
    ) -> dict[str, Any]:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE manga_books SET anilist_id=?,cover_url=?,site_url=?,user_score=?,mean_score=?,updated_at=? "
                "WHERE id=?",
                (
                    int(media_id),
                    str(cover_url or ""),
                    str(site_url or ""),
                    user_score,
                    mean_score,
                    time.time(),
                    int(book_id),
                ),
            )
            row = conn.execute("SELECT * FROM manga_books WHERE id=?", (int(book_id),)).fetchone()
        if row is None:
            raise KeyError(f"Unknown manga id={book_id}")
        self._propagate_series_anilist(int(book_id))
        return self._payload(self._book(int(book_id)))

    def unbind_anilist(self, book_id: int) -> dict[str, Any]:
        """Remove AniList metadata from every local volume in this series."""

        row = self._book(int(book_id))
        key = _manga_series_key(
            str(row["title"] or Path(str(row["path"] or "")).stem)
        )
        with self.db.connect() as conn:
            siblings = conn.execute("SELECT * FROM manga_books").fetchall()
            ids = [
                int(sibling["id"])
                for sibling in siblings
                if _manga_series_key(
                    str(
                        sibling["title"]
                        or Path(str(sibling["path"] or "")).stem
                    )
                )
                == key
            ]
            if not ids:
                ids = [int(book_id)]
            now = time.time()
            conn.executemany(
                "UPDATE manga_books SET anilist_id=NULL,cover_url='',site_url='',"
                "user_score=NULL,mean_score=NULL,updated_at=? WHERE id=?",
                [(now, sibling_id) for sibling_id in ids],
            )
        return self._payload(self._book(int(book_id)))

    def set_score(self, book_id: int, score: float) -> dict[str, Any]:
        row = self._book(int(book_id))
        now = time.time()
        with self.db.connect() as conn:
            if row["anilist_id"] is not None:
                conn.execute(
                    "UPDATE manga_books SET user_score=?,updated_at=? WHERE anilist_id=?",
                    (float(score), now, int(row["anilist_id"])),
                )
            else:
                conn.execute(
                    "UPDATE manga_books SET user_score=?,updated_at=? WHERE id=?",
                    (float(score), now, int(book_id)),
                )
        return self._payload(self._book(int(book_id)))

    def ocr_cache_status(self, book_id: int) -> dict[str, Any]:
        """Return persisted, loadable OCR page state for one volume.

        ``completed_pages`` is deliberately stricter than the legacy
        ``cached_pages`` counter: only ready/verified-empty pages count as
        completed. Partial/failed pages remain retryable failures, and a failed
        page is visible even when no cache row was written.
        """
        row = self._book(int(book_id))
        total = max(0, int(row["page_count"] or 0))
        status_prefix = f"manga_ocr_page_status:v18:{int(book_id)}:"
        with self.db.connect() as conn:
            current_fingerprint = str(row["source_fingerprint"] or "")
            current_generation = self._state_int_from_conn(
                conn, self._ocr_generation_state_key(int(book_id))
            )
            cache_rows = conn.execute(
                "SELECT page_index,text FROM manga_ocr_cache WHERE book_id=? AND region_key=?",
                (int(book_id), _REGION_CACHE_KEY),
            ).fetchall()
            status_rows = conn.execute(
                "SELECT key,value FROM state WHERE key LIKE ?",
                (f"{status_prefix}%",),
            ).fetchall()
        cache_by_page = {int(item["page_index"]): item for item in cache_rows}
        status_by_page: dict[int, dict[str, Any]] = {}
        for item in status_rows:
            try:
                page_index = int(str(item["key"])[len(status_prefix):])
                payload = json.loads(str(item["value"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                payload_fingerprint = str(payload.get("source_fingerprint") or "")
                payload_generation = payload.get("generation")
                if payload_fingerprint and payload_fingerprint != current_fingerprint:
                    continue
                if payload_generation is not None:
                    try:
                        if int(payload_generation) != current_generation:
                            continue
                    except (TypeError, ValueError):
                        continue
                elif current_generation > 0:
                    continue
                status_by_page[page_index] = dict(payload)

        ready_pages = 0
        empty_pages = 0
        partial_pages = 0
        failed_pages = 0
        unknown_pages = 0
        for page_index in range(total):
            item = cache_by_page.get(page_index)
            page_state = status_by_page.get(page_index, {})
            status = str(page_state.get("status") or "")
            if not status and item is not None:
                try:
                    legacy_regions = json.loads(str(item["text"] or "[]"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    legacy_regions = []
                status = "ready" if isinstance(legacy_regions, list) and legacy_regions else "unknown"
            if status in {"ready", "empty_verified"} and item is None:
                status = "unknown"
            if not status:
                continue
            if status == "ready":
                ready_pages += 1
            elif status == "empty_verified":
                empty_pages += 1
            elif status == "partial":
                partial_pages += 1
            elif status == "failed":
                failed_pages += 1
            else:
                unknown_pages += 1

        completed_pages = max(0, min(ready_pages + empty_pages, total))
        failed_total = max(0, min(partial_pages + failed_pages, total - completed_pages))
        not_started_pages = max(0, total - completed_pages - failed_total)
        # Compatibility field used by older UI/tests. A partial result exists in
        # cache, but is intentionally *not* counted as completed_pages.
        cached_pages = max(0, min(completed_pages + partial_pages, total))
        complete = bool(total > 0 and completed_pages >= total)
        return {
            "book_id": int(book_id),
            "cached_pages": cached_pages,
            "completed_pages": completed_pages,
            "ready_pages": ready_pages,
            "empty_pages": empty_pages,
            "partial_pages": partial_pages,
            "failed_pages": failed_total,
            "hard_failed_pages": failed_pages,
            "unknown_pages": unknown_pages,
            "not_started_pages": not_started_pages,
            "total_pages": total,
            "complete": complete,
        }

    def set_mean_scores(self, scores: dict[int, float]) -> int:
        # pudge-v0.7.23-manga-mean-score-backfill-v1
        normalized = {
            int(media_id): float(score)
            for media_id, score in (scores or {}).items()
            if int(media_id) > 0 and score is not None
        }
        if not normalized:
            return 0
        changed = 0
        now = time.time()
        with self.db.connect() as conn:
            for media_id, score in normalized.items():
                cursor = conn.execute(
                    "UPDATE manga_books SET mean_score=?,updated_at=? "
                    "WHERE anilist_id=? AND (mean_score IS NULL OR mean_score<>?)",
                    (score, now, media_id, score),
                )
                changed += max(0, int(cursor.rowcount or 0))
        return changed

    def state(self) -> dict[str, Any]:
        self._reconcile_series_anilist()
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM manga_books ORDER BY updated_at DESC,id DESC").fetchall()
        books: list[dict[str, Any]] = []
        for row in rows:
            payload = self._payload(row)
            status = self.ocr_cache_status(int(payload["id"]))
            payload["ocr_cached_pages"] = int(status["cached_pages"])
            payload["ocr_complete"] = bool(status["complete"])
            books.append(payload)
        return {
            "books": books,
            "ocr_available": self.ocr_available(),
            "reader_preferences": self.reader_preferences(),
        }

    def _book(self, book_id: int) -> Any:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM manga_books WHERE id=?", (int(book_id),)).fetchone()
        if row is None:
            raise KeyError(f"Unknown manga id={book_id}")
        return row

    def page(self, book_id: int, page_index: int) -> dict[str, Any]:
        row = self._book(book_id)
        path = Path(str(row["path"]))
        pages = self._pages(path)
        index = max(0, min(int(page_index), len(pages) - 1))
        # pudge-v0.7.23-manga-spread-detection-v1
        page_name = pages[index]
        with zipfile.ZipFile(path) as archive:
            data = archive.read(page_name)
        spread = False
        range_match = re.search(
            r"(?:^|[^0-9])(\d{1,4})\s*[-–—]\s*(\d{1,4})(?:[^0-9]|$)",
            Path(page_name).stem,
        )
        if range_match:
            spread = int(range_match.group(2)) == int(range_match.group(1)) + 1
        if not spread:
            try:
                with Image.open(io.BytesIO(data)) as source_image:
                    width, height = source_image.size
                spread = bool(width > 0 and height > 0 and width >= height * 1.15)
            except (OSError, ValueError):
                spread = False
        media_type = mimetypes.guess_type(page_name)[0] or "image/jpeg"
        # Page fetch/preload is intentionally read-only for progress.
        return {
            "book_id": int(book_id),
            "page_index": index,
            "page_count": len(pages),
            "name": page_name,
            "spread": spread,
            "data_uri": f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}",
        }

    def set_position(self, book_id: int, page_index: int) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE manga_books SET position=?,updated_at=? WHERE id=?",
                (max(0, int(page_index)), time.time(), int(book_id)),
            )

    def reset_progress(self, book_id: int) -> dict[str, Any]:
        """Reset reading progress only; preserve AniList/library metadata."""
        self._book(int(book_id))
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE manga_books SET position=0,read_pages=0,updated_at=? WHERE id=?",
                (time.time(), int(book_id)),
            )
        return self._payload(self._book(int(book_id)))

    def mark_read(self, book_id: int, page_index: int) -> dict[str, Any]:
        """Mark pages through page_index completed without ever regressing."""
        row = self._book(int(book_id))
        total = max(0, int(row["page_count"] or 0))
        if total <= 0:
            return self._payload(row)
        target = max(0, min(total, int(page_index) + 1))
        current = max(0, int(row["read_pages"] or 0))
        if target <= current:
            return self._payload(row)
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE manga_books SET read_pages=?,updated_at=? WHERE id=?",
                (target, time.time(), int(book_id)),
            )
        return self._payload(self._book(int(book_id)))

    def _vision_text_regions(self, image: Image.Image) -> list[dict[str, Any]]:
        """Detect manga text geometry with several Vision-friendly image passes.

        MangaOCR is a recognizer, not a detector.  Keep detection separate and
        combine the original page with contrast and inverted passes so stylised
        vertical bubbles are not lost before recognition even starts.
        """

        if sys.platform != "darwin":
            return []
        try:
            import Vision  # type: ignore
            from Foundation import NSURL  # type: ignore
        except ImportError:
            return []

        work_dir = self.cache_dir / "manga-text-regions"
        work_dir.mkdir(parents=True, exist_ok=True)
        rgb = image.convert("RGB")
        grayscale = ImageOps.grayscale(rgb)
        contrast = ImageEnhance.Contrast(ImageOps.autocontrast(grayscale)).enhance(1.35)
        variants = [
            ("vision-original", rgb),
            ("vision-contrast", contrast.convert("RGB")),
            ("vision-inverted", ImageOps.invert(contrast).convert("RGB")),
        ]
        regions: list[dict[str, Any]] = []
        temporary_paths: list[Path] = []

        def append_box(
            observation: Any,
            *,
            text: str,
            confidence: float,
            detector: str,
            recognized_text: Any | None = None,
        ) -> None:
            x, y, width, height, geometry_source = _vision_observation_bounds(observation)
            geometry_segments = (
                _vision_recognized_text_segments(recognized_text, text)
                if recognized_text is not None and width >= height * 1.05
                else []
            )
            if geometry_segments:
                geometry_source = "accurate-range-boxes-v2"
            else:
                geometry_segments = _vision_observation_segments(observation)
            if width <= 0.001 or height <= 0.001:
                return
            candidate = {
                "text": str(text or "").strip(),
                "raw_text": str(text or "").strip(),
                "confidence": round(max(0.0, min(1.0, float(confidence))), 4),
                "detector": detector,
                "recognizer": "apple-vision",
                "geometry_source": geometry_source,
                "x": round(x, 6),
                "y": round(y, 6),
                "width": round(width, 6),
                "height": round(height, 6),
            }
            if geometry_segments:
                candidate["segments"] = geometry_segments
            overlap = next(
                (item for item in regions if _box_overlap(candidate, item) >= 0.78),
                None,
            )
            if overlap is None:
                regions.append(candidate)
                return
            if geometry_segments:
                overlap.update(
                    {
                        "x": candidate["x"],
                        "y": candidate["y"],
                        "width": candidate["width"],
                        "height": candidate["height"],
                        "geometry_source": geometry_source,
                    }
                )
                if geometry_segments:
                    overlap["segments"] = geometry_segments
            if candidate["confidence"] > float(overlap.get("confidence") or 0.0):
                overlap.update(candidate)
            elif candidate["text"] and not str(overlap.get("text") or ""):
                overlap["text"] = candidate["text"]
                overlap["raw_text"] = candidate["raw_text"]
            detectors = set(str(overlap.get("detector") or "").split("+"))
            detectors.add(detector)
            overlap["detector"] = "+".join(sorted(item for item in detectors if item))

        try:
            for detector_name, variant in variants:
                with tempfile.NamedTemporaryFile(suffix=".png", dir=work_dir, delete=False) as handle:
                    input_path = Path(handle.name)
                temporary_paths.append(input_path)
                variant.save(input_path, format="PNG")
                request = Vision.VNRecognizeTextRequest.alloc().init()
                request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
                request.setRecognitionLanguages_(["ja-JP"])
                request.setUsesLanguageCorrection_(True)
                if hasattr(request, "setMinimumTextHeight_"):
                    request.setMinimumTextHeight_(0.003)
                handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(
                    NSURL.fileURLWithPath_(str(input_path)), None
                )
                success, _error = handler.performRequests_error_([request], None)
                if not success:
                    continue
                for observation in request.results() or []:
                    candidates = observation.topCandidates_(1)
                    if not candidates:
                        continue
                    candidate = candidates[0]
                    text = str(candidate.string()).strip()
                    if not text:
                        continue
                    try:
                        confidence = float(candidate.confidence())
                    except Exception:
                        confidence = 0.5
                    append_box(
                        observation,
                        text=text,
                        confidence=confidence,
                        detector=detector_name,
                        recognized_text=candidate,
                    )

            # The geometry-only request catches boxes whose provisional text
            # recognition failed.  Run it once on the original page and let
            # MangaOCR fill the text from the resulting crops.
            detector_class = getattr(Vision, "VNDetectTextRectanglesRequest", None)
            if detector_class is not None and temporary_paths:
                # Geometry-only detection used to run only on the original page.
                # That misses white-on-black narration boxes and low-contrast
                # bubbles even though the recognition pass already has contrast
                # and inverted variants. Run the same rectangle detector on all
                # prepared variants and merge/dedupe their geometry afterwards.
                for rectangle_index, rectangle_path in enumerate(temporary_paths):
                    detector = detector_class.alloc().init()
                    if hasattr(detector, "setReportCharacterBoxes_"):
                        detector.setReportCharacterBoxes_(True)
                    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(
                        NSURL.fileURLWithPath_(str(rectangle_path)), None
                    )
                    success, _error = handler.performRequests_error_([detector], None)
                    if not success:
                        continue
                    rectangle_detector = (
                        f"vision-rectangles-{variants[rectangle_index][0].removeprefix('vision-')}"
                    )
                    for observation in detector.results() or []:
                        append_box(
                            observation,
                            text="",
                            confidence=0.25,
                            detector=rectangle_detector,
                        )
        finally:
            for path in temporary_paths:
                path.unlink(missing_ok=True)

        merged = _merge_text_regions(regions)
        if merged:
            return merged
        # Detector miss is a verified empty region set; never OCR the whole artwork.
        return []

    @staticmethod
    def _ocr_generation_state_key(book_id: int) -> str:
        return f"manga_ocr_generation:v1:{int(book_id)}"

    @staticmethod
    def _ocr_revision_state_key(book_id: int) -> str:
        return f"manga_ocr_revision:v1:{int(book_id)}"

    def _ocr_publish_lock(self, book_id: int) -> threading.RLock:
        key = int(book_id)
        with self._ocr_publish_locks_guard:
            lock = self._ocr_publish_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._ocr_publish_locks[key] = lock
            return lock

    @staticmethod
    def _state_int_from_conn(conn: Any, key: str) -> int:
        row = conn.execute("SELECT value FROM state WHERE key=?", (str(key),)).fetchone()
        if row is None:
            return 0
        try:
            return max(0, int(str(row["value"] or "0")))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _set_state_in_conn(conn: Any, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO state(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (str(key), str(value), time.time()),
        )

    def _ocr_context_from_conn(
        self, conn: Any, book_id: int
    ) -> tuple[str, int, int]:
        row = conn.execute(
            "SELECT source_fingerprint FROM manga_books WHERE id=?", (int(book_id),)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown manga id={int(book_id)}")
        fingerprint = str(row["source_fingerprint"] or "")
        generation = self._state_int_from_conn(
            conn, self._ocr_generation_state_key(int(book_id))
        )
        revision = self._state_int_from_conn(
            conn, self._ocr_revision_state_key(int(book_id))
        )
        return fingerprint, generation, revision

    def _ocr_context(self, book_id: int) -> tuple[str, int, int]:
        with self.db.connect() as conn:
            return self._ocr_context_from_conn(conn, int(book_id))

    def _bump_ocr_generation_in_conn(self, conn: Any, book_id: int) -> tuple[int, int]:
        generation = self._state_int_from_conn(
            conn, self._ocr_generation_state_key(int(book_id))
        ) + 1
        revision = self._state_int_from_conn(
            conn, self._ocr_revision_state_key(int(book_id))
        ) + 1
        self._set_state_in_conn(
            conn, self._ocr_generation_state_key(int(book_id)), str(generation)
        )
        self._set_state_in_conn(
            conn, self._ocr_revision_state_key(int(book_id)), str(revision)
        )
        return generation, revision

    def _commit_ocr_page_updates(
        self,
        book_id: int,
        *,
        source_fingerprint: str,
        generation: int,
        updates: list[tuple[int, list[dict[str, Any]] | None, str, str, bool]],
    ) -> bool:
        """Atomically publish cache rows and page statuses for one OCR generation."""

        book_id = int(book_id)
        if not updates:
            return True
        with self._ocr_publish_lock(book_id):
            with self.db.connect() as conn:
                current_fingerprint, current_generation, current_revision = (
                    self._ocr_context_from_conn(conn, book_id)
                )
                if (
                    current_fingerprint != str(source_fingerprint)
                    or current_generation != int(generation)
                ):
                    return False

                now = time.time()
                for page_index, regions, status, reason, retryable in updates:
                    if regions is not None:
                        conn.execute(
                            "INSERT OR REPLACE INTO manga_ocr_cache"
                            "(book_id,page_index,region_key,text,updated_at) VALUES(?,?,?,?,?)",
                            (
                                book_id,
                                int(page_index),
                                _REGION_CACHE_KEY,
                                json.dumps(regions, ensure_ascii=False),
                                now,
                            ),
                        )
                    payload = json.dumps(
                        {
                            "status": str(status),
                            "reason": str(reason),
                            "retryable": bool(retryable),
                            "source_fingerprint": current_fingerprint,
                            "generation": current_generation,
                            "updated_at": now,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    self._set_state_in_conn(
                        conn,
                        self._ocr_page_status_state_key(book_id, int(page_index)),
                        payload,
                    )

                revision = current_revision + 1
                self._set_state_in_conn(
                    conn, self._ocr_revision_state_key(book_id), str(revision)
                )

            incremental = False
            if len(updates) == 1 and updates[0][1] is not None:
                page_index, regions, _status, _reason, _retryable = updates[0]
                incremental = self._update_ocr_artifact_page(
                    book_id,
                    page_index=int(page_index),
                    regions=[
                        dict(item) for item in (regions or []) if isinstance(item, dict)
                    ],
                    source_fingerprint=current_fingerprint,
                    generation=int(generation),
                    revision=int(revision),
                )
            if not incremental:
                self._rebuild_ocr_artifact(
                    book_id,
                    expected_generation=int(generation),
                    expected_revision=int(revision),
                )
            return True

    def _update_ocr_artifact_page(
        self,
        book_id: int,
        *,
        page_index: int,
        regions: list[dict[str, Any]],
        source_fingerprint: str,
        generation: int,
        revision: int,
    ) -> bool:
        """Publish one OCR page without rereading every cached archive page."""

        book_id = int(book_id)
        artifact_path = self._ocr_artifact_path(book_id)
        previous = read_artifact(artifact_path)
        if previous is None or previous.get("migrated_from_schema"):
            return False
        source = previous.get("source") if isinstance(previous.get("source"), dict) else {}
        try:
            previous_generation = int(previous.get("generation"))
            previous_revision = int(previous.get("revision"))
        except (TypeError, ValueError):
            return False
        if (
            str(source.get("fingerprint") or "") != str(source_fingerprint)
            or previous_generation != int(generation)
            or previous_revision != int(revision) - 1
        ):
            return False

        row = self._book(book_id)
        archive_path = Path(str(row["path"]))
        page_names = self._pages(archive_path)
        if not 0 <= int(page_index) < len(page_names):
            return False
        try:
            with zipfile.ZipFile(archive_path) as archive:
                with Image.open(
                    io.BytesIO(archive.read(page_names[int(page_index)]))
                ) as source_image:
                    width, height = source_image.size
        except (OSError, ValueError, KeyError):
            width, height = 0, 0

        pages_by_index = {
            int(page.get("page_index") or 0): dict(page)
            for page in previous.get("pages") or []
            if isinstance(page, dict)
        }
        pages_by_index[int(page_index)] = normalize_page(
            int(page_index),
            regions,
            name=page_names[int(page_index)],
            width=width,
            height=height,
        )
        pages = [pages_by_index[index] for index in sorted(pages_by_index)]
        detector_names = sorted(
            {
                str(region.get("detector") or "unknown")
                for page in pages
                for region in page.get("regions") or []
                if isinstance(region, dict)
            }
        )
        recognizer_names = sorted(
            {
                str(region.get("recognizer") or "unknown")
                for page in pages
                for region in page.get("regions") or []
                if isinstance(region, dict)
            }
        )
        artifact = build_artifact(
            source_fingerprint=str(source_fingerprint),
            title=str(row["title"] or ""),
            page_count=int(row["page_count"] or 0),
            pages=pages,
            detector="+".join(detector_names) or "unknown",
            recognizer="+".join(recognizer_names) or "unknown",
        )
        artifact["generation"] = int(generation)
        artifact["revision"] = int(revision)

        current_fingerprint, current_generation, current_revision = self._ocr_context(
            book_id
        )
        if (
            current_fingerprint != str(source_fingerprint)
            or current_generation != int(generation)
            or current_revision != int(revision)
        ):
            return False
        write_artifact(artifact_path, artifact)
        return True

    def _ocr_artifact_path(self, book_id: int) -> Path:
        row = self._book(int(book_id))
        fingerprint = str(row["source_fingerprint"] or f"book-{int(book_id)}")
        return self.cache_dir / "manga-ocr" / "artifacts" / f"{fingerprint}-regions-v96p27.json"

    def _load_ocr_artifact(self, book_id: int) -> dict[str, Any] | None:
        book_id = int(book_id)
        artifact = read_artifact(self._ocr_artifact_path(book_id))
        if artifact is None:
            return None
        fingerprint, generation, revision = self._ocr_context(book_id)
        source = artifact.get("source") if isinstance(artifact.get("source"), dict) else {}
        artifact_fingerprint = str(source.get("fingerprint") or "")
        artifact_generation = artifact.get("generation")
        artifact_revision = artifact.get("revision")
        generation_matches = (
            artifact_generation is None and generation == 0
        ) or (
            artifact_generation is not None
            and int(artifact_generation) == int(generation)
        )
        revision_matches = (
            artifact_revision is None and revision == 0
        ) or (
            artifact_revision is not None
            and int(artifact_revision) == int(revision)
        )
        if (
            artifact_fingerprint != fingerprint
            or not generation_matches
            or not revision_matches
        ):
            return self._rebuild_ocr_artifact(book_id)
        if not artifact.get("migrated_from_schema"):
            return artifact
        with self.db.connect() as conn:
            cached = int(
                conn.execute(
                    "SELECT COUNT(*) FROM manga_ocr_cache WHERE book_id=? AND region_key=?",
                    (book_id, _REGION_CACHE_KEY),
                ).fetchone()[0]
            )
        if cached <= 0:
            return artifact
        return self._rebuild_ocr_artifact(book_id)

    def _rebuild_ocr_artifact(
        self,
        book_id: int,
        *,
        expected_generation: int | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        book_id = int(book_id)
        with self._ocr_publish_lock(book_id):
            last_artifact: dict[str, Any] | None = None
            for _attempt in range(2):
                row = self._book(book_id)
                archive_path = Path(str(row["path"]))
                page_names = self._pages(archive_path)
                with self.db.connect() as conn:
                    fingerprint, generation, revision = self._ocr_context_from_conn(
                        conn, book_id
                    )
                    if (
                        expected_generation is not None
                        and generation != int(expected_generation)
                    ) or (
                        expected_revision is not None
                        and revision != int(expected_revision)
                    ):
                        current = read_artifact(self._ocr_artifact_path(book_id))
                        return current if current is not None else (last_artifact or {})
                    cache_rows = conn.execute(
                        "SELECT page_index,text FROM manga_ocr_cache "
                        "WHERE book_id=? AND region_key=? ORDER BY page_index",
                        (book_id, _REGION_CACHE_KEY),
                    ).fetchall()
                by_index = {
                    int(item["page_index"]): str(item["text"] or "[]")
                    for item in cache_rows
                }
                pages: list[dict[str, Any]] = []
                with zipfile.ZipFile(archive_path) as archive:
                    for page_index, encoded in sorted(by_index.items()):
                        try:
                            regions = json.loads(encoded)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            regions = []
                        if (
                            not isinstance(regions, list)
                            or not 0 <= page_index < len(page_names)
                        ):
                            continue
                        try:
                            with Image.open(
                                io.BytesIO(archive.read(page_names[page_index]))
                            ) as source_image:
                                width, height = source_image.size
                        except (OSError, ValueError, KeyError):
                            width, height = 0, 0
                        pages.append(
                            normalize_page(
                                page_index,
                                [
                                    dict(item)
                                    for item in regions
                                    if isinstance(item, dict)
                                ],
                                name=page_names[page_index],
                                width=width,
                                height=height,
                            )
                        )
                detector_names = sorted(
                    {
                        str(region.get("detector") or "unknown")
                        for page in pages
                        for region in page.get("regions") or []
                        if isinstance(region, dict)
                    }
                )
                recognizer_names = sorted(
                    {
                        str(region.get("recognizer") or "unknown")
                        for page in pages
                        for region in page.get("regions") or []
                        if isinstance(region, dict)
                    }
                )
                artifact = build_artifact(
                    source_fingerprint=fingerprint,
                    title=str(row["title"] or ""),
                    page_count=int(row["page_count"] or 0),
                    pages=pages,
                    detector="+".join(detector_names) or "unknown",
                    recognizer="+".join(recognizer_names) or "unknown",
                )
                artifact["generation"] = int(generation)
                artifact["revision"] = int(revision)
                last_artifact = artifact

                # A commit/invalidation that happened while the JSON projection
                # was built makes this snapshot stale.  Do not publish it.
                current_fingerprint, current_generation, current_revision = (
                    self._ocr_context(book_id)
                )
                if (
                    current_fingerprint == fingerprint
                    and current_generation == generation
                    and current_revision == revision
                ):
                    write_artifact(self._ocr_artifact_path(book_id), artifact)
                    return artifact
                if expected_generation is not None or expected_revision is not None:
                    return artifact
            return last_artifact or {}

    def ocr_artifact(self, book_id: int) -> dict[str, Any]:
        return self._load_ocr_artifact(int(book_id)) or self._rebuild_ocr_artifact(
            int(book_id)
        )

    def invalidate_region_cache(self, book_id: int) -> None:
        """Discard current OCR generation atomically before rebuilding."""

        book_id = int(book_id)
        with self._ocr_publish_lock(book_id):
            artifact_path = self._ocr_artifact_path(book_id)
            with self.db.connect() as conn:
                conn.execute(
                    "DELETE FROM manga_ocr_cache WHERE book_id=? AND region_key=?",
                    (book_id, _REGION_CACHE_KEY),
                )
                conn.execute(
                    "DELETE FROM state WHERE key LIKE ?",
                    (f"manga_ocr_page_status:v18:{book_id}:%",),
                )
                self._bump_ocr_generation_in_conn(conn, book_id)
            artifact_path.unlink(missing_ok=True)

    @staticmethod
    def _ocr_page_status_state_key(book_id: int, page_index: int) -> str:
        return f"manga_ocr_page_status:v18:{int(book_id)}:{int(page_index)}"

    def _ocr_page_status(self, book_id: int, page_index: int) -> dict[str, Any]:
        book_id = int(book_id)
        raw = self.db.get_state(
            self._ocr_page_status_state_key(book_id, page_index), ""
        ).strip()
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        fingerprint, generation, _revision = self._ocr_context(book_id)
        payload_fingerprint = str(payload.get("source_fingerprint") or "")
        payload_generation = payload.get("generation")
        if payload_fingerprint and payload_fingerprint != fingerprint:
            return {}
        if payload_generation is not None:
            try:
                if int(payload_generation) != generation:
                    return {}
            except (TypeError, ValueError):
                return {}
        elif generation > 0:
            # Legacy statuses predate generation ownership.  Once the source has
            # been invalidated, such rows must never become authoritative again.
            return {}
        return dict(payload)

    def _set_ocr_page_status(
        self,
        book_id: int,
        page_index: int,
        *,
        status: str,
        reason: str = "",
        retryable: bool = False,
    ) -> None:
        book_id = int(book_id)
        fingerprint, generation, _revision = self._ocr_context(book_id)
        self.db.set_state(
            self._ocr_page_status_state_key(book_id, page_index),
            json.dumps(
                {
                    "status": str(status),
                    "reason": str(reason),
                    "retryable": bool(retryable),
                    "source_fingerprint": fingerprint,
                    "generation": generation,
                    "updated_at": time.time(),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    def _cached_ocr_payload(
        self,
        book_id: int,
        page_index: int,
        regions: list[dict[str, Any]],
        *,
        artifact: bool = False,
        cached_only: bool = False,
    ) -> dict[str, Any] | None:
        state = self._ocr_page_status(book_id, page_index)
        status = str(state.get("status") or "")
        reason = str(state.get("reason") or "")
        retryable = bool(state.get("retryable"))
        if not status:
            if regions:
                status = "ready"
                reason = "legacy_nonempty_cache"
            else:
                status = "unknown_cached_empty"
                reason = "legacy_empty_cache_requires_retry"
                retryable = True
        if not regions and status not in {"empty_verified"} and not cached_only:
            return None
        normalized_regions = [
            _normalize_region_orientation(dict(item))
            for item in regions
            if isinstance(item, dict)
        ]
        corrected_regions, correction_conflicts = self._apply_manual_corrections(
            int(book_id), int(page_index), normalized_regions
        )
        visible_regions, suppression_conflicts = self._apply_manual_suppressions(
            int(book_id), int(page_index), corrected_regions
        )
        with_additions, addition_conflicts = self._apply_manual_additions(
            int(book_id), int(page_index), visible_regions
        )
        ordered_regions, order_conflicts = self._apply_manual_reading_order(
            int(book_id), int(page_index), with_additions
        )
        geometry_regions, geometry_conflicts = self._apply_manual_geometry(
            int(book_id), int(page_index), ordered_regions
        )
        return {
            "book_id": int(book_id),
            "page_index": int(page_index),
            "regions": geometry_regions,
            "available": True,
            "cached": True,
            "artifact": bool(artifact),
            "status": status,
            "reason": reason,
            "retryable": retryable,
            "manual_correction_conflicts": correction_conflicts,
            "manual_reading_order_conflicts": order_conflicts,
            "manual_suppression_conflicts": suppression_conflicts,
            "manual_addition_conflicts": addition_conflicts,
            "manual_geometry_conflicts": geometry_conflicts,
        }

    def text_regions(
        self,
        book_id: int,
        page_index: int,
        *,
        refresh: bool = False,
        cached_only: bool = False,
    ) -> dict[str, Any]:
        row = self._book(int(book_id))
        source_fingerprint, generation, _revision = self._ocr_context(int(book_id))
        pages = self._pages(Path(str(row["path"])))
        index = max(0, min(int(page_index), max(0, len(pages) - 1)))
        region_key = _REGION_CACHE_KEY
        if not refresh:
            page = artifact_page(self._load_ocr_artifact(int(book_id)), index)
            if page is not None:
                regions = [dict(item) for item in page.get("regions") or [] if isinstance(item, dict)]
                cached_payload = self._cached_ocr_payload(
                    int(book_id), index, regions, artifact=True, cached_only=cached_only
                )
                if cached_payload is not None:
                    return cached_payload
            with self.db.connect() as conn:
                cached = conn.execute(
                    "SELECT text FROM manga_ocr_cache WHERE book_id=? AND page_index=? AND region_key=?",
                    (int(book_id), index, region_key),
                ).fetchone()
            if cached is not None:
                try:
                    regions = json.loads(str(cached["text"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    regions = []
                if isinstance(regions, list):
                    cached_payload = self._cached_ocr_payload(
                        int(book_id),
                        index,
                        [dict(item) for item in regions if isinstance(item, dict)],
                        cached_only=cached_only,
                    )
                    if cached_payload is not None:
                        return cached_payload

        if cached_only:
            return {
                "book_id": int(book_id),
                "page_index": index,
                "regions": [],
                "available": sys.platform == "darwin",
                "cached": False,
                "status": "missing",
                "reason": "not_cached",
                "retryable": True,
            }

        if sys.platform != "darwin":
            return {
                "book_id": int(book_id),
                "page_index": index,
                "regions": [],
                "available": False,
                "cached": False,
                "status": "failed",
                "reason": "macos_required",
                "retryable": False,
            }
        path = Path(str(row["path"]))
        with zipfile.ZipFile(path) as archive:
            image = Image.open(io.BytesIO(archive.read(pages[index]))).convert("RGB")

        try:
            detected = self._vision_text_regions(image)
        except Exception as exc:
            return {
                "book_id": int(book_id),
                "page_index": index,
                "regions": [],
                "available": True,
                "cached": False,
                "status": "failed",
                "reason": f"detector_error:{type(exc).__name__}",
                "retryable": True,
            }

        regions = detected
        recognizer_ran = False
        if regions and self.ocr_available():
            try:
                recognizer_ran = True
                regions = self._ocr_regions(image, regions)
            except MangaOcrDeferred as exc:
                provisional = [
                    _normalize_region_orientation(item)
                    for item in detected
                    if str(item.get("text") or "").strip()
                ]
                return {
                    "book_id": int(book_id),
                    "page_index": index,
                    "regions": provisional,
                    "available": True,
                    "cached": False,
                    "status": "deferred",
                    "reason": str(exc),
                    "retryable": True,
                }
            except MangaOcrWorkerError as exc:
                provisional = [
                    _normalize_region_orientation(item)
                    for item in detected
                    if str(item.get("text") or "").strip()
                ]
                return {
                    "book_id": int(book_id),
                    "page_index": index,
                    "regions": provisional,
                    "available": True,
                    "cached": False,
                    "status": "failed",
                    "reason": str(exc),
                    "retryable": True,
                }

        normalized = _finalize_recognized_regions(
            [dict(item) for item in regions if isinstance(item, dict)]
        )
        normalized = _repair_repeated_latin_page_titles(
            normalized,
            self._cached_peer_region_pages(int(book_id), exclude_page=index),
        )
        detector_fallback = bool(detected) and all(bool(item.get("fallback")) for item in detected)
        partial = any(bool(item.get("error")) for item in normalized)
        if not normalized:
            if not detected:
                status, reason, retryable = "empty_verified", "successful_detector_no_text", False
            elif recognizer_ran and not detector_fallback:
                status, reason, retryable = "empty_verified", "successful_ocr_no_text", False
            else:
                return {
                    "book_id": int(book_id),
                    "page_index": index,
                    "regions": [],
                    "available": True,
                    "cached": False,
                    "status": "failed",
                    "reason": "detector_miss_or_recognizer_unavailable",
                    "retryable": True,
                }
        elif partial or not recognizer_ran:
            status, reason, retryable = "partial", "fallback_text_or_partial_ocr", True
        else:
            status, reason, retryable = "ready", "", False

        committed = self._commit_ocr_page_updates(
            int(book_id),
            source_fingerprint=source_fingerprint,
            generation=generation,
            updates=[(index, normalized, status, reason, retryable)],
        )
        if not committed:
            return {
                "book_id": int(book_id),
                "page_index": index,
                "regions": [],
                "available": True,
                "cached": False,
                "status": "stale",
                "reason": "source_changed_during_ocr",
                "retryable": True,
            }
        corrected_regions, correction_conflicts = self._apply_manual_corrections(
            int(book_id), index, normalized
        )
        visible_regions, suppression_conflicts = self._apply_manual_suppressions(
            int(book_id), index, corrected_regions
        )
        with_additions, addition_conflicts = self._apply_manual_additions(
            int(book_id), index, visible_regions
        )
        ordered_regions, order_conflicts = self._apply_manual_reading_order(
            int(book_id), index, with_additions
        )
        geometry_regions, geometry_conflicts = self._apply_manual_geometry(
            int(book_id), index, ordered_regions
        )
        return {
            "book_id": int(book_id),
            "page_index": index,
            "regions": geometry_regions,
            "available": True,
            "cached": False,
            "status": status,
            "reason": reason,
            "retryable": retryable,
            "manual_correction_conflicts": correction_conflicts,
            "manual_reading_order_conflicts": order_conflicts,
            "manual_suppression_conflicts": suppression_conflicts,
            "manual_addition_conflicts": addition_conflicts,
            "manual_geometry_conflicts": geometry_conflicts,
        }

    def _ocr_regions(self, image: Image.Image, regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        work_dir = self.cache_dir / "manga-ocr" / "regions"
        work_dir.mkdir(parents=True, exist_ok=True)
        heavy_lease = None
        if self.work_scheduler is not None:
            # pudge-v0.7.27-manga-foreground-mangaocr-v1
            # Direct page OCR is user-requested foreground work. Serialize it,
            # but do not reject it because the manga reader itself is foreground.
            heavy_lease = self.work_scheduler.acquire_heavy(
                "manga-ocr-region", blocking=True, foreground_sensitive=False
            )
            if heavy_lease is None:
                raise MangaOcrDeferred("heavy_work_deferred")
        with self._ocr_lock:
            input_path: Path | None = None
            manifest_path: Path | None = None
            output_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".png", dir=work_dir, delete=False) as handle:
                    input_path = Path(handle.name)
                manifest_path = input_path.with_suffix(".regions.json")
                output_path = input_path.with_suffix(".result.json")
                image.save(input_path, format="PNG")
                manifest_path.write_text(
                    json.dumps({"regions": regions}, ensure_ascii=False), encoding="utf-8"
                )
                completed = subprocess.run(
                    [
                        self.python,
                        "-m",
                        "pudge.manga_ocr_worker",
                        "--regions",
                        str(input_path),
                        str(manifest_path),
                        str(output_path),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=240,
                )
                if completed.returncode != 0 or output_path is None or not output_path.is_file():
                    detail = (completed.stderr or completed.stdout or "manga OCR worker failed").strip()
                    raise MangaOcrWorkerError(detail[-1000:])
                try:
                    payload = json.loads(output_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    raise MangaOcrWorkerError(f"invalid_worker_output:{type(exc).__name__}") from exc
                recognized = payload.get("regions") if isinstance(payload, dict) else None
                if not isinstance(recognized, list):
                    raise MangaOcrWorkerError("invalid_worker_regions")
                return [dict(item) for item in recognized if isinstance(item, dict)]
            except subprocess.TimeoutExpired as exc:
                raise MangaOcrWorkerError("manga_ocr_timeout") from exc
            finally:
                for path in (input_path, manifest_path, output_path):
                    if path is not None:
                        path.unlink(missing_ok=True)
                if heavy_lease is not None:
                    heavy_lease.release()
    def _nearby_mokuro_path(self, book_id: int) -> Path | None:
        """Discover only unambiguous, same-volume sidecars next to the source.

        Never search a whole library, follow symlinks, or accept paths supplied
        by the browser. Ambiguous names require the existing manual picker.
        """
        archive = Path(str(self._book(int(book_id))["path"]))
        choices = (
            archive.with_suffix(".mokuro"),
            archive.with_name(archive.name + ".mokuro"),
            archive.parent / "mokuro" / (archive.stem + ".mokuro"),
        )
        found: list[Path] = []
        for candidate in dict.fromkeys(choices):
            if candidate.is_symlink() or candidate.parent.is_symlink() or not candidate.is_file():
                continue
            try:
                if 0 < candidate.stat().st_size <= 100 * 1024 * 1024:
                    found.append(candidate)
            except OSError:
                continue
        return found[0] if len(found) == 1 else None

    def nearby_mokuro(self, book_id: int) -> dict[str, Any]:
        """A lightweight hint for the reader, without reading or importing OCR."""
        path = self._nearby_mokuro_path(int(book_id))
        return {"available": path is not None, "filename": path.name if path else ""}

    def import_nearby_mokuro(self, book_id: int) -> dict[str, Any]:
        """Require a complete page match before importing an auto-found sidecar."""
        book_id = int(book_id)
        path = self._nearby_mokuro_path(book_id)
        if path is None:
            raise MokuroImportError("No unambiguous Mokuro sidecar next to this volume; select the file manually")
        return self.import_mokuro(book_id, path, require_full_coverage=True)

    def import_mokuro(
        self, book_id: int, sidecar: Path, *, require_full_coverage: bool = False,
    ) -> dict[str, Any]:
        """Import an explicit Mokuro sidecar into the existing volume OCR cache.

        Validate every page and coordinate before writing. Existing verified
        OCR pages are left intact unless replacement was explicitly requested.
        """
        book_id = int(book_id)
        path = Path(sidecar).expanduser().resolve()
        if path.suffix.casefold() != ".mokuro" or not path.is_file():
            raise MokuroImportError("Select an existing .mokuro file")
        if path.stat().st_size > 100 * 1024 * 1024:
            raise MokuroImportError("Mokuro file exceeds the 100 MiB limit")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise MokuroImportError("Cannot read Mokuro JSON") from exc
        row = self._book(book_id)
        archive_path = Path(str(row["path"]))
        names = self._pages(archive_path)
        # A mismatched sidecar is rejected before any cache or artifact writes.
        sizes: dict[int, tuple[int, int]] = {}
        with zipfile.ZipFile(archive_path) as archive:
            for index, name in enumerate(names):
                with Image.open(io.BytesIO(archive.read(name))) as image:
                    sizes[index] = image.size
        pages = convert_mokuro(payload, names, image_sizes=sizes)
        if require_full_coverage and len(pages) != len(names):
            raise MokuroImportError("Nearby Mokuro does not cover every image in this volume")
        with self._ocr_publish_lock(book_id):
            fingerprint, generation, _revision = self._ocr_context(book_id)
            updates: list[tuple[int, list[dict[str, Any]] | None, str, str, bool]] = []
            skipped = 0
            for page_index, regions in sorted(pages.items()):
                with self.db.connect() as conn:
                    cached = conn.execute(
                        "SELECT text FROM manga_ocr_cache WHERE book_id=? AND page_index=? AND region_key=?",
                        (book_id, page_index, _REGION_CACHE_KEY),
                    ).fetchone()
                if cached is not None:
                    status = str(self._ocr_page_status(book_id, page_index).get("status") or "")
                    # Preserve authoritative native/Mokuro OCR and legacy nonempty
                    # cache rows; retryable partial/failed/empty-unknown rows may
                    # be repaired by an explicitly selected Mokuro sidecar.
                    if status in {"ready", "empty_verified"}:
                        skipped += 1
                        continue
                    if not status:
                        try:
                            legacy_regions = json.loads(str(cached["text"] or "[]"))
                        except (TypeError, ValueError, json.JSONDecodeError):
                            legacy_regions = []
                        if isinstance(legacy_regions, list) and legacy_regions:
                            skipped += 1
                            continue
                updates.append((page_index, regions, "ready" if regions else "empty_verified", "mokuro_import", False))
            if updates and not self._commit_ocr_page_updates(
                book_id, source_fingerprint=fingerprint, generation=generation, updates=updates,
            ):
                raise MokuroImportError("Volume changed during Mokuro import; no pages were published")
        return {
            "book_id": book_id,
            "imported_pages": len(updates),
            "skipped_pages": skipped,
            "sidecar_pages": len(pages),
            **self.ocr_cache_status(book_id),
        }

    def _cached_peer_region_pages(
        self,
        book_id: int,
        *,
        exclude_page: int | None = None,
    ) -> list[list[dict[str, Any]]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT page_index,text FROM manga_ocr_cache "
                "WHERE book_id=? AND region_key=? ORDER BY page_index",
                (int(book_id), _REGION_CACHE_KEY),
            ).fetchall()
        pages: list[list[dict[str, Any]]] = []
        for row in rows:
            if exclude_page is not None and int(row["page_index"]) == int(exclude_page):
                continue
            try:
                regions = json.loads(str(row["text"] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(regions, list):
                pages.append([dict(item) for item in regions if isinstance(item, dict)])
        return pages

    def cached_region_texts(self, book_id: int) -> list[tuple[int, str]]:
        """Return recognized bubbles in reading order for background study parsing."""

        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT page_index,text FROM manga_ocr_cache "
                "WHERE book_id=? AND region_key=? ORDER BY page_index",
                (int(book_id), _REGION_CACHE_KEY),
            ).fetchall()
        result: list[tuple[int, str]] = []
        for row in rows:
            try:
                regions = json.loads(str(row["text"] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            normalized = [dict(region) for region in regions if isinstance(region, dict)] if isinstance(regions, list) else []
            corrected, _correction_conflicts = self._apply_manual_corrections(
                int(book_id), int(row["page_index"]), normalized
            )
            visible, _suppression_conflicts = self._apply_manual_suppressions(
                int(book_id), int(row["page_index"]), corrected
            )
            with_additions, _addition_conflicts = self._apply_manual_additions(
                int(book_id), int(row["page_index"]), visible
            )
            ordered, _order_conflicts = self._apply_manual_reading_order(
                int(book_id), int(row["page_index"]), with_additions
            )
            for region in ordered:
                text = str(region.get("text") or "").strip()
                if text:
                    result.append((int(row["page_index"]), text))
        return result

    def ocr_book_mokuro(
        self,
        book_id: int,
        *,
        mokuro_python: Path,
        progress: Callable[..., None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Recognize a volume with the optional isolated Mokuro backend.

        The original archive is never handed to Mokuro. Pudge copies only the
        image members into a private staging directory, runs Mokuro there, then
        validates/publishes the generated .mokuro through the same importer used
        for user-provided sidecars.
        """

        python = Path(mokuro_python).expanduser()
        if not python.is_file():
            raise RuntimeError("Mokuro OCR backend is not installed. Install it from Settings → Essential.")

        row = self._book(int(book_id))
        archive_path = Path(str(row["path"]))
        pages = self._pages(archive_path)
        total = len(pages)
        if total <= 0:
            raise RuntimeError("Manga volume has no image pages")

        def emit(done: int, phase: str) -> None:
            if progress is None:
                return
            try:
                progress(max(0, min(total, int(done))), total, None, str(phase))
            except TypeError:
                progress(max(0, min(total, int(done))), total, None)

        if cancelled is not None and cancelled():
            return {**self.ocr_cache_status(int(book_id)), "ok": False, "cancelled": True, "errors": []}

        run_root = self.cache_dir / "manga-ocr" / "mokuro-runs"
        run_root.mkdir(parents=True, exist_ok=True)
        token = f"{int(book_id)}-{int(time.time() * 1000)}"
        run_dir = run_root / token
        volume_dir = run_dir / "volume"
        volume_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = run_dir / "stdout.log"
        stderr_path = run_dir / "stderr.log"
        output_sidecar = run_dir / "volume.mokuro"
        emit(0, "preparing")

        try:
            with zipfile.ZipFile(archive_path) as archive:
                for index, name in enumerate(pages, start=1):
                    if cancelled is not None and cancelled():
                        return {**self.ocr_cache_status(int(book_id)), "ok": False, "cancelled": True, "errors": []}
                    normalized = str(name).replace("\\", "/")
                    parts = [part for part in normalized.split("/") if part not in {"", "."}]
                    if not parts or any(part == ".." for part in parts) or normalized.startswith("/"):
                        raise RuntimeError(f"Unsafe manga archive path: {name}")
                    target = volume_dir.joinpath(*parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    data = archive.read(name)
                    if len(data) > 128 * 1024 * 1024:
                        raise RuntimeError(f"Manga page is too large for Mokuro staging: {name}")
                    target.write_bytes(data)
                    emit(index, "preparing")

            heavy_lease = None
            if self.work_scheduler is not None:
                heavy_lease = self.work_scheduler.acquire_heavy(
                    "mokuro-ocr-book",
                    blocking=True,
                    foreground_sensitive=True,
                    wait_for_foreground=True,
                    cancel_check=cancelled,
                    priority=WorkPriority.BACKGROUND,
                )
                if heavy_lease is None:
                    return {**self.ocr_cache_status(int(book_id)), "ok": False, "cancelled": True, "errors": []}
            try:
                code = (
                    "from mokuro.run import run; import sys; "
                    "run(sys.argv[1], disable_confirmation=True, no_cache=True, legacy_html=False)"
                )
                with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
                    "w", encoding="utf-8"
                ) as stderr_handle:
                    process = subprocess.Popen(
                        [str(python), "-c", code, str(volume_dir)],
                        stdout=stdout_handle,
                        stderr=stderr_handle,
                        text=True,
                        start_new_session=(os.name == "posix"),
                    )
                    last_done = -1
                    while process.poll() is None:
                        if cancelled is not None and cancelled():
                            _stop_ocr_process_tree(process, run_dir / "STOP", grace_seconds=1.0)
                            return {**self.ocr_cache_status(int(book_id)), "ok": False, "cancelled": True, "errors": []}
                        cache_dir = run_dir / "_ocr" / "volume"
                        try:
                            done = sum(1 for item in cache_dir.rglob("*.json") if item.is_file())
                        except OSError:
                            done = 0
                        if done != last_done:
                            emit(done, "ocr")
                            last_done = done
                        time.sleep(0.35)
                    returncode = int(process.returncode or 0)
                if returncode != 0:
                    stderr = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.is_file() else ""
                    detail = stderr.strip()[-2500:] or f"Mokuro exited with code {returncode}"
                    raise RuntimeError(detail)
                if not output_sidecar.is_file():
                    raise RuntimeError("Mokuro completed without generating a .mokuro file")
                emit(total, "publishing")
                imported = self.import_mokuro(int(book_id), output_sidecar, require_full_coverage=True)
                return {**imported, "ok": True, "errors": [], "backend": "mokuro"}
            finally:
                if heavy_lease is not None:
                    heavy_lease.release()
        finally:
            # The validated OCR cache is durable. Mokuro's private extraction,
            # model cache rows and stdout are only staging data.
            shutil.rmtree(run_dir, ignore_errors=True)

    def ocr_book(
        self,
        book_id: int,
        *,
        progress: Callable[..., None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if not self.ocr_available():
            raise RuntimeError("MangaOCR is not installed. Install it from Settings → Essential.")
        row = self._book(int(book_id))
        source_fingerprint, generation, _revision = self._ocr_context(int(book_id))

        def emit_progress(done: int, total: int, page_index: int | None, phase: str) -> None:
            if progress is None:
                return
            try:
                progress(int(done), int(total), page_index, str(phase))
            except TypeError:
                # Compatibility with older callers/tests that still provide the
                # original three-argument callback.
                progress(int(done), int(total), page_index)

        def wait_for_manual_priority() -> bool:
            scheduler = self.work_scheduler
            should_yield = getattr(scheduler, "should_yield_to_higher_priority", None)
            while callable(should_yield) and should_yield(WorkPriority.BACKGROUND):
                if cancelled is not None and cancelled():
                    return False
                time.sleep(0.10)
            return True

        archive_path = Path(str(row["path"]))
        pages = self._pages(archive_path)
        total = len(pages)
        with self.db.connect() as conn:
            cached_rows = conn.execute(
                "SELECT page_index,text FROM manga_ocr_cache WHERE book_id=? AND region_key=?",
                (int(book_id), _REGION_CACHE_KEY),
            ).fetchall()
        cached: set[int] = set()
        legacy_ready: list[int] = []
        for item in cached_rows:
            page_index_value = int(item["page_index"])
            status = str(self._ocr_page_status(int(book_id), page_index_value).get("status") or "")
            if status in {"ready", "empty_verified"}:
                cached.add(page_index_value)
                continue
            if status == "partial":
                continue
            try:
                legacy_regions = json.loads(str(item["text"] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                legacy_regions = []
            if isinstance(legacy_regions, list) and legacy_regions:
                cached.add(page_index_value)
                legacy_ready.append(page_index_value)
        for page_index_value in legacy_ready:
            self._set_ocr_page_status(
                int(book_id),
                page_index_value,
                status="ready",
                reason="legacy_nonempty_cache",
                retryable=False,
            )
        missing = [index for index in range(total) if index not in cached]
        done_before = total - len(missing)
        if not wait_for_manual_priority():
            return {
                **self.ocr_cache_status(int(book_id)),
                "ok": False,
                "cancelled": True,
                "errors": [],
            }
        emit_progress(
            done_before,
            total,
            None,
            "detecting" if missing else "ocr",
        )
        if not missing:
            return {**self.ocr_cache_status(int(book_id)), "ok": True, "errors": []}

        job_dir = self.cache_dir / "manga-ocr" / "batch"
        job_dir.mkdir(parents=True, exist_ok=True)
        token = f"{int(book_id)}-{int(time.time() * 1000)}"
        manifest_path = job_dir / f"{token}.manifest.json"
        output_path = job_dir / f"{token}.results.jsonl"
        progress_path = job_dir / f"{token}.progress.json"
        stop_path = job_dir / f"{token}.stop"
        stdout_path = job_dir / f"{token}.stdout"
        stderr_path = job_dir / f"{token}.stderr"
        if self.work_scheduler is not None:
            if not self.work_scheduler.wait_until_background(
                cancel_check=cancelled, poll_seconds=0.5
            ):
                return {
                    **self.ocr_cache_status(int(book_id)),
                    "ok": False, "cancelled": True, "errors": [],
                }
        page_regions: dict[int, list[dict[str, Any]]] = {}
        with zipfile.ZipFile(archive_path) as archive:
            for prepared_offset, index in enumerate(missing, start=1):
                if not wait_for_manual_priority():
                    return {
                        **self.ocr_cache_status(int(book_id)),
                        "ok": False,
                        "cancelled": True,
                        "errors": [],
                    }
                if cancelled is not None and cancelled():
                    return {
                        **self.ocr_cache_status(int(book_id)),
                        "ok": False,
                        "cancelled": True,
                        "errors": [],
                    }
                image = Image.open(io.BytesIO(archive.read(pages[index]))).convert("RGB")
                page_regions[index] = self._vision_text_regions(image)
                emit_progress(
                    min(total, done_before + prepared_offset),
                    total,
                    index,
                    "detecting",
                )
        manifest_path.write_text(
            json.dumps(
                {
                    "archive": str(archive_path),
                    "pages": [
                        {"page_index": index, "name": pages[index], "regions": page_regions[index]}
                        for index in missing
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        process: subprocess.Popen[str] | None = None
        errors: list[str] = []
        preempted = False
        cancelled_requested = False
        stop_requested_at: float | None = None
        heavy_lease = None
        if self.work_scheduler is not None:
            heavy_lease = self.work_scheduler.acquire_heavy(
                "manga-ocr-book",
                blocking=True,
                foreground_sensitive=True,
                wait_for_foreground=True,
                cancel_check=cancelled,
                priority=WorkPriority.BACKGROUND,
            )
            if heavy_lease is None:
                return {
                    **self.ocr_cache_status(int(book_id)),
                    "ok": False,
                    "cancelled": True,
                    "errors": [],
                }
        try:
            stop_path.unlink(missing_ok=True)
            with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
                "w", encoding="utf-8"
            ) as stderr_handle:
                process = subprocess.Popen(
                    [
                        self.python,
                        "-m",
                        (
                            "pudge.manga_ocr_parallel"
                            if len(missing) >= 6 else "pudge.manga_ocr_worker"
                        ),
                        "--batch",
                        str(manifest_path),
                        str(output_path),
                        str(progress_path),
                        str(stop_path),
                    ],
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    text=True,
                    start_new_session=(os.name == "posix"),
                )
                last_reported = -1
                while process.poll() is None:
                    now = time.monotonic()
                    wants_cancel = bool(cancelled is not None and cancelled())
                    should_yield = getattr(
                        self.work_scheduler, "should_yield_to_higher_priority", None
                    )
                    wants_preempt = bool(
                        callable(should_yield)
                        and should_yield(WorkPriority.BACKGROUND)
                    )
                    if (wants_cancel or wants_preempt) and stop_requested_at is None:
                        cancelled_requested = wants_cancel
                        preempted = wants_preempt and not wants_cancel
                        stop_requested_at = now
                        try:
                            stop_path.write_text("stop\n", encoding="utf-8")
                        except OSError:
                            pass
                    if stop_requested_at is not None and now - stop_requested_at >= 45.0:
                        _stop_ocr_process_tree(process, stop_path)
                        break
                    try:
                        payload = json.loads(progress_path.read_text(encoding="utf-8"))
                        processed = max(0, int(payload.get("done") or 0))
                        current = payload.get("page_index")
                        current_index = int(current) if current is not None else None
                    except (OSError, ValueError, TypeError, json.JSONDecodeError):
                        processed = 0
                        current_index = None
                    absolute_done = min(total, done_before + processed)
                    if absolute_done != last_reported:
                        emit_progress(absolute_done, total, current_index, "ocr")
                        last_reported = absolute_done
                    time.sleep(0.10 if stop_requested_at is not None else 0.35)
                if process.poll() is None:
                    process.wait(timeout=5)

            stdout = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.is_file() else ""
            stderr = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.is_file() else ""
            if stderr.strip() and not preempted and not cancelled_requested:
                errors.append(stderr.strip()[-2000:])
            page_updates: list[
                tuple[int, list[dict[str, Any]] | None, str, str, bool]
            ] = []
            output_lines = output_path.read_text(encoding="utf-8").splitlines() if output_path.is_file() else []
            validated, invalid_pages, validation_errors = _validated_batch_ocr_rows(
                output_lines, set(missing)
            )
            if not preempted and not cancelled_requested:
                errors.extend(validation_errors)
            cached_peer_pages = self._cached_peer_region_pages(int(book_id))
            batch_peer_pages: list[list[dict[str, Any]]] = []
            for page_index_value in missing:
                item = validated.get(page_index_value)
                if item is None:
                    # A worker may exit normally despite truncating its JSONL
                    # result. Keep the page retryable and expose the failure.
                    if not preempted and not cancelled_requested:
                        reason = (
                            "invalid_or_duplicate_worker_output"
                            if page_index_value in invalid_pages else "missing_worker_output"
                        )
                        errors.append(f"page {page_index_value + 1}: {reason}")
                        page_updates.append((page_index_value, None, "failed", reason, True))
                    continue
                error = str(item.get("error") or "").strip()
                if error:
                    errors.append(f"page {page_index_value + 1}: {error}")
                    page_updates.append((page_index_value, None, "failed", error, True))
                    continue
                regions = item["regions"]
                normalized_regions = _finalize_recognized_regions(
                    [dict(region) for region in regions]
                )
                normalized_regions = _repair_repeated_latin_page_titles(
                    normalized_regions,
                    cached_peer_pages + batch_peer_pages,
                )
                batch_peer_pages.append([dict(region) for region in normalized_regions])
                partial = any(
                    isinstance(region, dict) and bool(region.get("error"))
                    for region in normalized_regions
                )
                status_name = (
                    "partial" if partial else "ready" if normalized_regions else "empty_verified"
                )
                page_updates.append(
                    (
                        page_index_value,
                        normalized_regions,
                        status_name,
                        "batch_partial" if partial else "",
                        bool(partial),
                    )
                )
            committed = self._commit_ocr_page_updates(
                int(book_id),
                source_fingerprint=source_fingerprint,
                generation=generation,
                updates=page_updates,
            )
            status = self.ocr_cache_status(int(book_id))
            if page_updates and not committed:
                errors.append("source_changed_during_ocr")
                emit_progress(int(status["cached_pages"]), total, None, "ocr")
                return {
                    **status,
                    "ok": False,
                    "stale": True,
                    "errors": errors,
                }
            emit_progress(int(status["cached_pages"]), total, None, "ocr")
            if (
                process.returncode not in {0, None, 75}
                and not errors
                and not preempted
                and not cancelled_requested
            ):
                errors.append(stdout.strip()[-1000:] or f"worker exited with {process.returncode}")
            if cancelled_requested:
                return {
                    **status,
                    "ok": False,
                    "cancelled": True,
                    "errors": errors,
                }
            if preempted:
                return {
                    **status,
                    "ok": False,
                    "preempted": True,
                    "errors": errors,
                }
            return {**status, "ok": not errors and bool(status["complete"]), "errors": errors}
        finally:
            if process is not None:
                _stop_ocr_process_tree(process, stop_path)
            for path in (
                manifest_path,
                output_path,
                progress_path,
                stop_path,
                stdout_path,
                stderr_path,
            ):
                path.unlink(missing_ok=True)
            if heavy_lease is not None:
                heavy_lease.release()
