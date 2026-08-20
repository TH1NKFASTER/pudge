#!/usr/bin/env python3
"""Update Pudge's source and README version declarations."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise SystemExit(f"Expected exactly one version match in {path}, got {count}")
    path.write_text(updated, encoding="utf-8")


def ensure_changelog_entry(version: str) -> None:
    path = ROOT / "CHANGELOG.md"
    text = path.read_text(encoding="utf-8")
    heading = f"## v{version}"
    if heading in text:
        return
    marker = "## Unreleased\n"
    if marker not in text:
        raise SystemExit("CHANGELOG.md has no Unreleased section")
    text = text.replace(
        marker,
        f"{marker}\n{heading}\n\n- TODO: release notes\n",
        1,
    )
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("version", help="new version, e.g. 0.6.69")
    args = parser.parse_args()

    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        raise SystemExit("Version must look like 0.6.69")

    replace_once(
        ROOT / "pudge" / "__init__.py",
        r'^__version__\s*=\s*["\'][^"\']+["\']',
        f'__version__ = "{args.version}"',
    )
    replace_once(
        ROOT / "pyproject.toml",
        r'^version\s*=\s*"[^"]+"',
        f'version = "{args.version}"',
    )

    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    text = re.sub(r"Current version: \*\*[^*]+\*\*", f"Current version: **{args.version}**", text, count=1)
    text = re.sub(r"pudge-macos-v\d+\.\d+\.\d+\.zip", f"pudge-macos-v{args.version}.zip", text, count=1)
    readme.write_text(text, encoding="utf-8")
    ensure_changelog_entry(args.version)

    print(f"Bumped Pudge to {args.version}")
    print("Next: replace the changelog TODO, refresh uv.lock, then run make quality test-batches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
