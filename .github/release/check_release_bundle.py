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
LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


def validate(root: Path) -> list[str]:
    errors: list[str] = []
    for rel in REQUIRED:
        if not (root / rel).exists():
            errors.append(f"missing required release path: {rel}")
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
