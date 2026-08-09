#!/usr/bin/env python3
"""Verify that a Git tag matches all source version declarations."""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def package_version() -> str:
    init_file = ROOT / "anime_mpv" / "__init__.py"
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', init_file.read_text(), re.MULTILINE)
    if not match:
        raise SystemExit(f"Could not read __version__ from {init_file}")
    return match.group(1)


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tag")
    args = parser.parse_args()

    package = package_version()
    project = project_version()
    tag = args.tag.removeprefix("v")

    if package != project:
        raise SystemExit(f"Version mismatch: anime_mpv.__version__={package!r}, pyproject={project!r}")
    if tag != package:
        raise SystemExit(f"Tag {args.tag!r} does not match source version {package!r}")

    print(f"Release version OK: {args.tag} == {package} == pyproject")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
