"""Bounded, memory-aware parallel page OCR for one manga volume.

The coordinator does not import MangaOCR or hold a model. Each child owns one
model and an independent subset of pages. Results are streamed into the normal
JSONL contract so the caller can salvage completed pages after an interruption.
"""

from __future__ import annotations

import json
import re
import signal
import subprocess
import sys
import threading
import tempfile
import time
from pathlib import Path
from typing import Any

_GIB = 1024 ** 3
# Two independent models matched the one-model result on 12 pages in the G5
# macOS trial. RSS omits some Metal memory; require an additional 4-GiB launch
# margin on top of the 8-GiB system reserve and 4 GiB per process.
_MODEL_BUDGET = 4 * _GIB
_SYSTEM_RESERVE = 8 * _GIB
_LAUNCH_MARGIN = 4 * _GIB
_MAX_WORKERS = 2


def available_memory_bytes() -> int | None:
    """Conservatively measure reclaimable memory; unknown means one worker."""
    if sys.platform == "darwin":
        try:
            output = subprocess.run(
                ["/usr/bin/vm_stat"], capture_output=True, text=True,
                check=True, timeout=3,
            ).stdout
            size = re.search(r"page size of (\d+) bytes", output)
            if size is None:
                return None
            pages = dict((name.lower(), int(count.replace(".", ""))) for name, count in re.findall(
                r"Pages (free|inactive|speculative):\s*([\d.]+)", output,
            ))
            if not pages:
                return None
            # Do not double-count purgeable pages, which may already be inactive.
            return int(size.group(1)) * sum(pages.values())
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
    if sys.platform.startswith("linux"):
        try:
            match = re.search(r"^MemAvailable:\s*(\d+)\s*kB", Path("/proc/meminfo").read_text(), re.M)
            if match is None:
                return None
            available = int(match.group(1)) * 1024
            # Containers may report the host's RAM in /proc/meminfo. Respect
            # the cgroup's actual remaining allowance when it is present.
            limit_path = Path("/sys/fs/cgroup/memory.max")
            usage_path = Path("/sys/fs/cgroup/memory.current")
            if limit_path.is_file() and usage_path.is_file():
                limit = limit_path.read_text().strip()
                if limit.isdecimal():
                    available = min(available, max(0, int(limit) - int(usage_path.read_text().strip())))
            return available
        except (OSError, ValueError):
            return None
    return None


