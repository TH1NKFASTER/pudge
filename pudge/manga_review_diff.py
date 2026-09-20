from __future__ import annotations

import html
import json
import re
import shutil
from pathlib import Path
from typing import Any

from PIL import Image

from .manga_benchmark import box_iou, normalize_text

SCHEMA_VERSION = "pudge_manga_review_diff/v1"
_PAGE_RE = re.compile(r"^page_(\d{3})\.json$")


def _load_page(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    backend = payload.get("backend_ocr") if isinstance(payload, dict) else None
    regions = backend.get("regions") if isinstance(backend, dict) else None
    return [dict(item) for item in regions or [] if isinstance(item, dict)]


def _page_files(root: Path) -> dict[int, Path]:
    output: dict[int, Path] = {}
    for path in root.glob("page_*.json"):
        match = _PAGE_RE.match(path.name)
        if match:
            output[int(match.group(1))] = path
    return output


def _summary_region(item: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "index": index,
        "text": str(item.get("text") or ""),
        "x": float(item.get("x") or 0.0),
        "y": float(item.get("y") or 0.0),
        "width": float(item.get("width") or 0.0),
        "height": float(item.get("height") or 0.0),
        "confidence": item.get("confidence"),
        "source": str(item.get("source") or ""),
        "selected_hypothesis_id": str(item.get("selected_hypothesis_id") or ""),
    }


def _geometry_equal(left: dict[str, Any], right: dict[str, Any], *, tolerance: float = 1e-6) -> bool:
    return all(
        abs(float(left.get(key) or 0.0) - float(right.get(key) or 0.0)) <= tolerance
        for key in ("x", "y", "width", "height")
    )


def _match_regions(
    old_regions: list[dict[str, Any]],
    new_regions: list[dict[str, Any]],
) -> list[tuple[int, int, float]]:
    candidates: list[tuple[int, float, int, int]] = []
    for old_index, old in enumerate(old_regions):
        old_text = normalize_text(old.get("text"))
        for new_index, new in enumerate(new_regions):
            iou = box_iou(old, new)
            new_text = normalize_text(new.get("text"))
            exact_text = bool(old_text and old_text == new_text)
            if exact_text and iou >= 0.10:
                candidates.append((0, -iou, old_index, new_index))
            elif iou >= 0.30:
                candidates.append((1, -iou, old_index, new_index))
    candidates.sort()
    used_old: set[int] = set()
    used_new: set[int] = set()
    matched: list[tuple[int, int, float]] = []
    for _kind, negative_iou, old_index, new_index in candidates:
        if old_index in used_old or new_index in used_new:
            continue
        used_old.add(old_index)
        used_new.add(new_index)
        matched.append((old_index, new_index, -negative_iou))
    return sorted(matched)


def compare_review_directories(old_root: Path, new_root: Path) -> dict[str, Any]:
    old_files = _page_files(old_root)
    new_files = _page_files(new_root)
    page_numbers = sorted(old_files.keys() | new_files.keys())
    summary = {
        "pages": len(page_numbers),
        "unchanged": 0,
        "text_changed": 0,
        "geometry_changed": 0,
        "added": 0,
        "removed": 0,
    }
    pages: list[dict[str, Any]] = []
    for page in page_numbers:
        old_regions = _load_page(old_files[page]) if page in old_files else []
        new_regions = _load_page(new_files[page]) if page in new_files else []
        matches = _match_regions(old_regions, new_regions)
        matched_old = {old_index for old_index, _, _ in matches}
        matched_new = {new_index for _, new_index, _ in matches}
        changes: list[dict[str, Any]] = []
        for old_index, new_index, iou in matches:
            old = old_regions[old_index]
            new = new_regions[new_index]
            old_text = normalize_text(old.get("text"))
            new_text = normalize_text(new.get("text"))
            if old_text != new_text:
                kind = "text_changed"
            elif not _geometry_equal(old, new):
                kind = "geometry_changed"
            else:
                summary["unchanged"] += 1
                continue
            summary[kind] += 1
            changes.append(
                {
                    "kind": kind,
                    "iou": iou,
                    "old": _summary_region(old, old_index),
                    "new": _summary_region(new, new_index),
                }
            )
        for old_index, old in enumerate(old_regions):
            if old_index not in matched_old:
                summary["removed"] += 1
                changes.append({"kind": "removed", "old": _summary_region(old, old_index)})
        for new_index, new in enumerate(new_regions):
            if new_index not in matched_new:
                summary["added"] += 1
                changes.append({"kind": "added", "new": _summary_region(new, new_index)})
        order = {"text_changed": 0, "geometry_changed": 1, "removed": 2, "added": 3}
        changes.sort(key=lambda item: (order[str(item["kind"])], int((item.get("old") or item.get("new"))["index"])))
        pages.append({"page": page, "changes": changes})
    return {
        "schema": SCHEMA_VERSION,
        "old_root": str(old_root),
        "new_root": str(new_root),
        "summary": summary,
        "pages": pages,
    }


def _crop_region(image_path: Path, region: dict[str, Any], output: Path) -> None:
    with Image.open(image_path) as image:
        width, height = image.size
        x = float(region.get("x") or 0.0)
        y = float(region.get("y") or 0.0)
        w = max(0.0, float(region.get("width") or 0.0))
        h = max(0.0, float(region.get("height") or 0.0))
        left = max(0, int((x - 0.01) * width))
        right = min(width, int((x + w + 0.01) * width) + 1)
        top = max(0, int((1.0 - y - h - 0.01) * height))
        bottom = min(height, int((1.0 - y + 0.01) * height) + 1)
        image.crop((left, top, right, bottom)).convert("RGB").save(output, quality=92)


def _markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Manga OCR review diff",
        "",
        f"- Pages: {summary['pages']}",
        f"- Unchanged regions: {summary['unchanged']}",
        f"- Text changed: {summary['text_changed']}",
        f"- Geometry changed: {summary['geometry_changed']}",
        f"- Added: {summary['added']}",
        f"- Removed: {summary['removed']}",
        "",
    ]
    for page in report["pages"]:
        if not page["changes"]:
            continue
        lines.extend([f"## page {int(page['page']):03d}", ""])
        for change in page["changes"]:
            old = change.get("old") or {}
            new = change.get("new") or {}
            lines.append(
                f"- `{change['kind']}`: `{old.get('text', '')}` → `{new.get('text', '')}`"
            )
        lines.append("")
    return "\n".join(lines)


