from __future__ import annotations

import copy
import hashlib
import json
import math
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable


MANGA_OCR_ARTIFACT_SCHEMA = "pudge-manga-ocr-v3"
_LEGACY_SCHEMAS = {"pudge-manga-ocr-v1", "pudge-manga-ocr-v2"}

_GEOMETRY_STATUSES = {"observed", "approximate", "synthetic", "unknown", "unavailable"}
_SYNTHETIC_GEOMETRY_SOURCES = {
    "ink-grid-v1",
    "ink-columns-v2",
    "dark-columns-v1",
    "vertical-grid-fallback",
    "horizontal-region-fallback",
}
_DIAGNOSTIC_KEYS = (
    "geometry_source",
    "geometry_status",
    "detector_geometry",
    "recognizer_preprocess",
    "line_ocr_text",
    "line_ocr_alignment",
    "line_ocr_cost",
    "alignment_status",
    "alignment_quality",
    "alignment_reason",
    "line_id",
    "block_id",
    "generation_id",
    "pipeline_fingerprint",
    "selected_hypothesis_id",
    "observations",
    "raw_observations",
    "hypotheses",
    "token_spans",
    "click_boxes",
    "polygons",
    "normalization_map",
)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _orientation(value: Any, *, width: float, height: float) -> str:
    orientation = str(value or "").casefold()
    if orientation not in {"vertical", "horizontal", "mixed"}:
        orientation = "vertical" if height > width * 1.15 else "horizontal"
    return orientation



def _copy_diagnostic_fields(source: dict[str, Any], target: dict[str, Any]) -> None:
    for key in _DIAGNOSTIC_KEYS:
        if key not in source:
            continue
        value = source.get(key)
        if value is None or value == "":
            continue
        target[key] = copy.deepcopy(value)


def _geometry_status(segments: list[dict[str, Any]], explicit: Any = None) -> str:
    requested = str(explicit or "").strip().casefold()
    if requested in _GEOMETRY_STATUSES:
        return requested
    if not segments:
        return "unavailable"
    if any(not str(segment.get("text") or "").strip() for segment in segments):
        return "unknown"
    sources = {str(segment.get("source") or "").strip() for segment in segments}
    if any(source in _SYNTHETIC_GEOMETRY_SOURCES for source in sources):
        return "synthetic"
    if any(source.startswith("vision-") for source in sources):
        return "observed"
    return "approximate"


def _normalize_segment(segment: dict[str, Any]) -> dict[str, Any]:
    x = max(0.0, min(1.0, _number(segment.get("x"))))
    y = max(0.0, min(1.0, _number(segment.get("y"))))
    width = max(0.0, min(1.0 - x, _number(segment.get("width"))))
    height = max(0.0, min(1.0 - y, _number(segment.get("height"))))
    result: dict[str, Any] = {
        "text": str(segment.get("text") or "").strip(),
        "orientation": _orientation(segment.get("orientation"), width=width, height=height),
        "x": round(x, 6),
        "y": round(y, 6),
        "width": round(width, 6),
        "height": round(height, 6),
    }
    for key in ("raw_text", "detector", "recognizer", "source", "orientation_reason"):
        value = str(segment.get(key) or "").strip()
        if value:
            result[key] = value
    if segment.get("confidence") is not None:
        result["confidence"] = round(max(0.0, min(1.0, _number(segment.get("confidence")))), 4)
    _copy_diagnostic_fields(segment, result)
    return result


def normalize_region(region: dict[str, Any], *, page_index: int, order: int) -> dict[str, Any]:
    x = max(0.0, min(1.0, _number(region.get("x"))))
    y = max(0.0, min(1.0, _number(region.get("y"))))
    width = max(0.0, min(1.0 - x, _number(region.get("width"))))
    height = max(0.0, min(1.0 - y, _number(region.get("height"))))
    orientation = _orientation(region.get("orientation"), width=width, height=height)
    raw_text = str(region.get("raw_text") or region.get("text") or "").strip()
    text = str(region.get("text") or "").strip()
    detector = str(region.get("detector") or region.get("source") or "unknown")
    confidence = max(0.0, min(1.0, _number(region.get("confidence"), 0.0)))
    identity = hashlib.sha1(
        f"{page_index}:{round(x, 5)}:{round(y, 5)}:{round(width, 5)}:"
        f"{round(height, 5)}:{text}".encode("utf-8", errors="replace")
    ).hexdigest()[:16]
    segments = [
        _normalize_segment(dict(segment))
        for segment in region.get("segments") or []
        if isinstance(segment, dict)
    ]
    result: dict[str, Any] = {
        "id": str(region.get("id") or f"p{page_index}-r{identity}"),
        "order": int(order),
        "text": text,
        "raw_text": raw_text,
        "orientation": orientation,
        "confidence": round(confidence, 4),
        "detector": detector,
        "x": round(x, 6),
        "y": round(y, 6),
        "width": round(width, 6),
        "height": round(height, 6),
        "geometry_status": _geometry_status(segments, region.get("geometry_status")),
        "word_geometry": (
            "mapped_segments"
            if segments and all(str(segment.get("text") or "").strip() for segment in segments)
            else "unlabeled_segments" if segments else "unavailable"
        ),
    }
    if segments:
        result["segments"] = segments
    for key in (
        "recognizer",
        "detector_version",
        "recognizer_version",
        "orientation_reason",
        "fallback_reason",
        "source",
    ):
        value = str(region.get(key) or "").strip()
        if value:
            result[key] = value
    provenance = region.get("provenance")
    if isinstance(provenance, dict):
        result["provenance"] = copy.deepcopy(provenance)
    _copy_diagnostic_fields(region, result)
    result["geometry_status"] = _geometry_status(segments, region.get("geometry_status"))
    error = str(region.get("error") or "").strip()
    if error:
        result["error"] = error
    if bool(region.get("fallback")):
        result["fallback"] = True
    return result


