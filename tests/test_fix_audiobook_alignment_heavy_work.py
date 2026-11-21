"""LN↔audiobook alignment runs in the heavy-work slot; its chapter-start STT can be stopped (behaviour).

The alignment child process and one MLX whisper process per uncertain chapter start
ran outside the heavy-work lock (next to another book's transcription: two MLX
processes with 6 GB each, also during playback and on battery). The chapter-start
process was started with ``subprocess.run``: not registered, not marked as
background work, and neither cancel nor quitting could stop it.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

import pudge.audiobooks as audiobooks_module
from pudge.work_scheduler import WorkPriority, WorkScheduler
from test_v224_alignment_vn_generation import _paired_audio, _wait_until


def _scheduler(tmp_path: Path) -> WorkScheduler:
    scheduler = WorkScheduler(tmp_path / "scheduler")
    scheduler.background_allowed = lambda **_kw: True  # type: ignore[method-assign]
    return scheduler


def _quiet_alignment(audio, monkeypatch, fake_align) -> None:
    monkeypatch.setattr(
        audio,
        "_load_transcript",
        lambda _audio_id: {"segments": [{"start": 0.0, "end": 2.0, "text": "狼と商人"}]},
    )
    monkeypatch.setattr(audio, "_load_or_analyze_activity", lambda *_args, **_kwargs: ([], "test"))
    monkeypatch.setattr(audio, "_cached_chapter_start_reading_hints", lambda _ln_id: {})
    monkeypatch.setattr(audio, "_alignment_chapter_needs_precision", lambda _row: False)
    monkeypatch.setattr(audio, "_transcript_fingerprint", lambda _audio_id: "transcript")
    monkeypatch.setattr(
        audiobooks_module, "build_alignment_report", lambda _alignment, _chapters: {"summary": {}}
    )
    monkeypatch.setattr(audio, "_align_reading_audio", fake_align)


def test_alignment_waits_for_the_heavy_work_slot_and_holds_it(tmp_path: Path, monkeypatch) -> None:
    audio, ln_id, audio_id = _paired_audio(tmp_path)
    scheduler = _scheduler(tmp_path)
    audio.work_scheduler = scheduler
    seen: list[object] = []

    def fake_align(*_args, **_kwargs):
        # While aligning, no other heavy work can start.
        seen.append(
            scheduler.acquire_heavy(
                "probe", blocking=False, foreground_sensitive=False, priority=WorkPriority.USER
            )
        )
        return {"schema": "reading-audio-v3", "chapters": [], "confidence": 0.9, "anchor_count": 0}

    _quiet_alignment(audio, monkeypatch, fake_align)
    other = scheduler.acquire_heavy("audiobook-stt", blocking=False, foreground_sensitive=False)
    assert other is not None
    audio.prepare_alignment(ln_id)
    _wait_until(lambda: audio.alignment_status(ln_id).get("wait_reason") == "heavy_work")
    time.sleep(0.3)
    assert seen == []
    assert audio.alignment_status(ln_id)["status"] == "queued"

    other.release()
    _wait_until(lambda: audio._alignment_path(ln_id, audio_id).is_file(), timeout=60.0)
    _wait_until(lambda: audio.alignment_status(ln_id).get("ready") is True)
    assert seen == [None]
    # Released afterwards.
    lease = scheduler.acquire_heavy("next", blocking=False, foreground_sensitive=False)
    assert lease is not None
    lease.release()


def test_alignment_step_gives_the_slot_to_user_work(tmp_path: Path) -> None:
    audio, ln_id, audio_id = _paired_audio(tmp_path)
    scheduler = _scheduler(tmp_path)
    audio.work_scheduler = scheduler
    cancel = threading.Event()
    with audio._lock:
        audio._alignment_jobs[ln_id] = {"status": "aligning", "audiobook_id": audio_id, "generation": 1}
        audio._alignment_cancel_events[ln_id] = cancel
    lease = audio._alignment_heavy_lease(ln_id, audio_id, 1, cancel, WorkPriority.BACKGROUND)
    assert lease is not None
    # Nothing more urgent: the same lease is kept.
    assert audio._alignment_heavy_lease(ln_id, audio_id, 1, cancel, WorkPriority.BACKGROUND, lease) is lease

    token = scheduler.begin_priority_request(WorkPriority.USER, name="page-ocr")
    order: list[str] = []

    def user_work() -> None:
        user = scheduler.acquire_heavy(
            "page-ocr", blocking=True, foreground_sensitive=False, priority=WorkPriority.USER
        )
        order.append("user")
        user.release()
        scheduler.end_priority_request(token)

    thread = threading.Thread(target=user_work, daemon=True)
    thread.start()
    again = audio._alignment_heavy_lease(ln_id, audio_id, 1, cancel, WorkPriority.BACKGROUND, lease)
    order.append("alignment")
    thread.join(60)
    assert order == ["user", "alignment"]
    assert audio.alignment_status(ln_id)["status"] == "aligning"
    again.release()


def _sleeper(tmp_path: Path) -> Path:
    marker = tmp_path / "child-env.txt"
    script = tmp_path / "bin" / "fake-python"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        f'#!/bin/sh\nprintf "%s" "$PUDGE_BACKGROUND_PRIORITY" > "{marker}"\nexec sleep 600\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_chapter_start_stt_is_registered_background_and_cancellable(tmp_path: Path, monkeypatch) -> None:
    audio, ln_id, audio_id = _paired_audio(tmp_path)
    audio.python = str(_sleeper(tmp_path))
    monkeypatch.setattr(audio, "_resolved_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        audio,
        "_extract_global_audio_window",
        lambda _id, destination, *, start, duration: (destination.write_bytes(b"flac"), (start, duration))[1],
    )
    cancel = threading.Event()
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["result"] = audio._precision_chapter_start_segments(
                audio_id,
                tmp_path / "alignment" / "a.json",
                chapter_index=0,
                reference_time=30.0,
                total_duration=120.0,
                ln_book_id=ln_id,
                cancel_event=cancel,
                background=True,
            )
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    _wait_until(lambda: audio._alignment_processes.get(ln_id) is not None, timeout=60.0)
    process = audio._alignment_processes[ln_id]
    _wait_until(lambda: (tmp_path / "child-env.txt").is_file(), timeout=60.0)
    assert (tmp_path / "child-env.txt").read_text() == "1"

    started = time.monotonic()
    cancel.set()
    thread.join(120)
    assert time.monotonic() - started < 60  # stopped, not waited for (the child sleeps 10 min)
    assert isinstance(outcome.get("error"), InterruptedError)
    assert process.poll() is not None
    assert ln_id not in audio._alignment_processes


def test_quitting_stops_a_running_chapter_start_stt(tmp_path: Path, monkeypatch) -> None:
    audio, ln_id, audio_id = _paired_audio(tmp_path)
    audio.python = str(_sleeper(tmp_path))
    monkeypatch.setattr(audio, "_resolved_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        audio,
        "_extract_global_audio_window",
        lambda _id, destination, *, start, duration: (destination.write_bytes(b"flac"), (start, duration))[1],
    )
    errors: list[BaseException] = []

    def run() -> None:
        try:
            audio._precision_chapter_start_segments(
                audio_id,
                tmp_path / "alignment" / "a.json",
                chapter_index=0,
                reference_time=30.0,
                total_duration=120.0,
                ln_book_id=ln_id,
                cancel_event=threading.Event(),
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    _wait_until(lambda: audio._alignment_processes.get(ln_id) is not None, timeout=60.0)
    process = audio._alignment_processes[ln_id]
    audio.close()
    thread.join(120)
    assert process.poll() is not None
    assert errors and isinstance(errors[0], InterruptedError)


@pytest.mark.parametrize("returncode", [0, 3])
def test_chapter_start_stt_result_and_failure_are_read_from_the_child(
    tmp_path: Path, monkeypatch, returncode
) -> None:
    audio, ln_id, audio_id = _paired_audio(tmp_path)
    script = tmp_path / "bin" / "fake-python"
    script.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"segments": [{"start": 1.0, "end": 2.0, "text": "狼"}]})
    script.write_text(
        "#!/bin/sh\n"
        + (f"printf '%s' '{payload}' > \"$5\"\n" if returncode == 0 else 'echo "MLX failed" >&2\n')
        + f"exit {returncode}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    audio.python = str(script)
    monkeypatch.setattr(audio, "_resolved_ffmpeg", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        audio,
        "_extract_global_audio_window",
        lambda _id, destination, *, start, duration: (destination.write_bytes(b"flac"), (start, duration))[1],
    )
    rows, metadata = audio._precision_chapter_start_segments(
        audio_id,
        tmp_path / "alignment" / "a.json",
        chapter_index=0,
        reference_time=30.0,
        total_duration=120.0,
        ln_book_id=ln_id,
        cancel_event=threading.Event(),
    )
    if returncode == 0:
        assert [row["text"] for row in rows] == ["狼"]
    else:
        assert rows == [] and "MLX failed" in metadata["error"]
    assert ln_id not in audio._alignment_processes
