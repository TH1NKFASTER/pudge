"""Identify the code imported by this process, including same-version patches."""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path


def read_build_identity(package_root: Path) -> dict:
    root = Path(package_root).resolve()
    try:
        payload = json.loads((root / "build-info.json").read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("files"), dict):
            raise ValueError("invalid build metadata")
        build_id = str(payload["id"])
    except (OSError, ValueError, KeyError, TypeError):
        return {"id": "unknown", "display": "unknown", "verified": False}
    mismatches = []
    for name, expected in payload["files"].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            mismatches.append(name)
            continue
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            actual = "missing"
        if actual != expected:
            mismatches.append(name)
    return {
        "id": build_id,
        "display": build_id + (" (modified)" if mismatches else ""),
        "built_at": str(payload.get("built_at") or ""),
        "installed_at": str(payload.get("installed_at") or ""),
        "verified": not mismatches,
        "mismatches": mismatches,
    }


@lru_cache(maxsize=1)
def build_identity() -> dict:
    return read_build_identity(Path(__file__).resolve().parent)
