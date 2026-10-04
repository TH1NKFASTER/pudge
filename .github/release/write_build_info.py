#!/usr/bin/env python3
"""Fingerprint the package that is about to be built into a release wheel."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("version")
    parser.add_argument("--revision")
    args = parser.parse_args()
    revision = args.revision
    if not revision:
        result = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"], cwd=args.package.parent,
            capture_output=True, text=True, check=False, timeout=5,
        )
        revision = result.stdout.strip() if result.returncode == 0 else "source"
    files = {
        path.relative_to(args.package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(args.package.rglob("*"))
        if path.is_file() and path.name != "build-info.json"
        and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
        # Finder metadata (.DS_Store) and other dotfiles never ship in the wheel.
        and not any(part.startswith(".") for part in path.relative_to(args.package).parts)
    }
    payload = {
        "id": f"{args.version}-{revision}",
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "installed_at": "",
        "files": files,
    }
    (args.package / "build-info.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
