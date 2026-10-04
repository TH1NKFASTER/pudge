#!/usr/bin/env python3
"""Enforce procedural OCR regression fixtures before running the public suite."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

LEGACY_TESTS = (
    "test_v0727_manga_golden_p00_39.py",
    "test_v0727_manga_phase34_v96p21_peer_supported_wide_exact_crop.py",
    "test_v0727_manga_phase34_v96p21_wide_donor_physical_lane.py",
    "test_v0727_manga_phase34_v96p24_unsupported_art_retry.py",
)
PAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".tif",
    ".tiff",
    ".bmp",
    ".avif",
    ".cbz",
    ".cbr",
    ".rar",
    ".zip",
    ".pdf",
}
PRIVATE_DIRS = (
    "golden",
    "fixtures/v96p21",
    "fixtures/v96p24",
    "fixtures/v96p27",
    "fixtures/v96p28",
    "fixtures/v96p29",
    "fixtures/v96p34",
)


def _contains_regions(value):
    if isinstance(value, dict):
        return "regions" in value or any(_contains_regions(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_regions(v) for v in value)
    return False


def check(project: Path) -> list[str]:
    tests = project / "tests"
    errors = []
    for name in LEGACY_TESTS:
        if (tests / name).exists():
            errors.append("Retired private OCR test remains: tests/" + name)
    for name in PRIVATE_DIRS:
        directory = tests / name
        if directory.exists() and any(directory.iterdir()):
            errors.append("Private OCR data remains in the test tree: tests/" + name)
    for path in sorted(tests.rglob("*")):
        if any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(tests).parts):
            continue
        relative = str(path.relative_to(project))
        if path.is_file() and path.suffix.lower() in PAGE_EXTENSIONS:
            errors.append("Page/image fixture must be generated at test time: " + relative)
        elif path.is_file() and path.suffix == ".json":
            value = json.loads(path.read_text(encoding="utf-8"))
            if _contains_regions(value) and not (isinstance(value, dict) and value.get("synthetic") is True):
                errors.append("OCR region fixture needs an explicit synthetic origin: " + relative)
        elif path.is_file() and path.suffix == ".py":
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            ocr_module = any(
                isinstance(node, ast.ImportFrom)
                and (
                    "manga_ocr_worker" in (node.module or "")
                    or any(alias.name == "manga_ocr_worker" for alias in node.names)
                )
                for node in ast.walk(tree)
            )
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                # An inherited/private fixture root is never a public test prerequisite.
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("get", "getenv")
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and re.fullmatch(r"PUDGE_.*_FIXTURES", node.args[0].value)
                ):
                    errors.append("Private OCR fixture environment dependency: " + relative)
                # Worker image tests must draw their input in memory. Service tests
                # may still open temporary images they generated themselves.
                if (
                    ocr_module
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "Image"
                    and node.func.attr == "open"
                ):
                    errors.append("OCR worker input must be procedurally generated: " + relative)
    return sorted(set(errors))


if __name__ == "__main__":
    violations = check(Path(__file__).resolve().parents[1])
    if violations:
        raise SystemExit("\n".join(violations))
    print("OCR fixture policy passed: generated pages only.")
