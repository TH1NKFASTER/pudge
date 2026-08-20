#!/usr/bin/env python3
"""Run a deterministic subset of test files.

Splitting by sorted file index keeps all tests covered exactly once while making
local and GitHub Actions runs easy to parallelize.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, required=True, help="0-based batch index")
    parser.add_argument("--batches", type=int, default=4, help="total number of batches")
    parser.add_argument("pytest_args", nargs="*", help="extra pytest arguments")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.batches < 1:
        raise SystemExit("--batches must be >= 1")
    if not 0 <= args.batch < args.batches:
        raise SystemExit("--batch must satisfy 0 <= batch < batches")

    files = sorted(Path("tests").glob("test_*.py"))
    selected = [path for index, path in enumerate(files) if index % args.batches == args.batch]

    if not selected:
        print(f"Batch {args.batch + 1}/{args.batches}: no tests")
        return 0

    print(f"Batch {args.batch + 1}/{args.batches}: {len(selected)} files")
    for path in selected:
        print(f"  {path}")

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--durations=15",
        *(str(path) for path in selected),
        *args.pytest_args,
    ]
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