def normalize_page(
    page_index: int,
    regions: Iterable[dict[str, Any]],
    *,
    name: str = "",
    width: int = 0,
    height: int = 0,
) -> dict[str, Any]:
    normalized = [
        normalize_region(dict(region), page_index=int(page_index), order=order)
        for order, region in enumerate(regions)
        if isinstance(region, dict)
    ]
    return {
        "page_index": int(page_index),
        "name": str(name or ""),
        "width": max(0, int(width)),
        "height": max(0, int(height)),
        "regions": normalized,
        "text": "\n".join(row["text"] for row in normalized if row["text"]).strip(),
    }


def build_artifact(
    *,
    source_fingerprint: str,
    title: str,
    page_count: int,
    pages: Iterable[dict[str, Any]],
    detector: str,
    recognizer: str,
    created_at: float | None = None,
    detector_version: str = "",
    recognizer_version: str = "",
) -> dict[str, Any]:
    rows = sorted(
        (dict(page) for page in pages if isinstance(page, dict)),
        key=lambda page: int(page.get("page_index") or 0),
    )
    region_count = sum(len(page.get("regions") or []) for page in rows)
    fallback_count = sum(
        1
        for page in rows
        for region in page.get("regions") or []
        if isinstance(region, dict) and region.get("fallback")
    )
    engine = {
        "detector": str(detector or "unknown"),
        "recognizer": str(recognizer or "unknown"),
    }
    if detector_version:
        engine["detector_version"] = str(detector_version)
    if recognizer_version:
        engine["recognizer_version"] = str(recognizer_version)
    return {
        "schema": MANGA_OCR_ARTIFACT_SCHEMA,
        "source": {
            "fingerprint": str(source_fingerprint or ""),
            "title": str(title or ""),
            "page_count": max(0, int(page_count)),
        },
        "engine": engine,
        "created_at": float(created_at if created_at is not None else time.time()),
        "summary": {
            "processed_pages": len(rows),
            "region_count": region_count,
            "fallback_pages": fallback_count,
            "complete": bool(page_count > 0 and len(rows) >= int(page_count)),
        },
        "pages": rows,
    }


def write_artifact(path: Path, artifact: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Every publisher gets a unique staging file.  Shared ``artifact.json.tmp``
    # allowed concurrent writers to delete/replace each other's temporary file.
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(
                json.dumps(artifact, ensure_ascii=False, separators=(",", ":"))
            )
            temporary.flush()
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _upgrade_legacy_artifact(payload: dict[str, Any]) -> dict[str, Any]:
    pages = []
    for raw_page in payload.get("pages") or []:
        if not isinstance(raw_page, dict):
            continue
        pages.append(
            normalize_page(
                int(raw_page.get("page_index") or 0),
                [dict(item) for item in raw_page.get("regions") or [] if isinstance(item, dict)],
                name=str(raw_page.get("name") or ""),
                width=int(raw_page.get("width") or 0),
                height=int(raw_page.get("height") or 0),
            )
        )
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    engine = payload.get("engine") if isinstance(payload.get("engine"), dict) else {}
    upgraded = build_artifact(
        source_fingerprint=str(source.get("fingerprint") or ""),
        title=str(source.get("title") or ""),
        page_count=int(source.get("page_count") or len(pages)),
        pages=pages,
        detector=str(engine.get("detector") or "unknown"),
        recognizer=str(engine.get("recognizer") or "unknown"),
        created_at=_number(payload.get("created_at"), time.time()),
        detector_version=str(engine.get("detector_version") or ""),
        recognizer_version=str(engine.get("recognizer_version") or ""),
    )
    upgraded["migrated_from_schema"] = str(payload.get("schema") or "")
    return upgraded


def read_artifact(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    schema = str(payload.get("schema") or "")
    if schema not in {MANGA_OCR_ARTIFACT_SCHEMA, *_LEGACY_SCHEMAS}:
        return None
    if not isinstance(payload.get("pages"), list):
        return None
    if schema in _LEGACY_SCHEMAS:
        return _upgrade_legacy_artifact(payload)
    return payload


def artifact_page(artifact: dict[str, Any] | None, page_index: int) -> dict[str, Any] | None:
    if not isinstance(artifact, dict):
        return None
    return next(
        (
            dict(page)
            for page in artifact.get("pages") or []
            if isinstance(page, dict) and int(page.get("page_index") or 0) == int(page_index)
        ),
        None,
    )
