#!/usr/bin/env python3
"""Validate that a staged source release is self-contained for documented tooling."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

REQUIRED = (
    "pudge",
    "tests",
    ".github/release",
    "pudge/install_checks.py",
    "Makefile",
    "install.sh",
    "build_release.sh",
    "pudge/legacy_install.py",
    ".github/workflows/release.yml",
)
# Removed from Pudge: a developer folder that still holds them (built without git, or
# copied over an older version) must not ship them again.
RETIRED = (
    "pudge/web/companion",
    "pudge/mobile_sync.py",
    "pudge/mobile_sync_http.py",
    "pudge/companion_streaming.py",
    "pudge/web/library.js",
    "pudge/alignment_replay.py",
    "pudge/manga_review_diff.py",
    "pudge/ocr_geometry.py",
    "pudge/subtitles/normalization.py",
    "anime_mpv",
    "rename_brand.py",
    "brand_migration.py",
    "MOBILE_SYNC_PROTOCOL.md",
    "docs/LLM_HANDOFF.md",
    "tests/golden",
)
LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


def validate(root: Path) -> list[str]:
    errors: list[str] = []
    for rel in REQUIRED:
        if not (root / rel).exists():
            errors.append(f"missing required release path: {rel}")
    for rel in RETIRED:
        if (root / rel).exists() or (root / rel).is_symlink():
            errors.append(f"retired path in release: {rel}")
    for doc in root.rglob("*.md"):
        try:
            text = doc.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for raw in LINK_RE.findall(text):
            target = raw.strip().strip("<>").split("#", 1)[0].strip()
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = unquote(target)
            candidate = (doc.parent / target).resolve()
            try:
                candidate.relative_to(root.resolve())
            except ValueError:
                errors.append(f"{doc.relative_to(root)}: link escapes bundle: {raw}")
                continue
            if not candidate.exists():
                errors.append(f"{doc.relative_to(root)}: broken local link: {raw}")
    return errors


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    errors = validate(root)
    if errors:
        print("Release bundle validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Release bundle OK: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
