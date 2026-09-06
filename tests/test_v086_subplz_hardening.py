from pathlib import Path
import subprocess

from pudge import media
from pudge import syncing
from pudge.reading_audio_alignment import (
    _best_dense_anchor_chain,
    _fuzzy_global_anchor_chain,
    align_light_novel_to_transcript,
)


def _japanese_stream(index: int, title: str, *, default: int = 0, forced: int = 0, comment: int = 0):
    return {
        "index": index,
        "codec_type": "subtitle",
        "codec_name": "ass",
        "tags": {"language": "jpn", "title": title},
        "disposition": {"default": default, "forced": forced, "comment": comment},
    }


def test_embedded_japanese_prefers_full_dialogue_over_commentary_and_forced(monkeypatch):
    monkeypatch.setattr(
        media,
        "probe_media",
        lambda *_: {
            "streams": [
                _japanese_stream(1, "Director Commentary", default=1, comment=1),
                _japanese_stream(2, "Signs & Songs", forced=1),
                _japanese_stream(3, "Japanese Full Dialogue"),
            ]
        },
    )

    candidates = media.find_embedded_japanese_subtitles(Path("episode.mkv"), "ffprobe", "ffmpeg")

    assert [candidate.stream_index for candidate in candidates] == [3]


def test_embedded_japanese_default_is_only_a_tiebreaker(monkeypatch):
    monkeypatch.setattr(
        media,
        "probe_media",
        lambda *_: {
            "streams": [
                _japanese_stream(4, "Japanese Full Dialogue"),
                _japanese_stream(5, "Japanese Full Dialogue", default=1),
            ]
        },
    )

    candidates = media.find_embedded_japanese_subtitles(Path("episode.mkv"), "ffprobe", "ffmpeg")

    assert candidates[0].stream_index == 5


def test_timing_reference_rejects_commentary_even_when_default():
    streams = [
        {
            "index": 7,
            "codec_type": "subtitle",
            "codec_name": "subrip",
            "tags": {"language": "eng", "title": "Director Commentary"},
            "disposition": {"default": 1, "comment": 1},
        },
        {
            "index": 8,
            "codec_type": "subtitle",
            "codec_name": "ass",
            "tags": {"language": "eng", "title": "Full Dialogue"},
            "disposition": {"default": 0},
        },
    ]

    selected = syncing._select_timing_reference_stream(streams)

    assert selected is not None
    assert selected["index"] == 8


def test_text_sample_uses_bounded_probe_and_timeout(monkeypatch, tmp_path):
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        Path(command[-1]).write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    text = media._extract_text_sample(video, 2, "ffmpeg")

    assert "日本語" in text
    assert "-nostdin" in captured["command"]
    assert captured["command"][captured["command"].index("-probesize") + 1] == "10M"
    assert captured["command"][captured["command"].index("-analyzeduration") + 1] == "10000000"
    assert captured["kwargs"]["timeout"] == 45


def test_text_sample_timeout_is_nonfatal(monkeypatch, tmp_path):
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")

    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 0))

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    assert media._extract_text_sample(video, 2, "ffmpeg") == ""


def test_embedded_timing_reference_is_published_atomically(monkeypatch, tmp_path):
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    calls = []

    monkeypatch.setattr(syncing, "_resolve_command", lambda command: command)

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0] == "ffprobe":
            payload = {
                "streams": [
                    {
                        "index": 4,
                        "codec_type": "subtitle",
                        "codec_name": "subrip",
                        "tags": {"language": "eng", "title": "Full Dialogue"},
                        "disposition": {"default": 1},
                    }
                ]
            }
            import json
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
        temp_output = Path(command[-1])
        assert temp_output.name.startswith(".")
        assert temp_output.suffix == ".srt"
        temp_output.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nHello\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\nWorld\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(syncing.subprocess, "run", fake_run)
    output, result = syncing.extract_embedded_timing_reference(video, tmp_path / "cache")

    assert output is not None and output.exists()
    assert result["reason"] == "applied"
    ffmpeg_command = calls[1]
    assert "-nostdin" in ffmpeg_command
    assert ffmpeg_command[ffmpeg_command.index("-probesize") + 1] == "10M"
    assert ffmpeg_command[ffmpeg_command.index("-analyzeduration") + 1] == "10000000"
    assert not list(output.parent.glob(".*.tmp.srt"))


def test_fuzzy_global_ln_alignment_recovers_when_no_four_char_anchor_exists():
    novel = "".join(chr(0x4E00 + index) for index in range(120))
    transcript = "".join("ゑ" if index % 4 == 3 else char for index, char in enumerate(novel))

    exact_size, exact = _best_dense_anchor_chain(novel, transcript)
    fuzzy_size, fuzzy, similarity = _fuzzy_global_anchor_chain(novel, transcript)

    assert exact_size == 0
    assert exact == []
    assert fuzzy_size == 3
    assert len(fuzzy) >= 8
    assert similarity >= 0.70

    alignment = align_light_novel_to_transcript(
        [{"chapter_index": 1, "title": "chapter", "text": novel}],
        [{"start": 0.0, "end": 60.0, "text": transcript}],
        duration=60.0,
        model="test",
    )
    assert alignment["alignment_method"] == "fuzzy-global"
    assert alignment["fuzzy_similarity"] >= 0.70
    assert alignment["matched_anchor_count"] >= 8


def test_invalid_cached_timing_reference_is_rebuilt(monkeypatch, tmp_path):
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    monkeypatch.setattr(syncing, "_resolve_command", lambda command: command)

    import hashlib
    stat = video.stat()
    digest = hashlib.sha1(
        (
            f"{video.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:"
            "stream=4:timing-reference-v1"
        ).encode()
    ).hexdigest()[:20]
    cached = tmp_path / "cache" / "timing-reference" / f"{digest}.srt"
    cached.parent.mkdir(parents=True)
    cached.write_text("partial garbage", encoding="utf-8")

    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0] == "ffprobe":
            import json
            payload = {"streams": [{
                "index": 4,
                "codec_type": "subtitle",
                "codec_name": "subrip",
                "tags": {"language": "eng", "title": "Full Dialogue"},
                "disposition": {"default": 1},
            }]}
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
        Path(command[-1]).write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nRebuilt\n", encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(syncing.subprocess, "run", fake_run)
    output, result = syncing.extract_embedded_timing_reference(video, tmp_path / "cache")

    assert output == cached
    assert result["reason"] == "applied"
    assert "Rebuilt" in cached.read_text(encoding="utf-8")
    assert len(calls) == 2
