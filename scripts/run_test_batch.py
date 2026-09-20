#!/usr/bin/env python3
"""Run a deterministic batch of pytest files in isolated subprocesses.

Each test file gets its own process, PUDGE_HOME, runtime log and pytest basetemp.
A failure/timeout does not hide later file results. The runner writes a machine-
readable summary and returns non-zero when any selected file fails.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, required=True, help="0-based batch index")
    parser.add_argument("--batches", type=int, default=4, help="total number of batches")
    parser.add_argument("--timeout", type=float, default=900.0, help="timeout per test file in seconds")
    parser.add_argument("--results-dir", type=Path, default=None, help="directory for logs/JUnit/JSON")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER, help="extra pytest arguments after --")
    return parser.parse_args()


def discover_test_files(root: Path = Path("tests")) -> list[Path]:
    return sorted(path for path in root.rglob("test_*.py") if path.is_file())


def selected_files(files: list[Path], batch: int, batches: int) -> list[Path]:
    return [path for index, path in enumerate(files) if index % batches == batch]


def verify_partition(files: list[Path], batches: int) -> None:
    assignments = [path for batch in range(batches) for path in selected_files(files, batch, batches)]
    if len(assignments) != len(files) or set(assignments) != set(files):
        raise RuntimeError("batch partition does not cover all test files exactly once")


def _fingerprint_source_files(root: Path) -> list[Path]:
    selected: set[Path] = set()
    for directory in (root / "pudge", root / "scripts"):
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            selected.add(path)
    for name in ("pyproject.toml", "uv.lock", "Makefile", "config.example.toml"):
        path = root / name
        if path.is_file():
            selected.add(path)
    return sorted(selected, key=lambda path: path.as_posix())


def _installed_dependency_versions() -> list[str]:
    values: list[str] = []
    try:
        for distribution in importlib.metadata.distributions():
            name = str(distribution.metadata.get("Name") or "").strip().casefold()
            version = str(distribution.version or "").strip()
            if name:
                values.append(f"{name}=={version}")
    except Exception:
        return ["<dependency-metadata-unavailable>"]
    return sorted(set(values))


def tree_fingerprint(files: list[Path], *, root: Path = Path(".")) -> str:
    digest = hashlib.sha256()
    digest.update(sys.version.encode("utf-8", errors="replace"))
    digest.update(sys.executable.encode("utf-8", errors="replace"))
    hashed: set[Path] = set()
    for path in [*files, *_fingerprint_source_files(root)]:
        path = Path(path)
        identity = path.as_posix()
        if path in hashed:
            continue
        hashed.add(path)
        digest.update(identity.encode("utf-8", errors="replace"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
    for dependency in _installed_dependency_versions():
        digest.update(b"\0dep:")
        digest.update(dependency.encode("utf-8", errors="replace"))
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=no"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        commit, status = "", ""
    digest.update(commit.encode())
    digest.update(status.encode())
    return digest.hexdigest()


def _safe_name(path: Path) -> str:
    value = "__".join(path.with_suffix("").parts).replace("/", "_")
    if len(value) <= 96:
        return value
    digest = hashlib.sha1(value.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"{value[:80]}__{digest}"


def _junit_counts(path: Path) -> dict[str, int]:
    counts = {"passed": 0, "failed": 0, "skipped": 0, "xfailed": 0, "errors": 0}
    if not path.is_file():
        return counts
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        counts["errors"] = 1
        return counts
    cases = list(root.iter("testcase"))
    for case in cases:
        if case.find("failure") is not None:
            counts["failed"] += 1
        elif case.find("error") is not None:
            counts["errors"] += 1
        elif (skipped := case.find("skipped")) is not None:
            marker = " ".join(
                str(value or "")
                for value in (skipped.get("type"), skipped.get("message"), skipped.text)
            ).casefold()
            if "xfail" in marker:
                counts["xfailed"] += 1
            else:
                counts["skipped"] += 1
        else:
            counts["passed"] += 1
    if not cases:
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        if suites and sum(int(item.get("tests", "0") or 0) for item in suites) == 0:
            counts["errors"] = max(1, counts["errors"])
    return counts


def _write_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def run_file(path: Path, *, results_dir: Path, timeout: float, pytest_args: list[str]) -> dict[str, Any]:
    name = _safe_name(path)
    stdout_path = results_dir / "logs" / f"{name}.stdout.log"
    stderr_path = results_dir / "logs" / f"{name}.stderr.log"
    junit_path = results_dir / "junit" / f"{name}.xml"
    runtime_log_path = results_dir / "runtime" / f"{name}.runtime.log"
    started = time.monotonic()
    timed_out = False
    returncode = 1

    # Keep test HOME/TMP/basetemp outside the report tree. Some tests assert
    # that runtime artifacts never land in Downloads; report directories are
    # allowed there, isolated runtime state is not.
    system_tmp = Path("/private/tmp") if sys.platform == "darwin" and Path("/private/tmp").is_dir() else None
    with tempfile.TemporaryDirectory(prefix=f"pudge-{name}-", dir=system_tmp) as isolated_raw:
        isolated = Path(isolated_raw)
        home = isolated / "home"
        tmp = isolated / "tmp"
        basetemp = isolated / "pytest-basetemp"
        transient_runtime_log = isolated / "runtime.log"
        home.mkdir(parents=True)
        tmp.mkdir(parents=True)

        # Tests must never steal focus by launching Finder (or xdg-open on
        # non-macOS development hosts).  A few integration tests intentionally
        # execute real application methods that call `open -R`; prepend a
        # per-test no-op shim so those side effects stay inside the subprocess.
        gui_stub_dir = isolated / "gui-stubs"
        gui_stub_dir.mkdir()
        open_stub = gui_stub_dir / "open"
        open_stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        open_stub.chmod(0o755)

        env = os.environ.copy()
        env.update(
            {
                "PUDGE_HOME": str(home),
                "PUDGE_RUNTIME_LOG_PATH": str(transient_runtime_log),
                "TMPDIR": str(tmp),
                "TEMP": str(tmp),
                "TMP": str(tmp),
                "PUDGE_TEST_ISOLATED": "1",
                "PATH": str(gui_stub_dir) + os.pathsep + env.get("PATH", ""),
            }
        )
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--durations=15",
            f"--basetemp={basetemp}",
            f"--junitxml={junit_path}",
            str(path),
            *pytest_args,
        ]
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
            try:
                completed = subprocess.run(
                    cmd,
                    stdout=stdout,
                    stderr=stderr,
                    env=env,
                    check=False,
                    timeout=max(1.0, float(timeout)),
                    text=True,
                )
                returncode = int(completed.returncode)
            except subprocess.TimeoutExpired:
                timed_out = True
                returncode = 124
                stderr.write(f"\nTIMEOUT after {timeout:.1f}s\n")
        if transient_runtime_log.is_file():
            shutil.copy2(transient_runtime_log, runtime_log_path)

    counts = _junit_counts(junit_path)
    if returncode != 0 and not timed_out and not (counts["failed"] or counts["errors"]):
        counts["errors"] += 1
    return {
        "path": str(path),
        "returncode": returncode,
        "timeout": timed_out,
        "duration_seconds": round(time.monotonic() - started, 3),
        **counts,
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "junit": str(junit_path),
        "runtime_log": str(runtime_log_path) if runtime_log_path.is_file() else "",
    }


def _tail(path: Path, *, lines: int = 40) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-max(1, int(lines)):])


def main() -> int:
    args = parse_args()
    if args.batches < 1:
        raise SystemExit("--batches must be >= 1")
    if not 0 <= args.batch < args.batches:
        raise SystemExit("--batch must satisfy 0 <= batch < batches")
    if args.timeout <= 0:
        raise SystemExit("--timeout must be > 0")

    files = discover_test_files()
    verify_partition(files, args.batches)
    selected = selected_files(files, args.batch, args.batches)
    fingerprint = tree_fingerprint(files)

    results_dir = args.results_dir or Path(tempfile.mkdtemp(prefix="pudge-test-batch-"))
    results_dir = results_dir.expanduser().resolve()
    (results_dir / "logs").mkdir(parents=True, exist_ok=True)
    (results_dir / "junit").mkdir(parents=True, exist_ok=True)
    (results_dir / "runtime").mkdir(parents=True, exist_ok=True)

    manifest = {
        "batch": args.batch,
        "batches": args.batches,
        "python": sys.version,
        "python_executable": sys.executable,
        "tree_fingerprint": fingerprint,
        "all_files": [str(path) for path in files],
        "selected_files": [str(path) for path in selected],
    }
    _write_json(results_dir / "manifest.json", manifest)

    print(f"Batch {args.batch + 1}/{args.batches}: {len(selected)} files")
    print(f"Results: {results_dir}")
    print(f"Tree fingerprint: {fingerprint[:16]}")
    if not selected:
        _write_json(results_dir / "summary.json", {**manifest, "files": [], "totals": {}})
        return 0

    rows: list[dict[str, Any]] = []
    for index, path in enumerate(selected, start=1):
        print(f"[{index}/{len(selected)}] {path}", flush=True)
        row = run_file(
            path,
            results_dir=results_dir,
            timeout=args.timeout,
            pytest_args=list(args.pytest_args),
        )
        rows.append(row)
        state = "TIMEOUT" if row["timeout"] else "PASS" if row["returncode"] == 0 else "FAIL"
        print(f"  {state} {row['duration_seconds']:.1f}s", flush=True)
        if row["returncode"] != 0:
            for label, key in (("stdout", "stdout"), ("stderr", "stderr")):
                tail = _tail(Path(str(row[key])))
                if tail:
                    print(f"  --- {label} tail ---\n{tail}", flush=True)
        _write_json(results_dir / "summary.partial.json", {**manifest, "files": rows})

    totals = {
        key: sum(int(row[key]) for row in rows)
        for key in ("passed", "failed", "skipped", "xfailed", "errors")
    }
    totals["timeouts"] = sum(bool(row["timeout"]) for row in rows)
    totals["files"] = len(rows)
    totals["failed_files"] = sum(row["returncode"] != 0 for row in rows)
    summary = {**manifest, "files": rows, "totals": totals}
    _write_json(results_dir / "summary.json", summary)
    (results_dir / "summary.partial.json").unlink(missing_ok=True)
    print("Totals: " + " ".join(f"{key}={value}" for key, value in totals.items()))
    return 1 if totals["failed_files"] or totals["timeouts"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
