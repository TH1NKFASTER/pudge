from __future__ import annotations
import unicodedata
from typing import Any

def normalize_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return "".join(character for character in text if not character.isspace())


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
