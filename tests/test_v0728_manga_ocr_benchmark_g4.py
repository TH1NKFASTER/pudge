from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

from scripts import benchmark_ocr_parallel as benchmark


def test_benchmark_extracts_only_private_reference_pages_from_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "snapshot.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("project/pudge/web/logo.png", b"public-logo")
        for number in range(9):
            archive.writestr(
                f"ocr_reference/private-ocr-sources/pages/page_{number:03d}.png",
                f"private-page-{number}".encode(),
            )
    target = tmp_path / "selected.cbz"
    assert benchmark._fixture_archive(source, target, 6) == 6
    with zipfile.ZipFile(target) as archive:
        assert archive.namelist() == [f"page_{number:03d}.png" for number in range(6)]
        assert archive.read("page_000.png") == b"private-page-0"
    with zipfile.ZipFile(source) as archive:
        assert len(archive.namelist()) == 10  # The input was never modified.


def test_benchmark_preserves_natural_page_order(tmp_path: Path) -> None:
    source = tmp_path / "manga.cbz"
    with zipfile.ZipFile(source, "w") as archive:
        for number in (10, 2, 1, 11, 3, 4, 5):
            archive.writestr(f"{number}.png", str(number))
    target = tmp_path / "selected.cbz"
    assert benchmark._fixture_archive(source, target, 6) == 6
    with zipfile.ZipFile(target) as archive:
        assert [archive.read(name).decode() for name in archive.namelist()] == ["1", "2", "3", "4", "5", "10"]


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "private-reference.zip"
    with zipfile.ZipFile(source, "w") as archive:
        for number in range(6):
            archive.writestr(f"page_{number}.png", b"private-image-NEVER-EXPORT")
    return source


def _fake_mode(mode, module, manifest, root, python, *, benchmark_budget_gib=None):
    assert len(json.loads(manifest.read_text())["pages"]) == 6
    assert benchmark_budget_gib is None or (mode == "adaptive" and benchmark_budget_gib == 4)
    return {
        "mode": mode, "exit_code": 0, "seconds": 20.0 if mode == "sequential" else 10.0,
        "pages_returned": 6, "page_errors": 0,
        "peak_summed_rss_gib": 2.0 if mode == "sequential" else 4.0,
        "peak_process_count": 1 if mode == "sequential" else 3,
        "memory_probe_samples": 5,
        "min_available_memory_gib": 16.0,
        "_rows": {i: {"page_index": i, "text": "secret-NEVER-EXPORT"} for i in range(6)},
    }


def test_benchmark_measures_one_worker_even_when_original_policy_selects_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pudge import manga_ocr_parallel

    source = _source(tmp_path)
    monkeypatch.setattr(manga_ocr_parallel, "available_memory_bytes", lambda: 23 * 1024**3)
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args[0], kwargs))
        return _fake_mode(*args, **kwargs)

    monkeypatch.setattr(benchmark, "_run_mode", fake_run)
    monkeypatch.setattr(sys, "argv", ["benchmark", "--source", str(source), "--output-dir", str(tmp_path / "summary")])
    assert benchmark.main() == 0
    report_text = (tmp_path / "summary" / "summary.json").read_text()
    report = json.loads(report_text)
    assert report["original_policy_workers"] == 2
    assert report["measured_worker_budget_gib"] == 4
    assert calls == [("sequential", {}), ("adaptive", {"benchmark_budget_gib": 4})]
    assert report["speedup"] == 2.0 and report["different_page_results"] == 0
    assert report["status"] == "complete"
    assert report["production_policy_changed"] is False
    assert "NEVER-EXPORT" not in report_text
    assert "_rows" not in report_text
    assert str(source) not in report_text


def test_benchmark_still_measures_one_worker_when_two_cannot_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pudge import manga_ocr_parallel

    source = _source(tmp_path)
    monkeypatch.setattr(manga_ocr_parallel, "available_memory_bytes", lambda: 14 * 1024**3)
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args[0])
        return _fake_mode(*args, **kwargs)

    monkeypatch.setattr(benchmark, "_run_mode", fake_run)
    monkeypatch.setattr(sys, "argv", ["benchmark", "--source", str(source), "--output-dir", str(tmp_path / "summary")])
    assert benchmark.main() == 0
    report = json.loads((tmp_path / "summary" / "summary.json").read_text())
    assert report["status"] == "two_worker_trial_skipped_for_memory_safety"
    assert report["sequential"]["pages_returned"] == 6
    assert calls == ["sequential"]


