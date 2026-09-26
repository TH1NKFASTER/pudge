"""Local-only OCR throughput/RSS benchmark; never exports page images or OCR text.

Run with the installed Pudge runtime Python, e.g.:
  ~/.local/share/pudge/venv/bin/python scripts/benchmark_ocr_parallel.py

Select an existing CBZ or a Pudge collection ZIP containing the private OCR
reference pages. Both runs use the same temporary CBZ and detection inputs.
The temporary OCR output is destroyed before the summary is written.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

_GIB = 1024 ** 3
_SYSTEM_RESERVE_GIB = 8
_MIN_BENCH_BUDGET_GIB = 4
_MAX_BENCH_PAGES = 500
_MAX_TOTAL_BYTES = 3 * 1024**3

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
_SNAPSHOT_PAGE_PREFIX = "ocr_reference/private-ocr-sources/pages/"


def _select_source() -> Path:
    if sys.platform != "darwin":
        raise RuntimeError("On non-macOS systems specify --source /path/to/book.cbz")
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", 'POSIX path of (choose file with prompt "Choose a manga CBZ or a Pudge environment ZIP")'],
        capture_output=True, text=True, check=True,
    )
    return Path(result.stdout.strip()).expanduser()


def _natural_key(name: str) -> list[int | str]:
    return [int(piece) if piece.isdigit() else piece.casefold() for piece in re.split(r"(\d+)", name)]


def _fixture_archive(source: Path, destination: Path, count: int | None) -> int:
    """Use identical pages for both modes; optional full-volume input."""
    with zipfile.ZipFile(source) as archive:
        all_names = archive.namelist()
        is_snapshot = any(name.startswith(_SNAPSHOT_PAGE_PREFIX) for name in all_names)
        names = [
            name for name in all_names
            if Path(name).suffix.lower() in _IMAGE_SUFFIXES
            and (name.startswith(_SNAPSHOT_PAGE_PREFIX) if is_snapshot else not name.startswith("__MACOSX/"))
            and not name.endswith("/")
        ]
        names.sort(key=_natural_key)
        if len(names) > _MAX_BENCH_PAGES and count is None:
            raise ValueError("Volume exceeds the safe 500-page benchmark limit")
        chosen = names[:count] if count is not None else names
        if len(chosen) < 6:
            raise ValueError("At least six image pages are required for a parallel OCR benchmark")
        if sum(archive.getinfo(name).file_size for name in chosen) > _MAX_TOTAL_BYTES:
            raise ValueError("Selected pages exceed the 3-GiB benchmark input limit")
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as out:
            for index, name in enumerate(chosen):
                data = archive.read(name)
                if len(data) > 35 * 1024 * 1024:
                    raise ValueError("Refusing a page larger than 35 MiB")
                out.writestr(f"page_{index:03d}{Path(name).suffix.lower()}", data)
    return len(chosen)


def _descendant_rss_kib(pid: int) -> tuple[int, int]:
    """Sum RSS of descendants, not unique physical memory (shared pages overlap)."""
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,rss="], capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return 0, 0
    processes: dict[int, tuple[int, int]] = {}
    for line in result.stdout.splitlines():
        try:
            process_id, parent_id, rss = map(int, line.split()[:3])
        except (ValueError, IndexError):
            continue
        processes[process_id] = (parent_id, rss)
    pending, visited, rss = [pid], set(), 0
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        if current in processes:
            rss += processes[current][1]
        pending.extend(child for child, (parent, _) in processes.items() if parent == current and child not in visited)
    return rss, len(visited & processes.keys())


def _run_mode(
    mode: str, module: str, manifest: Path, root: Path, python: Path,
    *, benchmark_budget_gib: int | None = None,
) -> dict[str, Any]:
    output, progress, stop = (root / f"{mode}.{suffix}" for suffix in ("jsonl", "progress", "stop"))
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    start = time.monotonic()
    command = [str(python), "-m", module, "--batch", str(manifest), str(output), str(progress), str(stop)]
    if benchmark_budget_gib is not None:
        if module != "pudge.manga_ocr_parallel" or not 4 <= benchmark_budget_gib <= 32:
            raise ValueError("Benchmark budget applies only to the bounded parallel coordinator")
        command.extend(["--benchmark-worker-budget-gib", str(benchmark_budget_gib)])
    # Results contain OCR text and remain inside this temporary directory.
    with (root / f"{mode}.stderr").open("wb") as stderr:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr, env=env,
            start_new_session=(os.name == "posix"),
        )
        peak_rss_kib, peak_processes = 0, 0
        min_available: int | None = None
        memory_probe_count = 0
        memory_guard_triggered = False
        try:
            while process.poll() is None:
                rss, count = _descendant_rss_kib(process.pid)
                peak_rss_kib, peak_processes = max(peak_rss_kib, rss), max(peak_processes, count)
                from pudge.manga_ocr_parallel import available_memory_bytes
                available = available_memory_bytes()
                if available is not None:
                    min_available = available if min_available is None else min(min_available, available)
                    memory_probe_count += 1
                    if benchmark_budget_gib is not None and available < _SYSTEM_RESERVE_GIB * _GIB:
                        # Guard applies only to the experimental two-worker run.
                        # Stop the entire session, not just its coordinator.
                        memory_guard_triggered = True
                        stop.write_text("stop\n", encoding="utf-8")
                        if process.poll() is None:
                            if os.name == "posix":
                                import signal
                                os.killpg(process.pid, signal.SIGTERM)
                            else:
                                process.terminate()
                        break
                time.sleep(0.3)
            if memory_guard_triggered:
                try:
                    code = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if process.poll() is None:
                        if os.name == "posix":
                            import signal
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                    code = process.wait(timeout=5)
                code = code or 75
            else:
                code = process.wait()
        except BaseException:
            stop.write_text("stop\n")
            if process.poll() is None:
                if os.name == "posix":
                    import signal
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if os.name == "posix":
                        import signal
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    process.wait(timeout=5)
            raise
    elapsed = time.monotonic() - start
    rows: dict[int, dict[str, Any]] = {}
    if output.exists():
        for line in output.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                page_id = int(row["page_index"])
                if page_id in rows:
                    raise ValueError("duplicate page")
                rows[page_id] = row
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                code = code or 1
    # No page text, image, hash of copyrighted page content or stderr is exported.
    return {
        "mode": mode,
        "exit_code": code,
        "seconds": round(elapsed, 2),
        "pages_returned": len(rows),
        "page_errors": sum(bool(row.get("error")) for row in rows.values()),
        "peak_summed_rss_gib": round(peak_rss_kib / (1024 ** 2), 2),
        "peak_process_count": peak_processes,
        "min_available_memory_gib": round(min_available / _GIB, 2) if min_available is not None else None,
        "memory_probe_samples": memory_probe_count,
        "memory_guard_triggered": memory_guard_triggered,
        "_rows": rows,
    }


def _comparison(a: dict[str, Any], b: dict[str, Any]) -> tuple[int, bool]:
    rows_a, rows_b = a["_rows"], b["_rows"]
    all_indexes = rows_a.keys() | rows_b.keys()
    differences = sum(rows_a.get(index) != rows_b.get(index) for index in all_indexes)
    return differences, rows_a.keys() == rows_b.keys()


def _safe_budget_gib(peak_rss_gib: float) -> int | None:
    """Use a conservative, rounded-up RSS estimate, never a smaller budget."""
    import math

    if not math.isfinite(peak_rss_gib) or peak_rss_gib <= 0:
        return None
    budget = max(_MIN_BENCH_BUDGET_GIB, math.ceil(peak_rss_gib * 1.5))
    return budget if budget <= 32 else None


def _eligible_for_trial(
    page_count: int, available_bytes: int | None, model_budget_gib: int | None,
) -> bool:
    if page_count < 6 or available_bytes is None or model_budget_gib is None:
        return False
    return available_bytes >= (_SYSTEM_RESERVE_GIB + 2 * model_budget_gib) * _GIB


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Existing manga CBZ or private Pudge environment ZIP")
    parser.add_argument("--pages", default="12", help="Number of pages (6–500), or 'all' for a full volume")
    parser.add_argument("--output-dir", type=Path, default=Path.home() / "Downloads" / "pudge-ocr-benchmark")
    args = parser.parse_args()
    if str(args.pages).lower() == "all":
        selected_pages = None
    else:
        try:
            selected_pages = int(args.pages)
        except ValueError:
            parser.error("--pages must be an integer from 6 to 500, or 'all'")
        if not 6 <= selected_pages <= _MAX_BENCH_PAGES:
            parser.error("--pages must be between 6 and 500")
    source = args.source or _select_source()
    if not source.is_file() or not zipfile.is_zipfile(source):
        parser.error("--source must be an existing CBZ/ZIP archive")
    output_dir = args.output_dir.expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    from pudge.manga_ocr_parallel import available_memory_bytes, worker_count

    available_before = available_memory_bytes()
    with tempfile.TemporaryDirectory(prefix="pudge-ocr-benchmark-") as temporary:
        root = Path(temporary)
        count = _fixture_archive(source, root / "pages.cbz", selected_pages)
        original_workers = worker_count(count, available_before)
        report: dict[str, Any] = {
            "page_count": count,
            "memory_probe_gib": round(available_before / _GIB, 2) if available_before is not None else None,
            "original_policy_workers": original_workers,
            "full_volume": selected_pages is None,
            "production_workers_at_probe": original_workers,
            "method": "same selected pages; no Vision seed regions; independent cold model runs",
            "memory_note": "summed RSS is not unique physical RAM; 50% headroom and 8 GiB system reserve are benchmark-only safeguards",
            "production_policy_changed": False,
        }
        manifest = root / "manifest.json"
        with zipfile.ZipFile(root / "pages.cbz") as archive:
            names = archive.namelist()
        manifest.write_text(json.dumps({
            "archive": str(root / "pages.cbz"),
            "pages": [{"page_index": index, "name": name, "regions": []} for index, name in enumerate(names)],
        }), encoding="utf-8")
        # Always measure the real one-worker model first, even when the old
        # conservative policy predicts that multiple models will not fit.
        if available_before is not None and available_before < 12 * _GIB:
            report["status"] = "insufficient_memory_even_for_single_worker_probe"
        else:
            baseline = _run_mode("sequential", "pudge.manga_ocr_worker", manifest, root, Path(sys.executable))
            report["sequential"] = {key: value for key, value in baseline.items() if key != "_rows"}
            budget = _safe_budget_gib(baseline["peak_summed_rss_gib"])
            report["measured_worker_budget_gib"] = budget
            available_after = available_memory_bytes()
            report["memory_after_baseline_gib"] = round(available_after / _GIB, 2) if available_after is not None else None
            if baseline["exit_code"] != 0 or baseline["page_errors"] or baseline["pages_returned"] != count:
                report["status"] = "single_worker_incomplete"
            elif baseline["memory_probe_samples"] == 0 or budget is None:
                report["status"] = "memory_measurement_unavailable"
            elif not _eligible_for_trial(count, available_after, budget):
                report["status"] = "two_worker_trial_skipped_for_memory_safety"
            else:
                report["trial_workers"] = 2
                adaptive = _run_mode(
                    "adaptive", "pudge.manga_ocr_parallel", manifest, root, Path(sys.executable),
                    benchmark_budget_gib=budget,
                )
                differences, indexes_equal = _comparison(baseline, adaptive)
                report["adaptive"] = {key: value for key, value in adaptive.items() if key != "_rows"}
                report["same_page_indexes"] = indexes_equal
                report["different_page_results"] = differences
                report["speedup"] = round(baseline["seconds"] / adaptive["seconds"], 3) if adaptive["seconds"] else None
                report["status"] = (
                    "complete" if not adaptive.get("memory_guard_triggered", False)
                    and baseline["exit_code"] == adaptive["exit_code"] == 0
                    and adaptive["pages_returned"] == count and not adaptive["page_errors"]
                    and indexes_equal and differences == 0 and adaptive["peak_process_count"] >= 3
                    else "incomplete_or_results_differ"
                )
    dest = output_dir / "summary.json"
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Summary: {dest}")
    return 0 if report["status"] in {
        "complete", "two_worker_trial_skipped_for_memory_safety",
        "insufficient_memory_even_for_single_worker_probe", "memory_measurement_unavailable",
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
