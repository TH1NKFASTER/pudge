from __future__ import annotations

import json
import platform
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

from rapidfuzz.distance import Levenshtein

from . import __version__
from .manga_ocr_worker import _PIPELINE_GENERATION, _PIPELINE_WORKER

SCHEMA_VERSION = "pudge_manga_benchmark/v1"


def normalize_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return "".join(character for character in text if not character.isspace())


def edit_distance(reference: object, prediction: object) -> int:
    return int(Levenshtein.distance(normalize_text(reference), normalize_text(prediction)))


def _rect(item: dict[str, Any]) -> tuple[float, float, float, float]:
    x1 = float(item.get("x") or 0.0)
    y1 = float(item.get("y") or 0.0)
    width = max(0.0, float(item.get("width") or 0.0))
    height = max(0.0, float(item.get("height") or 0.0))
    return x1, y1, x1 + width, y1 + height


def box_iou(left: dict[str, Any], right: dict[str, Any]) -> float:
    lx1, ly1, lx2, ly2 = _rect(left)
    rx1, ry1, rx2, ry2 = _rect(right)
    intersection = max(0.0, min(lx2, rx2) - max(lx1, rx1)) * max(
        0.0, min(ly2, ry2) - max(ly1, ry1)
    )
    left_area = max(0.0, lx2 - lx1) * max(0.0, ly2 - ly1)
    right_area = max(0.0, rx2 - rx1) * max(0.0, ry2 - ry1)
    union = left_area + right_area - intersection
    return intersection / union if union > 0.0 else 0.0


def greedy_match(
    ground_truth: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    *,
    iou_threshold: float = 0.5,
) -> list[tuple[int, int, float]]:
    candidates: list[tuple[float, int, int]] = []
    for gt_index, gt in enumerate(ground_truth):
        for pred_index, pred in enumerate(predictions):
            iou = box_iou(gt, pred)
            if iou >= iou_threshold:
                candidates.append((iou, gt_index, pred_index))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    used_gt: set[int] = set()
    used_pred: set[int] = set()
    matched: list[tuple[int, int, float]] = []
    for iou, gt_index, pred_index in candidates:
        if gt_index in used_gt or pred_index in used_pred:
            continue
        used_gt.add(gt_index)
        used_pred.add(pred_index)
        matched.append((gt_index, pred_index, iou))
    return sorted(matched, key=lambda item: (item[0], item[1]))


def _git_commit() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def benchmark_metadata(
    benchmark: str,
    *,
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "benchmark": benchmark,
        "pudge": {
            "version": __version__,
            "pipeline_worker": _PIPELINE_WORKER,
            "pipeline_generation": _PIPELINE_GENERATION,
            "git_commit": _git_commit(),
        },
        "runtime": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "parameters": dict(parameters or {}),
    }


def _numeric_metrics(value: dict[str, Any], *, prefix: str = "") -> dict[str, float]:
    output: dict[str, float] = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)):
            output[path] = float(item)
        elif isinstance(item, dict):
            output.update(_numeric_metrics(item, prefix=path))
    return output


def compare_reports(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    if old.get("schema") != SCHEMA_VERSION or new.get("schema") != SCHEMA_VERSION:
        raise ValueError("benchmark report schema mismatch")
    if old.get("benchmark") != new.get("benchmark"):
        raise ValueError("benchmark ids do not match")
    old_parameters = old.get("parameters") if isinstance(old.get("parameters"), dict) else {}
    new_parameters = new.get("parameters") if isinstance(new.get("parameters"), dict) else {}
    if old.get("benchmark") == "manga109_fullpage" and float(
        old_parameters.get("iou_threshold", 0.5)
    ) != float(new_parameters.get("iou_threshold", 0.5)):
        raise ValueError("Manga109 IoU thresholds do not match")

    old_metrics = _numeric_metrics(old.get("metrics") or {})
    new_metrics = _numeric_metrics(new.get("metrics") or {})
    metric_deltas = {
        key: new_metrics[key] - old_metrics[key]
        for key in sorted(old_metrics.keys() & new_metrics.keys())
    }
    result: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "benchmark": old.get("benchmark"),
        "metric_deltas": metric_deltas,
    }

    old_pages = {
        str(item.get("key")): item
        for item in old.get("pages") or []
        if isinstance(item, dict) and item.get("key") is not None
    }
    new_pages = {
        str(item.get("key")): item
        for item in new.get("pages") or []
        if isinstance(item, dict) and item.get("key") is not None
    }
    if old_pages and new_pages:
        improved = regressed = unchanged = 0
        changes: list[dict[str, Any]] = []
        for key in sorted(old_pages.keys() & new_pages.keys()):
            old_distance = int(old_pages[key].get("end_to_end_edit_distance") or 0)
            new_distance = int(new_pages[key].get("end_to_end_edit_distance") or 0)
            delta = new_distance - old_distance
            if delta < 0:
                improved += 1
            elif delta > 0:
                regressed += 1
            else:
                unchanged += 1
            if delta:
                changes.append({"key": key, "old": old_distance, "new": new_distance, "delta": delta})
        changes.sort(key=lambda item: (-abs(int(item["delta"])), str(item["key"])))
        result["pages"] = {
            "improved": improved,
            "regressed": regressed,
            "unchanged": unchanged,
            "changes": changes,
        }
    return result


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"Expected JSON object: {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
