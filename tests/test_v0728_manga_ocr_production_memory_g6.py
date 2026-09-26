"""Production two-worker admission and memory-pressure safeguards (G6)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pudge import manga_ocr_parallel as parallel
from scripts import benchmark_ocr_parallel as benchmark
from tests.test_v0728_manga_ocr_parallel_g3 import fake_worker, make_manifest

GIB = 1024**3


def test_production_two_worker_thresholds_and_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    assert parallel._MODEL_BUDGET == 4 * GIB
    assert parallel._MAX_WORKERS == 2
    # None requests a fresh memory probe; simulate an unavailable measurement.
    monkeypatch.setattr(parallel, "available_memory_bytes", lambda: None)
    for free in (None, 0, 12 * GIB, 19 * GIB):
        assert parallel.worker_count(50, free) == 1
    assert parallel.worker_count(5, 100 * GIB) == 1
    assert parallel.worker_count(6, 20 * GIB) == 2
    assert parallel.worker_count(50, 23 * GIB) == 2
    assert parallel.worker_count(50, 200 * GIB) == 2
    # Even a benchmark budget override cannot bypass the safety margin.
    assert parallel.worker_count(50, 15 * GIB, model_budget_bytes=4 * GIB) == 1
    assert parallel.worker_count(50, 31 * GIB, model_budget_bytes=10 * GIB) == 1


def _fake_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, delay: str = "0.12") -> None:
    env = fake_worker(tmp_path)
    env["MOCK_OCR_DELAY"] = delay
    original = subprocess.Popen

    def launch(*args, **kwargs):
        kwargs["env"] = env
        return original(*args, **kwargs)

    monkeypatch.setattr(parallel.subprocess, "Popen", launch)


def test_memory_pressure_stops_children_and_preserves_retryable_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_env(tmp_path, monkeypatch, delay="0.14")
    # Preflight and worker-2 admission are healthy. During the running job the
    # available memory stays below the 8-GiB reserve for three samples.
    probe = iter([24 * GIB, 24 * GIB, 7 * GIB, 7 * GIB, 7 * GIB])
    monkeypatch.setattr(parallel, "available_memory_bytes", lambda: next(probe, 7 * GIB))
    manifest, output, progress, stop = make_manifest(tmp_path, 24)
    assert parallel.run(manifest, output, progress, stop, worker_module="mock_ocr_batch") == 75
    assert stop.is_file()
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    ids = [int(row["page_index"]) for row in rows]
    assert len(ids) == len(set(ids))
    assert len(ids) < 24
    assert not list(tmp_path.glob("ocr-shards-*"))
    # Original caller retries exactly the incomplete pages with a fresh stop.
    remaining = sorted(set(range(24)) - set(ids))
    stop.unlink()
    manifest.write_text(json.dumps({"archive": "unused", "pages": [{"page_index": i} for i in remaining]}))
    assert parallel.run(manifest, output, progress, stop, worker_module="mock_ocr_batch", free_bytes=24 * GIB) == 0
    assert [json.loads(line)["page_index"] for line in output.read_text().splitlines()]


def test_one_transient_low_memory_sample_does_not_interrupt_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_env(tmp_path, monkeypatch, delay="0.1")
    probe = iter([24 * GIB, 24 * GIB, 7 * GIB, 24 * GIB])
    monkeypatch.setattr(parallel, "available_memory_bytes", lambda: next(probe, 24 * GIB))
    manifest, output, progress, stop = make_manifest(tmp_path, 12)
    assert parallel.run(manifest, output, progress, stop, worker_module="mock_ocr_batch") == 0
    assert not stop.exists()
    assert len(output.read_text().splitlines()) == 12


def test_full_volume_benchmark_includes_all_pages_and_enforces_limits(tmp_path: Path) -> None:
    import zipfile

    source = tmp_path / "source.cbz"
    with zipfile.ZipFile(source, "w") as archive:
        for index in range(45):
            archive.writestr(f"{index:03d}.png", str(index).encode())
    full = tmp_path / "all.cbz"
    assert benchmark._fixture_archive(source, full, None) == 45
    with zipfile.ZipFile(full) as archive:
        assert len(archive.namelist()) == 45
        assert archive.read("page_044.png") == b"44"
    assert benchmark._fixture_archive(source, tmp_path / "sample.cbz", 12) == 12
    assert len(zipfile.ZipFile(tmp_path / "sample.cbz").namelist()) == 12
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(benchmark, "_MAX_BENCH_PAGES", 40)
        with pytest.raises(ValueError, match="500-page"):
            benchmark._fixture_archive(source, tmp_path / "too-many.cbz", None)
        monkeypatch.setattr(benchmark, "_MAX_BENCH_PAGES", 500)
        with pytest.raises(ValueError, match="3-GiB"):
            monkeypatch.setattr(benchmark, "_MAX_TOTAL_BYTES", 1)
            benchmark._fixture_archive(source, tmp_path / "too-big.cbz", None)
    finally:
        monkeypatch.undo()


def test_full_volume_cli_uses_all_pages_without_exporting_ocr_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    import zipfile

    source = tmp_path / "synthetic.cbz"
    with zipfile.ZipFile(source, "w") as archive:
        for index in range(45):
            archive.writestr(f"page_{index:03d}.png", b"synthetic-image")
    monkeypatch.setattr(parallel, "available_memory_bytes", lambda: 24 * GIB)
    calls: list[tuple[str, int]] = []

    def mock_ocr(mode, _module, manifest, _root, _python, *, benchmark_budget_gib=None):
        pages = json.loads(manifest.read_text())["pages"]
        calls.append((mode, len(pages)))
        assert benchmark_budget_gib in (None, 4)
        return {
            "mode": mode, "exit_code": 0, "seconds": 30.0 if mode == "sequential" else 20.0,
            "pages_returned": 45, "page_errors": 0,
            "peak_summed_rss_gib": 1.0 if mode == "sequential" else 2.0,
            "peak_process_count": 1 if mode == "sequential" else 3,
            "min_available_memory_gib": 16.0,
            "memory_probe_samples": 4,
            "memory_guard_triggered": False,
            "_rows": {i: {"page_index": i, "text": "private-NEVER-EXPORT"} for i in range(45)},
        }

    monkeypatch.setattr(benchmark, "_run_mode", mock_ocr)
    output_dir = tmp_path / "report"
    monkeypatch.setattr(sys, "argv", ["benchmark", "--source", str(source), "--pages", "all", "--output-dir", str(output_dir)])
    assert benchmark.main() == 0
    report_text = (output_dir / "summary.json").read_text()
    report = json.loads(report_text)
    assert report["full_volume"] is True and report["page_count"] == 45
    assert report["status"] == "complete"
    assert calls == [("sequential", 45), ("adaptive", 45)]
    assert "private-NEVER-EXPORT" not in report_text
    assert str(source) not in report_text
