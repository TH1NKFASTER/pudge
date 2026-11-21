"""Subtitle pipeline review (rv3): robustness of STT, caches and format parsing."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from pudge import pipeline_cache, subtitle_formats
from pudge.models import SubtitleCandidate
from pudge.subtitles import discovery, stt, stt_worker, video_segments


def _fake_stt(tmp_path: Path, monkeypatch, worker_output: str) -> list[str]:
    calls: list[str] = []

    def run(command, **_kwargs):
        if "pudge.subtitles.stt_worker" in command:
            calls.append("worker")
            Path(command[-2]).write_text(worker_output, encoding="utf-8")
        else:
            calls.append("ffmpeg")
            Path(command[-1]).write_bytes(b"flac")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(stt, "_select_japanese_audio_stream", lambda *_a: (None, {}))
    monkeypatch.setattr(stt, "_worker_availability", lambda _python: {"available": True})
    monkeypatch.setattr(stt.subprocess, "run", run)
    return calls


def _segments(count: int) -> str:
    return json.dumps(
        {"segments": [{"start": i * 2.0, "end": i * 2.0 + 1.5, "text": f"台詞{i}"} for i in range(count)]}
    )


def test_an_unreadable_stt_result_keeps_the_previous_transcript(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video" * 100)
    _fake_stt(tmp_path, monkeypatch, _segments(10))
    reference, info = stt.prepare_japanese_stt_reference(
        video, tmp_path / "cache", ffmpeg_path="ffmpeg", model="tiny", timeout_seconds=60
    )
    assert reference is not None
    transcript = Path(info["transcript_path"])
    good = transcript.read_text(encoding="utf-8")

    for broken in ('{"segments": [', _segments(3)):
        _fake_stt(tmp_path, monkeypatch, broken)
        again, info = stt.prepare_japanese_stt_reference(
            video, tmp_path / "cache", ffmpeg_path="ffmpeg", model="tiny", timeout_seconds=60, force=True
        )
        assert again is None and info["available"] is False
        # The cached reference still points at a transcript that belongs to it.
        assert transcript.read_text(encoding="utf-8") == good
        assert sorted(path.name for path in transcript.parent.iterdir()) == [
            "metadata.json",
            "reference.ja.srt",
            "transcription.json",
        ]


def test_an_infinite_stt_segment_time_is_skipped(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video" * 100)
    payload = json.loads(_segments(10))
    payload["segments"].append({"start": "inf", "end": "inf", "text": "壊れた"})
    _fake_stt(tmp_path, monkeypatch, json.dumps(payload))
    reference, info = stt.prepare_japanese_stt_reference(
        video, tmp_path / "cache", ffmpeg_path="ffmpeg", model="tiny", timeout_seconds=60
    )
    assert reference is not None and info["segments"] == 10
    assert "壊れた" not in reference.read_text(encoding="utf-8")


def test_the_stt_audio_is_removed_when_it_cannot_be_hashed(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video" * 100)
    calls = _fake_stt(tmp_path, monkeypatch, _segments(10))

    def fail(_path: Path) -> str:
        raise OSError("read error")

    monkeypatch.setattr(stt, "_file_sha256", fail)
    reference, info = stt.prepare_japanese_stt_reference(
        video, tmp_path / "cache", ffmpeg_path="ffmpeg", model="tiny", timeout_seconds=60
    )
    assert reference is None and info["reason"] == "audio_extract_failed"
    assert calls == ["ffmpeg"]
    assert not list((tmp_path / "cache").rglob("*.flac"))


def test_the_serving_stt_worker_survives_a_failed_write(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(stt_worker, "_load_transcribe", lambda: lambda *_a, **_k: {"segments": []})
    monkeypatch.setattr(stt_worker, "_configure_mlx_memory", lambda: None)
    answers: list[dict] = []

    class Protocol:
        def write(self, text: str) -> None:
            answers.append(json.loads(text))

        def flush(self) -> None:
            pass

    real_fdopen = stt_worker.os.fdopen
    monkeypatch.setattr(
        stt_worker.os, "fdopen", lambda fd, *a, **k: Protocol() if fd == -99 else real_fdopen(fd, *a, **k)
    )
    monkeypatch.setattr(stt_worker.os, "dup", lambda _fd: -99)
    monkeypatch.setattr(stt_worker.os, "dup2", lambda *_a: None)
    writes = {"count": 0}
    real_write = stt_worker._write_json_atomic

    def flaky_write(path: Path, payload: object) -> None:
        writes["count"] += 1
        if writes["count"] == 1:
            raise OSError("No space left on device")
        real_write(path, payload)

    monkeypatch.setattr(stt_worker, "_write_json_atomic", flaky_write)
    requests = [
        json.dumps({"id": 1, "audio": str(tmp_path / "a.flac"), "output": str(tmp_path / "a.json")}),
        json.dumps({"id": 2, "audio": str(tmp_path / "b.flac"), "output": str(tmp_path / "b.json")}),
    ]
    monkeypatch.setattr(stt_worker.sys, "stdin", iter(line + "\n" for line in requests))
    stdout = stt_worker.sys.stdout
    try:
        assert stt_worker._serve("tiny") == 0
    finally:
        stt_worker.sys.stdout = stdout
    assert answers[0] == {"ready": True}
    assert answers[1]["id"] == 1 and answers[1]["ok"] is False
    assert answers[2]["id"] == 2 and answers[2]["ok"] is True, answers[2]
    assert (tmp_path / "b.json").is_file()


def test_a_vanished_ass_candidate_is_kept_without_a_fingerprint(tmp_path: Path) -> None:
    present = tmp_path / "a.srt"
    present.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n", encoding="utf-8")
    gone = tmp_path / "gone.ass"
    candidates = [
        SubtitleCandidate(path=present, source="local", name=present.name, score=2.0),
        SubtitleCandidate(path=gone, source="local", name=gone.name, score=1.0),
    ]
    assert discovery.content_fingerprint(candidates[1], tmp_path / "cache", ffmpeg_path="ffmpeg") is None
    kept, removed = discovery.deduplicate_candidates(candidates, tmp_path / "cache", ffmpeg_path="ffmpeg")
    assert [candidate.path for candidate in kept] == [present, gone] and removed == 0


def test_chapter_probe_tolerates_odd_ffprobe_output(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "ffprobe"
    binary.write_text("")
    answers = iter(
        [
            '{"chapters": null}',
            "[]",
            json.dumps(
                {
                    "chapters": [
                        {"start_time": "nan", "end_time": "90.0"},
                        {"start_time": "90.0", "end_time": "inf"},
                    ]
                }
            ),
        ]
    )
    monkeypatch.setattr(
        video_segments.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=next(answers), stderr=""),
    )
    assert video_segments.probe_container_edit_points(tmp_path / "v.mkv", str(binary)) == []
    assert video_segments.probe_container_edit_points(tmp_path / "v.mkv", str(binary)) == []
    points = video_segments.probe_container_edit_points(tmp_path / "v.mkv", str(binary))
    assert [point["time"] for point in points] == [90.0]


def test_a_damaged_pipeline_manifest_is_a_cache_miss(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    manifest = tmp_path / "manifest.json"
    monkeypatch.setattr(pipeline_cache, "_manifest_path", lambda *_a: manifest)
    config = SimpleNamespace(matching=SimpleNamespace(ocr_image_subtitles=False))
    payload = {"schema": pipeline_cache._CACHE_SCHEMA, "created_at": 1.0, "subtitle": "", "subtitle_id": "x"}
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    assert pipeline_cache.load_final_pipeline_result(video, config) is None
    payload["subtitle_id"] = 3
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    assert pipeline_cache.load_final_pipeline_result(video, config)["subtitle_id"] == 3


def test_python_ass_fallback_parses_the_script_once(tmp_path: Path, monkeypatch) -> None:
    subtitle = tmp_path / "episode.ass"
    subtitle.write_text(
        "[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,こんにちは\n"
        "Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,さようなら\n",
        encoding="utf-8",
    )
    parsed: list[Path] = []
    real = subtitle_formats._ass_geometry_cues

    def counting(source: Path, *text):
        parsed.append(source)
        return real(source, *text)

    monkeypatch.setattr(subtitle_formats, "_ass_geometry_cues", counting)
    monkeypatch.setattr(subtitle_formats.shutil, "which", lambda _name: None)  # no ffmpeg
    output, info = subtitle_formats.convert_to_plain_srt(subtitle, tmp_path / "cache", ffmpeg_path="ffmpeg")
    assert info["method"] == "python" and len(parsed) == 1
    assert [text for _s, _e, text in subtitle_formats.parse_srt(output)] == ["こんにちは", "さようなら"]


def _ffmpeg_or_skip() -> str:
    import shutil

    import pytest

    binary = shutil.which("ffmpeg")
    if binary is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not installed")
    return binary


def _tone_video(tmp_path: Path, ffmpeg: str, *, tracks: int = 1) -> Path:
    import subprocess

    video = tmp_path / f"episode-{tracks}.mkv"
    inputs = ["-f", "lavfi", "-i", "color=c=black:s=32x32:r=5:d=4"]
    maps = ["-map", "0:v"]
    for track in range(tracks):
        inputs += ["-f", "lavfi", "-i", f"sine=frequency={300 + 200 * track}:sample_rate=44100:duration=4"]
        maps += ["-map", f"{track + 1}:a"]
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", *inputs, *maps, "-c:v", "mpeg4", "-c:a", "aac", "-ac", "2", str(video)],
        check=True,
    )
    return video


def test_alass_reference_audio_keeps_alass_own_samples(tmp_path: Path) -> None:
    import subprocess

    from pudge import audio_timeline, syncing

    ffmpeg = _ffmpeg_or_skip()
    video = _tone_video(tmp_path, ffmpeg)
    timeline = audio_timeline.timeline(video, "ffprobe")
    wav = syncing._alass_reference_audio(video, tmp_path / "cache", ffmpeg_path=ffmpeg, timeline=timeline)
    assert wav is not None and wav.parent.name == "segment-audio"
    # What ALASS reads from the video itself, and what it reads back from the WAV.
    alass_args = ["-acodec", "pcm_s16le", "-ar", "8000", "-ac", "1", "-f", "s16le", "-"]
    index = timeline.audio_indexes[0]
    direct = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(video), "-map", f"0:{index}", *alass_args],
        capture_output=True,
        check=True,
    ).stdout
    again = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(wav), "-map", "0:0", *alass_args], capture_output=True, check=True
    ).stdout
    assert direct and direct == again
    # Reused, not decoded again; nothing temporary left beside it.
    assert (
        syncing._alass_reference_audio(video, tmp_path / "cache", ffmpeg_path=ffmpeg, timeline=timeline)
        == wav
    )
    assert [path.name for path in wav.parent.iterdir()] == [wav.name]


def test_alass_decodes_a_dual_audio_file_itself(tmp_path: Path) -> None:
    from pudge import audio_timeline, syncing

    ffmpeg = _ffmpeg_or_skip()
    video = _tone_video(tmp_path, ffmpeg, tracks=2)
    timeline = audio_timeline.timeline(video, "ffprobe")
    assert len(timeline.audio_indexes) == 2
    assert (
        syncing._alass_reference_audio(video, tmp_path / "cache", ffmpeg_path=ffmpeg, timeline=timeline)
        is None
    )
    assert (
        syncing._alass_reference_audio(video, tmp_path / "cache", ffmpeg_path=ffmpeg, timeline=None) is None
    )


_FAKE_ALASS = """
import json, os, shutil, sys
from pathlib import Path
args = sys.argv[1:]
Path({log!r}).write_text(json.dumps([os.path.realpath(args[0])] + args[1:]), encoding="utf-8")
shutil.copyfile(args[1], args[2])
"""


def test_alass_reads_the_decoded_reference_once_per_video(tmp_path: Path) -> None:
    import sys

    from pudge.config import SyncConfig
    from pudge.syncing import synchronize_with_alass

    import shutil

    ffmpeg = _ffmpeg_or_skip()
    video = _tone_video(tmp_path, ffmpeg)
    # ffmpeg and its ffprobe side by side, as Homebrew installs them.
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in ("ffmpeg", "ffprobe"):
        (tools / name).symlink_to(shutil.which(name))
    ffmpeg = str(tools / "ffmpeg")
    log = tmp_path / "argv.json"
    script = tmp_path / "alass"
    script.write_text(f"#!{sys.executable}\n" + _FAKE_ALASS.format(log=str(log)), encoding="utf-8")
    script.chmod(0o755)
    references = []
    for name in ("a", "b"):
        source = tmp_path / f"{name}.srt"
        subtitle_formats.write_srt([(0.5, 1.5, f"台詞{name}"), (2.0, 3.0, "もう一つ")], source)
        _output, result = synchronize_with_alass(
            video, source, tmp_path / "cache", SyncConfig(), alass_path=str(script), ffmpeg_path=ffmpeg
        )
        assert result["sync_was_successful"] is True, result
        references.append(json.loads(log.read_text(encoding="utf-8"))[0])
    assert references[0] == references[1] and references[0].endswith(".alass8k.wav")


def test_parallel_han_pairs_match_a_full_scan() -> None:
    import random

    rng = random.Random(7)
    cues: list[tuple[float, float, str]] = []
    for index in range(400):
        start = index * 2.0 + rng.uniform(-0.5, 0.5)
        length = rng.uniform(0.3, 6.0)
        cues.append((start, start + length, "これは日本語" if index % 3 else "中文翻译字幕"))
        if index % 4 == 0:
            shift = rng.uniform(-0.3, 0.3)
            cues.append((start + shift, start + length + shift, "漢字行"))

    def full_scan(items):
        kinds = [subtitle_formats._cue_script_kind(text) for _s, _e, text in items]
        japanese = sorted(
            [(i, s, e) for i, ((s, e, _t), k) in enumerate(zip(items, kinds)) if k == "japanese"],
            key=lambda item: (item[1], item[2]),
        )
        found = set()
        for i, ((s, e, _t), k) in enumerate(zip(items, kinds)):
            if k != "han_only":
                continue
            for _j, js, je in japanese:
                if js > e + 0.18:
                    break
                if je < s - 0.18:
                    continue
                overlap = max(0.0, min(e, je) - max(s, js))
                ratio = overlap / max(min(max(e - s, 0.001), max(je - js, 0.001)), 0.001)
                if (abs(s - js) <= 0.18 and abs(e - je) <= 0.18) or (
                    abs(s - js) <= 0.35 and abs(e - je) <= 0.35 and ratio >= 0.9
                ):
                    found.add(i)
                    break
        return found

    expected = full_scan(cues)
    assert expected and subtitle_formats._parallel_han_cue_indices(cues) == expected


def test_onset_shift_votes_match_every_pair() -> None:
    import random

    from pudge import syncing

    rng = random.Random(11)
    source = sorted(rng.uniform(0, 1400) for _ in range(300))
    reference = sorted(value + 3.7 + rng.uniform(-0.2, 0.2) for value in source[:250])

    def every_pair(limit: float) -> list[float]:
        bins: dict[int, int] = {}
        for source_time in source:
            for reference_time in reference:
                shift = reference_time - source_time
                if shift < -limit:
                    continue
                if shift > limit:
                    break
                key = int(round(shift / 0.20))
                bins[key] = bins.get(key, 0) + 1
        ranked = sorted(bins.items(), key=lambda item: (item[1], -abs(item[0])), reverse=True)
        shifts = [key * 0.20 for key, _votes in ranked[:48]]
        return shifts if 0.0 in shifts else [*shifts, 0.0]

    for limit in (5.0, 30.0, 120.0):
        assert syncing._onset_candidate_shifts(source, reference, limit=limit) == every_pair(limit)
