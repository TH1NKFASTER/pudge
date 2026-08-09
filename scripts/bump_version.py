#!/usr/bin/env python3
"""Update Pudge's source and README version declarations."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text()
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise SystemExit(f"Expected exactly one version match in {path}, got {count}")
    path.write_text(updated)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("version", help="new version, e.g. 0.6.69")
    args = parser.parse_args()

    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        raise SystemExit("Version must look like 0.6.69")

    replace_once(
        ROOT / "anime_mpv" / "__init__.py",
        r'^__version__\s*=\s*["\'][^"\']+["\']',
        f'__version__ = "{args.version}"',
    )
    replace_once(
        ROOT / "pyproject.toml",
        r'^version\s*=\s*"[^"]+"',
        f'version = "{args.version}"',
    )

    readme = ROOT / "README.md"
    text = readme.read_text()
    text = re.sub(r"Current version: \*\*[^*]+\*\*", f"Current version: **{args.version}**", text, count=1)
    text = re.sub(r"pudge-macos-v\d+\.\d+\.\d+\.zip", f"pudge-macos-v{args.version}.zip", text, count=1)
    readme.write_text(text)

    print(f"Bumped Pudge to {args.version}")
    print(f"Next: make test-batches && git add -A && git commit -m 'Pudge v{args.version}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
