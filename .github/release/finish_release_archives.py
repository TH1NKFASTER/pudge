#!/usr/bin/env python3
"""Publish identical current and legacy updater archives with portable checksums."""
from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path


def main() -> int:
    archive = Path(sys.argv[1])
    legacy = archive.with_name(sys.argv[2])
    shutil.copyfile(archive, legacy)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    for path in (archive, legacy):
        path.with_suffix(path.suffix + ".sha256").write_text(
            f"{digest}  {path.name}\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
