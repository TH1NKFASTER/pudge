#!/usr/bin/env python3
"""Keep the repository and packaged configuration examples identical."""

from __future__ import annotations

import argparse
import difflib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "config.example.toml"
PACKAGED = ROOT / "pudge" / "config.example.toml"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    source = SOURCE.read_text(encoding="utf-8")
    packaged = PACKAGED.read_text(encoding="utf-8")
    if source == packaged:
        return 0
    if args.check:
        print(
            "".join(
                difflib.unified_diff(
                    packaged.splitlines(keepends=True),
                    source.splitlines(keepends=True),
                    fromfile=str(PACKAGED.relative_to(ROOT)),
                    tofile=str(SOURCE.relative_to(ROOT)),
                )
            ),
            end="",
        )
        return 1
    PACKAGED.write_text(source, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
