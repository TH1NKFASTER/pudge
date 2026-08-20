#!/usr/bin/env python3
"""Validate version, changelog and reproducible-release metadata."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = str(project["project"]["version"])
    package = (ROOT / "pudge" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)', package, re.MULTILINE)
    if match is None or match.group(1) != version:
        raise SystemExit("pyproject.toml and pudge.__version__ disagree")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if f"## v{version}" not in changelog:
        raise SystemExit(f"CHANGELOG.md has no v{version} entry")
    if "TODO: release notes" in changelog:
        raise SystemExit("CHANGELOG.md still contains TODO release notes")
    if not (ROOT / "uv.lock").is_file():
        raise SystemExit("uv.lock is missing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