def _html(report: dict[str, Any]) -> str:
    blocks: list[str] = []
    for page in report["pages"]:
        if not page["changes"]:
            continue
        page_number = int(page["page"])
        rows = "".join(
            "<li><b>{}</b>: <code>{}</code> → <code>{}</code></li>".format(
                html.escape(str(change["kind"])),
                html.escape(str((change.get("old") or {}).get("text", ""))),
                html.escape(str((change.get("new") or {}).get("text", ""))),
            )
            for change in page["changes"]
        )
        blocks.append(
            f"<section><h2>page {page_number:03d}</h2><ul>{rows}</ul>"
            f'<div class="pair"><img src="old/page_{page_number:03d}_overlay.jpg">'
            f'<img src="new/page_{page_number:03d}_overlay.jpg"></div></section>'
        )
    return """<!doctype html><meta charset="utf-8"><title>Manga OCR review diff</title>
<style>body{font-family:system-ui;margin:24px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:12px}.pair img{width:100%;height:auto}code{white-space:pre-wrap}</style>
<h1>Manga OCR review diff</h1>""" + "".join(blocks)


def write_review_diff_report(old_root: Path, new_root: Path, output: Path) -> dict[str, Any]:
    report = compare_review_directories(old_root, new_root)
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "report.md").write_text(_markdown(report), encoding="utf-8")
    (output / "index.html").write_text(_html(report), encoding="utf-8")
    crops = output / "crops"
    crops.mkdir(exist_ok=True)
    for side, root in (("old", old_root), ("new", new_root)):
        side_dir = output / side
        side_dir.mkdir(exist_ok=True)
        for page in report["pages"]:
            if not page["changes"]:
                continue
            page_number = int(page["page"])
            overlay = root / f"page_{page_number:03d}_overlay.jpg"
            if overlay.is_file():
                shutil.copy2(overlay, side_dir / overlay.name)
        for page in report["pages"]:
            page_number = int(page["page"])
            image_path = root / f"page_{page_number:03d}.jpg"
            if not image_path.is_file():
                continue
            for change_index, change in enumerate(page["changes"]):
                region = change.get(side)
                if not isinstance(region, dict):
                    continue
                _crop_region(
                    image_path,
                    region,
                    crops / f"page_{page_number:03d}_{change_index:02d}_{side}.jpg",
                )
    return report