def worker_count(
    page_count: int, free_bytes: int | None = None, *, model_budget_bytes: int = _MODEL_BUDGET,
) -> int:
    """Use at most two models while retaining a separate 8-GiB system reserve.

    The 4-GiB allowance is a conservative launch budget rather than measured
    total Metal memory. A further 4-GiB margin is mandatory for production,
    including when the explicit benchmark budget is supplied. Fail closed if
    available memory cannot be measured or fewer than six pages remain.
    """
    if page_count < 6:
        return 1
    if not 4 * _GIB <= model_budget_bytes <= 32 * _GIB:
        raise ValueError("OCR worker memory budget outside safe bounds")
    available = available_memory_bytes() if free_bytes is None else free_bytes
    if available is None or available < _SYSTEM_RESERVE + _LAUNCH_MARGIN + 2 * model_budget_bytes:
        return 1
    return max(1, min(_MAX_WORKERS, page_count // 3, int((available - _SYSTEM_RESERVE - _LAUNCH_MARGIN) // model_budget_bytes)))


def run(
    manifest_path: Path,
    output_path: Path,
    progress_path: Path,
    stop_path: Path,
    *,
    worker_module: str = "pudge.manga_ocr_worker",
    free_bytes: int | None = None,
    model_budget_bytes: int = _MODEL_BUDGET,
    max_workers: int = _MAX_WORKERS,
) -> int:
    """Run independent OCR workers and preserve each completed JSONL row.

    The calling process remains responsible for validating every row and for
    publishing only matching page generations. The coordinator never writes the
    database or modifies an OCR cache.
    """
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pages = manifest["pages"]
    if not isinstance(pages, list):
        raise ValueError("Invalid OCR batch page list")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"")
    if stop_path.exists():
        return 75
    count = min(max_workers, worker_count(len(pages), free_bytes, model_budget_bytes=model_budget_bytes))
    groups = [pages[i::count] for i in range(count)]
    processes: list[subprocess.Popen[bytes]] = []
    positions: list[int] = []
    pending: list[bytes] = []
    stop_signal = False
    completed_pages = 0
    last_page: int | None = None

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_signal
        stop_signal = True
        try:
            stop_path.write_text("stop\n", encoding="utf-8")
        except OSError:
            pass

    main_thread = threading.current_thread() is threading.main_thread()
    previous_handler = signal.signal(signal.SIGTERM, request_stop) if main_thread else None
    try:
        with tempfile.TemporaryDirectory(prefix="ocr-shards-", dir=output_path.parent) as temporary:
            root = Path(temporary)
            shard_results: list[Path] = []
            shard_errors: list[Path] = []
            with output_path.open("ab") as combined:
                for index, group in enumerate(groups):
                    if stop_path.exists() or stop_signal:
                        return 75
                    shard_manifest = root / f"{index}.manifest.json"
                    shard_output = root / f"{index}.results.jsonl"
                    shard_progress = root / f"{index}.progress.json"
                    shard_manifest.write_text(
                        json.dumps({"archive": manifest["archive"], "pages": group}, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    shard_output.write_bytes(b"")
                    shard_stderr = root / f"{index}.stderr"
                    with shard_stderr.open("wb") as stderr_handle:
                        child = subprocess.Popen(
                            [sys.executable, "-m", worker_module, "--batch", str(shard_manifest),
                             str(shard_output), str(shard_progress), str(stop_path)],
                            stdout=subprocess.DEVNULL, stderr=stderr_handle,
                        )
                    processes.append(child)
                    shard_errors.append(shard_stderr)
                    shard_results.append(shard_output)
                    positions.append(0)
                    pending.append(b"")
                    # Other applications can consume RAM while we are starting
                    # workers. Do not start additional models when the reserve
                    # has already been exhausted.
                    if index + 1 < len(groups):
                        remaining = available_memory_bytes() if free_bytes is None else free_bytes
                        if remaining is None or remaining < _SYSTEM_RESERVE + _LAUNCH_MARGIN + model_budget_bytes:
                            # Let the already-started worker process remaining
                            # groups instead of silently dropping any pages.
                            # This is handled by a separate fallback worker.
                            rest = [item for other in groups[index + 1:] for item in other]
                            if rest:
                                fallback = root / "fallback.manifest.json"
                                fallback_out = root / "fallback.results.jsonl"
                                fallback_progress = root / "fallback.progress.json"
                                fallback.write_text(
                                    json.dumps({"archive": manifest["archive"], "pages": rest}, ensure_ascii=False),
                                    encoding="utf-8",
                                )
                                # Delay launching the fallback until the first
                                # wave completes, without allocating another
                                # model while the machine is under pressure.
                                deferred = (fallback, fallback_out, fallback_progress)
                            else:
                                deferred = None
                            break
                else:
                    deferred = None

                def collect() -> None:
                    nonlocal completed_pages, last_page
                    for idx, shard in enumerate(shard_results):
                        with shard.open("rb") as source:
                            source.seek(positions[idx])
                            data = pending[idx] + source.read()
                            positions[idx] = source.tell()
                        parts = data.split(b"\n")
                        pending[idx] = parts.pop()
                        for line in parts:
                            if not line.strip():
                                continue
                            combined.write(line + b"\n")
                            completed_pages += 1
                            try:
                                last_page = int(json.loads(line)["page_index"])
                            except (ValueError, KeyError, TypeError):
                                pass
                    combined.flush()
                    progress_path.write_text(
                        json.dumps({"done": completed_pages, "total": len(pages), "page_index": last_page}),
                        encoding="utf-8",
                    )

                # Sustained memory pressure means results already streamed are
                # retained, while the remaining pages become retryable. A single
                # transient vm_stat reading does not cancel a long OCR job.
                low_memory_samples = 0
                memory_cancelled = False
                stop_deadline: float | None = None
                while any(proc.poll() is None for proc in processes):
                    collect()
                    if len(processes) > 1 and free_bytes is None and not stop_signal and not stop_path.exists():
                        current_memory = available_memory_bytes()
                        low_memory_samples = (
                            low_memory_samples + 1
                            if current_memory is not None and current_memory < _SYSTEM_RESERVE
                            else 0
                        )
                        if low_memory_samples >= 3:
                            memory_cancelled = True
                            request_stop(signal.SIGTERM, None)
                    if stop_signal or stop_path.exists():
                        if stop_deadline is None:
                            stop_deadline = time.monotonic() + 5.0
                        # The stop file is cooperative, but an in-flight model
                        # may be unresponsive. Escalate to kill after a bounded
                        # grace period so process-tree shutdown cannot hang.
                        for child in processes:
                            if child.poll() is None:
                                if time.monotonic() >= stop_deadline:
                                    child.kill()
                                else:
                                    child.terminate()
                    time.sleep(0.15)
                collect()
                codes = [proc.wait() for proc in processes]
                if deferred is not None and not stop_path.exists() and not stop_signal:
                    fallback, fallback_out, fallback_progress = deferred
                    # The machine may still be under memory pressure after the
                    # first wave. Fail safely and leave these pages retryable
                    # instead of forcing another large model into RAM.
                    free_now = available_memory_bytes() if free_bytes is None else free_bytes
                    if free_now is None or free_now < _SYSTEM_RESERVE + _LAUNCH_MARGIN + model_budget_bytes:
                        print("OCR deferred: insufficient free RAM for remaining pages", file=sys.stderr)
                        return 1
                    fallback_out.write_bytes(b"")
                    fallback_stderr = root / "fallback.stderr"
                    with fallback_stderr.open("wb") as stderr_handle:
                        child = subprocess.Popen(
                            [sys.executable, "-m", worker_module, "--batch", str(fallback),
                             str(fallback_out), str(fallback_progress), str(stop_path)],
                            stdout=subprocess.DEVNULL, stderr=stderr_handle,
                        )
                    processes.append(child)
                    shard_errors.append(fallback_stderr)
                    shard_results.append(fallback_out)
                    positions.append(0)
                    pending.append(b"")
                    while child.poll() is None:
                        collect()
                        time.sleep(0.15)
                    collect()
                    codes.append(child.wait())
                for child in processes:
                    if child.returncode not in (0, 75):
                        detail = shard_errors[processes.index(child)].read_bytes()[-2000:]
                        print(f"OCR shard exited {child.returncode}: {detail.decode(errors='replace')}", file=sys.stderr)
                if memory_cancelled:
                    print("OCR paused: system available memory below 8 GiB", file=sys.stderr)
                if stop_path.exists() or stop_signal or 75 in codes:
                    return 75
                return 1 if any(code != 0 for code in codes) else 0
    finally:
        for child in processes:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        if main_thread and previous_handler is not None:
            signal.signal(signal.SIGTERM, previous_handler)


def main() -> int:
    if sys.argv[1:2] != ["--batch"] or len(sys.argv) not in (6, 8):
        print("usage: python -m pudge.manga_ocr_parallel --batch MANIFEST RESULTS PROGRESS STOP "
              "[--benchmark-worker-budget-gib N]", file=sys.stderr)
        return 2
    try:
        if len(sys.argv) == 8:
            if sys.argv[6] != "--benchmark-worker-budget-gib":
                return 2
            # Benchmark-only override; it never raises the two-worker cap or
            # bypasses the production launch margin and memory guard.
            gib = int(sys.argv[7])
            if not 4 <= gib <= 32:
                return 2
            return run(*map(Path, sys.argv[2:6]), model_budget_bytes=gib * _GIB, max_workers=2)
        return run(*map(Path, sys.argv[2:6]))
    except Exception as exc:
        print(f"OCR coordinator failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
