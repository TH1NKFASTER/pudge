"""Validate Mokuro 0.2+ text blocks for a *known* CBZ/ZIP volume.

This module neither opens images nor guesses that position N means image N.
A caller must supply the actual archive page names in reader order.
"""

from __future__ import annotations

import math
from pathlib import PurePosixPath
from typing import Any


class MokuroImportError(ValueError):
    """The sidecar is invalid or does not match the selected manga volume."""


def _page_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MokuroImportError("Mokuro page is missing img_path")
    name = value.replace("\\", "/").removeprefix("./")
    if name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/")):
        raise MokuroImportError(f"Invalid Mokuro img_path: {value!r}")
    return name


def _size(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MokuroImportError(f"Invalid Mokuro {field}")
    if not math.isfinite(value) or not 0 < value <= 100000 or int(value) != value:
        raise MokuroImportError(f"Invalid Mokuro {field}")
    return int(value)


def _rect(value: object, width: int, height: int, where: str) -> dict[str, float]:
    if not isinstance(value, list) or len(value) != 4:
        raise MokuroImportError(f"Invalid {where}: expected [left, top, right, bottom]")
    if any(isinstance(n, bool) or not isinstance(n, (float, int)) or not math.isfinite(n) for n in value):
        raise MokuroImportError(f"Invalid {where}: non-finite coordinate")
    x1, y1, x2, y2 = (float(n) for n in value)
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        raise MokuroImportError(f"Invalid {where}: outside page bounds")
    # Mokuro uses image pixel coordinates with the origin at top-left;
    # Pudge's normalized page geometry uses the origin at bottom-left.
    return {
        "x": round(x1 / width, 6),
        "y": round(1.0 - y2 / height, 6),
        "width": round((x2 - x1) / width, 6),
        "height": round((y2 - y1) / height, 6),
    }


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
        raise MokuroImportError("Not a Mokuro sidecar: missing pages array")
    entries = payload["pages"]
    if not entries or len(entries) > 5000:
        raise MokuroImportError("Mokuro page count is invalid")
    exact: dict[str, int] = {}
    basename: dict[str, list[int]] = {}
    for i, archive_name in enumerate(archive_pages):
        name = _page_name(archive_name)
        if name in exact:
            raise MokuroImportError(f"Duplicate archive page: {name}")
        exact[name] = i
        basename.setdefault(PurePosixPath(name).name, []).append(i)

    parsed: dict[int, list[dict[str, Any]]] = {}
    for raw_page in entries:
        if not isinstance(raw_page, dict):
            raise MokuroImportError("Invalid Mokuro page entry")
        source_name = _page_name(raw_page.get("img_path"))
        index = exact.get(source_name)
        if index is None:
            candidates = basename.get(PurePosixPath(source_name).name, [])
            if len(candidates) != 1:
                raise MokuroImportError(f"Mokuro image cannot be matched uniquely: {source_name}")
            index = candidates[0]
        if index in parsed:
            raise MokuroImportError(f"Repeated Mokuro page: {source_name}")
        width = _size(raw_page.get("img_width"), "img_width")
        height = _size(raw_page.get("img_height"), "img_height")
        if image_sizes is not None and image_sizes.get(index) != (width, height):
            raise MokuroImportError(f"Image dimensions differ for {source_name}")
        blocks = raw_page.get("blocks")
        if not isinstance(blocks, list) or len(blocks) > 5000:
            raise MokuroImportError(f"Invalid Mokuro blocks for {source_name}")
        regions: list[dict[str, Any]] = []
        for position, block in enumerate(blocks):
            if not isinstance(block, dict) or not isinstance(block.get("lines"), list):
                raise MokuroImportError(f"Invalid text block in {source_name}")
            if not isinstance(block.get("vertical"), bool):
                raise MokuroImportError(f"Missing writing direction in {source_name}")
            lines = block["lines"]
            if len(lines) > 500 or any(not isinstance(s, str) or len(s) > 4096 for s in lines):
                raise MokuroImportError(f"Invalid text lines in {source_name}")
            text = "".join(lines).strip() if block["vertical"] else " ".join(lines).strip()
            geometry = _rect(block.get("box"), width, height, f"block {position} of {source_name}")
            if not text:
                continue
            region: dict[str, Any] = {
                **geometry,
                "text": text,
                "raw_text": text,
                "orientation": "vertical" if block["vertical"] else "horizontal",
                "detector": "mokuro-import",
                "recognizer": "mokuro-import",
                "source": "mokuro-import-v1",
                "geometry_status": "approximate",
                "provenance": {"source": "mokuro", "img_path": source_name},
            }
            # Mokuro line polygons are not word boxes. Keep them as diagnostic
            # provenance, not segments that the Jiten overlay treats as word geometry.
            coords = block.get("lines_coords")
            if isinstance(coords, list) and len(coords) == len(lines):
                segments = []
                for line, polygon in zip(lines, coords):
                    if not line.strip():
                        continue
                    points = polygon if isinstance(polygon, list) else []
                    if len(points) < 2 or not all(isinstance(p, (list, tuple)) and len(p) == 2 for p in points):
                        continue
                    try:
                        xs = [float(p[0]) for p in points]
                        ys = [float(p[1]) for p in points]
                        bounds = _rect([min(xs), min(ys), max(xs), max(ys)], width, height, "line")
                    except (ValueError, TypeError, MokuroImportError):
                        continue
                    segments.append({**bounds, "text": line, "orientation": region["orientation"], "source": "mokuro-line"})
                if segments and len(segments) == len([line for line in lines if line.strip()]):
                    region["provenance"]["line_boxes"] = segments
            regions.append(region)
        parsed[index] = regions
    return parsed
