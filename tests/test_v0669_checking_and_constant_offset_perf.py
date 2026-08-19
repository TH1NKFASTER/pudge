from pathlib import Path

from pudge import syncing


def _write_srt(path: Path, starts: list[float]) -> None:
    rows = []
    for index, start in enumerate(starts, start=1):
        end = start + 1.1 + (index % 3) * 0.17

        def stamp(value: float) -> str:
            milliseconds = int(round(value * 1000))
            hours, milliseconds = divmod(milliseconds, 3_600_000)
            minutes, milliseconds = divmod(milliseconds, 60_000)
            seconds, milliseconds = divmod(milliseconds, 1000)
            return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"

        rows.append(f"{index}\n{stamp(start)} --> {stamp(end)}\n字幕 {index}\n")
    path.write_text("\n".join(rows), encoding="utf-8")


def test_future_priority_subtitle_jobs_do_not_keep_checking_ui_active():
    html = Path("pudge/web/index.html").read_text(encoding="utf-8")
    assert "function nextPrioritySubtitleDelay()" in html
    assert "function nextForegroundSubtitleDelay()" in html
    assert "activeDownloads().length>0||dueForegroundSubtitleJobs().length>0" in html
    assert "ui.startupMaintenanceRunning||duePrioritySubtitleJobs().length" not in html
    assert "hasActiveForegroundWork()||foregroundSubtitleJobs().length||ui.emptyPolls<2" in html
    assert "if(dueForegroundSubtitleJobs().length)return document.hidden||!ui.windowActive?5000:1000" in html
    assert "function ensureForegroundPollScheduled()" in html


def test_constant_offset_search_is_bounded_and_keeps_small_global_shift(tmp_path: Path, monkeypatch):
    source = tmp_path / "source.srt"
    reference = tmp_path / "reference.srt"
    gaps = [1.7, 4.9, 2.6, 7.3, 1.1, 5.8, 3.4, 9.2, 2.1, 6.7]
    starts: list[float] = []
    current = 8.0
    for index in range(90):
        current += gaps[index % len(gaps)] + (index % 4) * 0.07
        starts.append(current)
    _write_srt(source, starts)
    _write_srt(reference, [value + 4.4 for value in starts])

    original = syncing._activity_correlation_for_shift
    calls = {"count": 0}

    def counted(*args, **kwargs):
        calls["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(syncing, "_activity_correlation_for_shift", counted)
    results = syncing.estimate_constant_subtitle_offsets(
        source, reference, max_offset_seconds=30.0, maximum_results=6
    )

    assert any(
        item.get("available") and abs(float(item.get("offset_seconds") or 0.0) - 4.4) <= 0.15
        for item in results
    )
    assert calls["count"] <= 140
    assert max(int(item.get("candidate_evaluations") or 0) for item in results) <= 140


def test_sync_cache_generation_changed_for_new_alignment_algorithm():
    source = Path("pudge/syncing.py").read_text(encoding="utf-8")
    assert "syncing-v0.3." in source
    assert "early-edit-speech-verification" in source
