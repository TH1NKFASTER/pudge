from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any, Iterable


MANGA_OCR_ARTIFACT_SCHEMA = "pudge-manga-ocr-v1"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def normalize_region(region: dict[str, Any], *, page_index: int, order: int) -> dict[str, Any]:
    x = max(0.0, min(1.0, _number(region.get("x"))))
    y = max(0.0, min(1.0, _number(region.get("y"))))
    width = max(0.0, min(1.0 - x, _number(region.get("width"))))
    height = max(0.0, min(1.0 - y, _number(region.get("height"))))
    orientation = str(region.get("orientation") or "").casefold()
    if orientation not in {"vertical", "horizontal", "mixed"}:
        orientation = "vertical" if height > width * 1.15 else "horizontal"
    raw_text = str(region.get("raw_text") or region.get("text") or "").strip()
    text = str(region.get("text") or "").strip()
    detector = str(region.get("detector") or region.get("source") or "unknown")
    confidence = max(0.0, min(1.0, _number(region.get("confidence"), 0.0)))
    identity = hashlib.sha1(
        f"{page_index}:{round(x, 5)}:{round(y, 5)}:{round(width, 5)}:"
        f"{round(height, 5)}:{text}".encode("utf-8", errors="replace")
    ).hexdigest()[:16]
    result = {
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
    }
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
    return {
        "schema": MANGA_OCR_ARTIFACT_SCHEMA,
        "source": {
            "fingerprint": str(source_fingerprint or ""),
            "title": str(title or ""),
            "page_count": max(0, int(page_count)),
        },
        "engine": {
            "detector": str(detector or "unknown"),
            "recognizer": str(recognizer or "unknown"),
        },
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
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(artifact, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)


def read_artifact(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != MANGA_OCR_ARTIFACT_SCHEMA:
        return None
    if not isinstance(payload.get("pages"), list):
        return None
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