def test_benchmark_skips_model_when_single_worker_probe_is_unsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pudge import manga_ocr_parallel

    source = _source(tmp_path)
    monkeypatch.setattr(manga_ocr_parallel, "available_memory_bytes", lambda: 1 * 1024**3)
    monkeypatch.setattr(benchmark, "_run_mode", lambda *_args, **_kwargs: pytest.fail("model must not start"))
    monkeypatch.setattr(sys, "argv", ["benchmark", "--source", str(source), "--output-dir", str(tmp_path / "summary")])
    assert benchmark.main() == 0
    assert json.loads((tmp_path / "summary" / "summary.json").read_text())["status"] == "insufficient_memory_even_for_single_worker_probe"


def test_benchmark_requires_observed_rss_before_trial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pudge import manga_ocr_parallel

    source = _source(tmp_path)
    monkeypatch.setattr(manga_ocr_parallel, "available_memory_bytes", lambda: 32 * 1024**3)
    def no_measurement(*args, **kwargs):
        result = _fake_mode(*args, **kwargs)
        result["peak_summed_rss_gib"] = 0
        return result
    monkeypatch.setattr(benchmark, "_run_mode", no_measurement)
    monkeypatch.setattr(sys, "argv", ["benchmark", "--source", str(source), "--output-dir", str(tmp_path / "summary")])
    assert benchmark.main() == 0
    report = json.loads((tmp_path / "summary" / "summary.json").read_text())
    assert report["status"] == "memory_measurement_unavailable"


def test_benchmark_worker_budget_is_bounded() -> None:
    gib = 1024**3
    from pudge import manga_ocr_parallel

    assert benchmark._safe_budget_gib(2.0) == 4
    assert benchmark._safe_budget_gib(6.1) == 10
    assert benchmark._safe_budget_gib(0) is None
    assert benchmark._safe_budget_gib(float("nan")) is None
    assert manga_ocr_parallel.worker_count(12, 23 * gib) == 2
    assert manga_ocr_parallel.worker_count(12, 23 * gib, model_budget_bytes=4 * gib) >= 2
    with pytest.raises(ValueError):
        manga_ocr_parallel.worker_count(12, 64 * gib, model_budget_bytes=1 * gib)


def test_benchmark_budget_runs_two_separate_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import subprocess
    from pudge import manga_ocr_parallel as parallel
    from tests.test_v0728_manga_ocr_parallel_g3 import fake_worker, make_manifest

    env = fake_worker(tmp_path)
    original = subprocess.Popen

    def popen(*args, **kwargs):
        kwargs["env"] = env
        return original(*args, **kwargs)

    monkeypatch.setattr(parallel.subprocess, "Popen", popen)
    gib = 1024**3
    assert parallel.worker_count(12, 23 * gib) == 2
    assert parallel.worker_count(12, 23 * gib, model_budget_bytes=4 * gib) == 2
    manifest, output, progress, stop = make_manifest(tmp_path)
    assert parallel.run(
        manifest, output, progress, stop,
        worker_module="mock_ocr_batch", free_bytes=23 * gib,
        model_budget_bytes=4 * gib, max_workers=2,
    ) == 0
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(rows) == 12
    assert len({r["regions"][0]["text"] for r in rows}) == 2


def test_invalid_benchmark_override_does_not_run_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    from pudge import manga_ocr_parallel as parallel

    def no_run(*args, **kwargs):
        pytest.fail("invalid override must be rejected")

    monkeypatch.setattr(parallel, "run", no_run)
    monkeypatch.setattr(sys, "argv", [
        "parallel", "--batch", "manifest", "output", "progress", "stop",
        "--benchmark-worker-budget-gib", "1",
    ])
    assert parallel.main() == 2


def test_benchmark_stops_experimental_process_when_system_reserve_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess
    import os
    from pudge import manga_ocr_parallel as parallel

    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    original_popen = subprocess.Popen
    processes = []

    def harmless_worker(*args, **kwargs):
        # Exercise real POSIX session cancellation without starting MangaOCR.
        proc = original_popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=kwargs.get("stdin"), stdout=kwargs.get("stdout"),
            stderr=kwargs.get("stderr"), start_new_session=kwargs.get("start_new_session", False),
        )
        processes.append(proc)
        return proc

    monkeypatch.setattr(benchmark.subprocess, "Popen", harmless_worker)
    monkeypatch.setattr(benchmark, "_descendant_rss_kib", lambda _pid: (64000, 1))
    monkeypatch.setattr(parallel, "available_memory_bytes", lambda: 5 * 1024**3)
    try:
        output = benchmark._run_mode(
            "adaptive", "pudge.manga_ocr_parallel", manifest, tmp_path, Path(sys.executable),
            benchmark_budget_gib=4,
        )
        assert output["memory_guard_triggered"] is True
        assert output["exit_code"] != 0
        assert (tmp_path / "adaptive.stop").exists()
        assert processes and processes[0].poll() is not None
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)
