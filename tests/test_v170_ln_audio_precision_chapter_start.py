from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pudge.audiobooks as audiobooks
import pudge.reading_audio_alignment as reading_alignment
from pudge.audiobooks import AudiobookService


def test_degraded_chapter_marker_requests_precision_stt() -> None:
    assert AudiobookService._alignment_chapter_needs_precision(
        {
            "title": "第一幕",
            "leading_prefix_debug": {
                "attempted": True,
                "recovered": True,
                "degraded": True,
                "reason": "no_prefix_seed",
            },
        }
    )
    assert not AudiobookService._alignment_chapter_needs_precision(
        {
            "title": "第一幕",
            "leading_prefix_debug": {
                "attempted": True,
                "recovered": True,
                "degraded": False,
                "reason": "local_fuzzy_prefix",
            },
        }
    )
    assert not AudiobookService._alignment_chapter_needs_precision(
        {"title": "Chapter 1", "leading_prefix_debug": {"degraded": True}}
    )


def test_precision_chapter_start_stt_is_cached_and_shifted(tmp_path: Path, monkeypatch) -> None:
    service = object.__new__(AudiobookService)
    service.cache_dir = tmp_path / "cache"
    service.stt_model = "mlx-community/whisper-tiny"
    service.python = "/usr/bin/python3"

    extract_calls: list[tuple[float, float]] = []

    def fake_extract(_book_id: int, destination: Path, *, start: float, duration: float):
        extract_calls.append((start, duration))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"audio")
        return 12.5, 40.0

    monkeypatch.setattr(service, "_extract_global_audio_window", fake_extract)
    monkeypatch.setattr(service, "_resolved_ffmpeg", lambda: "/usr/bin/ffmpeg")

    run_calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        run_calls.append(list(command))
        result_path = Path(command[5])
        result_path.write_text(
            json.dumps(
                {
                    "segments": [
                        {
                            "start": 1.0,
                            "end": 2.0,
                            "text": "小高い丘が続く",
                            "words": [
                                {"start": 1.0, "end": 1.4, "word": "小高い丘"},
                                {"start": 1.4, "end": 2.0, "word": "が続く"},
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(audiobooks.subprocess, "run", fake_run)

    output = tmp_path / "alignment" / "abc.json"
    output.parent.mkdir(parents=True)
    segments, metadata = service._precision_chapter_start_segments(
        283,
        output,
        chapter_index=0,
        reference_time=35.84,
        total_duration=10_000.0,
    )

    assert metadata["model"] == "mlx-community/whisper-small-mlx"
    assert metadata["cached"] is False
    assert len(run_calls) == 1
    assert segments[0]["start"] == 13.5
    assert segments[0]["end"] == 14.5
    assert segments[0]["words"][0]["start"] == 13.5
    assert extract_calls

    cached_segments, cached_metadata = service._precision_chapter_start_segments(
        283,
        output,
        chapter_index=0,
        reference_time=35.84,
        total_duration=10_000.0,
    )
    assert cached_metadata["cached"] is True
    assert cached_segments == segments
    assert len(run_calls) == 1


def test_alignment_uses_precision_segments_only_for_chapter_start(monkeypatch) -> None:
    chapter_text = (
        "小高い丘が延々と続く。岩ばかりが目立ち、草も木も少ない。"
        "風が乾いた地面をなで、遠くには低い山並みが見えている。"
        "旅人は静かな道を歩きながら、空の色を眺めていた。"
    ) * 4
    chapters = [{"chapter_index": 0, "title": "第一幕", "text": chapter_text}]
    full_segments = [
        {"start": 30.0, "end": 65.0, "text": chapter_text},
    ]
    precision_segments = [
        {"start": 34.0, "end": 35.0, "text": "第一幕"},
        {"start": 36.0, "end": 42.0, "text": chapter_text[:80]},
    ]
    seen: list[list[dict]] = []
    original = reading_alignment._recover_leading_prefix_clock

    def capture(chapter_text_arg, segments_arg, clock, **kwargs):
        seen.append(list(segments_arg))
        return original(chapter_text_arg, segments_arg, clock, **kwargs)

    monkeypatch.setattr(reading_alignment, "_recover_leading_prefix_clock", capture)
    result = reading_alignment.align_light_novel_to_transcript(
        chapters,
        full_segments,
        duration=90.0,
        model="test-model",
        chapter_start_segments={0: precision_segments},
    )

    assert result["matched_anchor_count"] >= 8
    assert seen
    assert seen[0] == precision_segments
    debug = result["chapters"][0]["leading_prefix_debug"]
    assert debug["precision_chapter_start_stt"] is True
    assert debug["precision_segment_count"] == len(precision_segments)


def test_precision_window_recovers_real_wolf_spice_shape_after_tiny_prefix_failure() -> None:
    story = (
        "小高い丘が延々と続く。岩ばかりが目立ち草も木も少ない。"
        "道は丘と丘の間を縫って作られ旅人は静かに荷馬車を進めた。"
    ) * 10
    broken_clock = [
        {"offset": 0, "time": 35.84},
        {"offset": 411, "time": 36.24},
        {"offset": 421, "time": 40.90},
        {"offset": 759, "time": 111.78},
    ]
    tiny_segments = [
        {"start": 35.84, "end": 36.24, "text": "第一話"},
        {"start": 40.90, "end": 44.20, "text": "遠い町まで旅を続けていた"},
        {"start": 44.40, "end": 48.00, "text": "そのころ商人は荷馬車を止めた"},
    ]
    _, _, tiny_debug = reading_alignment._recover_leading_prefix_clock(
        story,
        tiny_segments,
        broken_clock,
        chapter_title="第一幕",
        chapter_index=0,
        force_verify=True,
        search_start=20.0,
        search_end=100.0,
    )
    assert tiny_debug["reason"] in {"no_prefix_seed", "structural_bias_rebase"}

    precision_segments = [
        {"start": 35.84, "end": 36.70, "text": "第一幕"},
        {"start": 40.90, "end": 44.20, "text": story[:30]},
        {"start": 44.40, "end": 48.00, "text": story[30:64]},
        {"start": 48.20, "end": 52.00, "text": story[64:100]},
    ]
    repaired, story_start, debug = reading_alignment._recover_leading_prefix_clock(
        story,
        precision_segments,
        broken_clock,
        chapter_title="第一幕",
        chapter_index=0,
        force_verify=True,
        search_start=20.0,
        search_end=100.0,
    )
    assert debug["chapter_marker"] is True
    assert debug["recovered"] is True
    assert debug.get("degraded") is not True
    assert story_start is not None and 40.5 <= story_start <= 41.2
    assert repaired[0]["offset"] == 0
    assert repaired[0]["time"] == story_start
    first_progress = next(row for row in repaired if float(row["offset"]) >= 20.0)
    assert 40.9 <= float(first_progress["time"]) <= 48.5
