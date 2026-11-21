"""Smaller copies of an episode for the phone ("like YouTube": keep more episodes on the device).

- "smaller" (720p HEVC ~1.4 Mbit/s, AAC 96k) and "saver" (540p, ~0.75 Mbit/s, AAC 64k) are
  re-encodes: VideoToolbox HEVC on the Mac (its H.264 when the GPU has no HEVC encoder),
  libx265 elsewhere; the Japanese audio track is kept, the moov atom is in front;
- each quality is cached separately (its own key and prepared row); the original's key is
  the one of before (copies prepared earlier stay valid);
- the phone asks with ``quality`` (query or body); an older phone (nothing) gets the original;
- every episode answer lists the qualities with their sizes (estimated from bit rate x
  duration until prepared);
- the timing is the source's: the same start and duration as the original copy (the Mac's
  subtitles fit every copy).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from test_fix_iphone_video_stream import FAKE_PROBE, _connection, _episode, server, service  # noqa: F401 - fixtures

from pudge import iphone_media
from pudge.iphone_media import VIDEO_QUALITIES, prepare_video, video_quality, video_quality_bytes

FAKE_FFMPEG_FAILING_HEVC = """#!{python}
import json, sys
calls = json.loads(open({log!r}).read()) if __import__("os").path.exists({log!r}) else []
calls.append(sys.argv[1:])
open({log!r}, "w").write(json.dumps(calls))
if "hevc_videotoolbox" in sys.argv and {fail_hevc!r}:
    sys.stderr.write("Error: cannot create compression session\\n")
    sys.exit(1)
open(sys.argv[-1], "wb").write(b"x")
"""


def _tools(
    tmp_path: Path, monkeypatch, *, fail_hevc: bool = False, codec: str = "h264"
) -> tuple[Path, Path, Path]:
    from pudge.subtitles import stt

    monkeypatch.setattr(stt, "_select_japanese_audio_stream", lambda _source, _ffprobe: (1, {}))
    log = tmp_path / "calls.json"
    probe = tmp_path / "ffprobe"
    probe.write_text(FAKE_PROBE.format(python=sys.executable, codec=codec, pix="yuv420p"))
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_text(
        FAKE_FFMPEG_FAILING_HEVC.format(python=sys.executable, log=str(log), fail_hevc=fail_hevc)
    )
    for tool in (probe, ffmpeg):
        tool.chmod(0o755)
    return probe, ffmpeg, log


class _Uname:
    def __init__(self, name: str) -> None:
        self.sysname = name


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "Show - 01.mkv"
    source.write_bytes(b"source")
    return source


def test_the_smaller_copy_is_a_hardware_hevc_reencode_on_the_mac(tmp_path, monkeypatch) -> None:
    probe, ffmpeg, log = _tools(tmp_path, monkeypatch)
    monkeypatch.setattr(iphone_media.os, "uname", lambda: _Uname("Darwin"))
    result = prepare_video(
        _source(tmp_path), tmp_path / "video", ffmpeg=str(ffmpeg), ffprobe=str(probe), quality="smaller"
    )
    (argv,) = json.loads(log.read_text())
    joined = " ".join(argv)
    assert "-c:v hevc_videotoolbox" in joined and "-tag:v hvc1" in joined
    assert "-b:v 1400k" in joined
    assert "scale=-2:'2*trunc(min(720,ih)/2)'" in argv  # even height (REVIEW: odd heights failed)
    # The Japanese track (index 1), re-encoded small; no subtitles, moov first.
    assert argv[argv.index("-map", argv.index("-map") + 1) + 1] == "0:1"
    assert "-c:a aac -b:a 96k -ac 2" in joined
    assert argv[argv.index("-movflags") + 1] == "+faststart"
    # Timing as the source's: nothing that seeks or shifts.
    assert not {"-ss", "-itsoffset", "-output_ts_offset", "-t"} & set(argv)
    assert result["path"].is_file() and result["durationMs"] == 60000


def test_a_mac_without_an_hevc_encoder_falls_back_to_videotoolbox_h264(tmp_path, monkeypatch) -> None:
    probe, ffmpeg, log = _tools(tmp_path, monkeypatch, fail_hevc=True)
    monkeypatch.setattr(iphone_media.os, "uname", lambda: _Uname("Darwin"))
    result = prepare_video(
        _source(tmp_path), tmp_path / "video", ffmpeg=str(ffmpeg), ffprobe=str(probe), quality="saver"
    )
    calls = json.loads(log.read_text())
    assert len(calls) == 2
    assert "hevc_videotoolbox" in calls[0]
    second = " ".join(calls[1])
    assert "-c:v h264_videotoolbox" in second and "-tag:v avc1" in second
    assert "scale=-2:'2*trunc(min(540,ih)/2)'" in calls[1] and "-b:a 64k" in second
    assert result["path"].is_file()
    assert not list((tmp_path / "video").glob("*.part.mp4"))


def test_off_the_mac_the_smaller_copy_uses_libx265(tmp_path, monkeypatch) -> None:
    probe, ffmpeg, log = _tools(tmp_path, monkeypatch)
    monkeypatch.setattr(iphone_media.os, "uname", lambda: _Uname("Linux"))
    prepare_video(
        _source(tmp_path), tmp_path / "video", ffmpeg=str(ffmpeg), ffprobe=str(probe), quality="smaller"
    )
    (argv,) = json.loads(log.read_text())
    assert "-c:v libx265" in " ".join(argv) and "-tag:v hvc1" in " ".join(argv)


def test_each_quality_has_its_own_copy_and_the_original_key_is_unchanged(tmp_path, monkeypatch) -> None:
    probe, ffmpeg, _log = _tools(tmp_path, monkeypatch)
    source = _source(tmp_path)
    paths = {
        quality: prepare_video(
            source, tmp_path / "video", ffmpeg=str(ffmpeg), ffprobe=str(probe), quality=quality
        )["path"]
        for quality in VIDEO_QUALITIES
    }
    assert len(set(paths.values())) == 3
    stat = source.stat()
    assert (
        paths["original"].name
        == hashlib.sha256(
            f"{source.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:iphone-mp4-v1".encode()
        ).hexdigest()[:32]
        + ".mp4"
    )
    # An unknown quality (or none, an older phone) is the original.
    assert video_quality("4k") == video_quality(None) == "original"
    again = prepare_video(source, tmp_path / "video", ffmpeg=str(ffmpeg), ffprobe=str(probe), quality="bogus")
    assert again["path"] == paths["original"]


def test_sizes_are_estimated_from_bit_rate_and_duration() -> None:
    minutes24 = 24 * 60.0
    source = 1_500_000_000
    smaller = video_quality_bytes("smaller", minutes24, source_bytes=source, source_kbps=8300)
    saver = video_quality_bytes("saver", minutes24, source_bytes=source, source_kbps=8300)
    assert 250_000_000 <= smaller <= 300_000_000
    assert 130_000_000 <= saver <= 160_000_000
    # The original copies the video: about the source's size; re-encoded: 3.5 Mbit/s.
    assert video_quality_bytes("original", minutes24, source_bytes=source) == source
    assert (
        600_000_000
        < video_quality_bytes("original", minutes24, source_bytes=source, copies_video=False)
        < 700_000_000
    )
    # A small source is never made bigger: its bit rate is followed.
    small = video_quality_bytes("smaller", minutes24, source_bytes=120_000_000, source_kbps=660)
    assert small <= 120_000_000
    assert video_quality_bytes("smaller", 0) is None


def _fake_worker_prepare(monkeypatch, seen: list[str]) -> None:
    def fake_prepare(source, folder, *, quality="original", **_kwargs):
        seen.append(quality)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{quality}.mp4"
        target.write_bytes(quality.encode() * 100)
        return {"path": target, "durationMs": 1440000}

    monkeypatch.setattr("pudge.iphone_media.prepare_video", fake_prepare)


def test_the_phone_asks_for_a_quality_and_each_is_prepared_and_kept_apart(
    service, tmp_path, monkeypatch
) -> None:  # noqa: F811
    episode = _episode(service, tmp_path)
    seen: list[str] = []
    _fake_worker_prepare(monkeypatch, seen)
    monkeypatch.setattr(
        service.media, "episode_subtitles", lambda _id: {"schema": 1, "tokenized": True, "cues": []}
    )
    started = []
    monkeypatch.setattr(service.media, "_prepare_worker", lambda *args: started.append(args))
    media = service.media
    answer = media.handle(
        "POST", f"/api/v2/media/episodes/{episode}/prepare", {}, lambda: {"quality": "smaller"}
    )
    assert answer["state"] == "preparing" and answer["quality"] == "smaller"
    assert started == [(episode, "smaller")]
    assert (episode, "smaller") in media.video_jobs and episode not in media.video_jobs
    # An older phone: an empty body, the original (the call of before).
    media.handle("POST", f"/api/v2/media/episodes/{episode}/prepare", {}, lambda: {})
    assert started[-1] == (episode,)
    monkeypatch.undo()
    _fake_worker_prepare(monkeypatch, seen)
    monkeypatch.setattr(
        service.media, "episode_subtitles", lambda _id: {"schema": 1, "tokenized": True, "cues": []}
    )
    media.video_jobs.clear()
    media._prepare_worker(episode, "smaller")
    assert seen == ["smaller"]
    smaller = media.handle("GET", f"/api/v2/media/episodes/{episode}", {"quality": ["smaller"]}, lambda: {})
    original = media.handle("GET", f"/api/v2/media/episodes/{episode}", {}, lambda: {})
    assert smaller["state"] == "ready" and smaller["video"]["sizeBytes"] == len(b"smaller" * 100)
    assert original["state"] == "idle" and "video" not in original
    with service.db.connect() as conn:
        rows = {row[0] for row in conn.execute("SELECT source FROM iphone_prepared")}
    assert rows == {f"video:{episode}:smaller"}
    options = {item["id"]: item for item in smaller["qualities"]}
    assert set(options) == {"original", "smaller", "saver"}
    assert options["smaller"] == {"id": "smaller", "prepared": True, "sizeBytes": 700}
    assert options["original"]["prepared"] is False
    # The original's own preparation does not touch the smaller copy.
    media._prepare_worker(episode)
    assert media.episode(episode)["video"]["sizeBytes"] == len(b"original" * 100)
    assert media.episode(episode, quality="smaller")["video"]["sizeBytes"] == 700
    # The list offers the choices too (the original stays "prepared" as before).
    item = media._episode_item(
        {
            "id": episode,
            "media_id": 105228,
            "number": 3,
            "anime_title": "Dorohedoro",
            "title": "Ep",
            "state": "ready",
            "playback_position": 0,
            "playback_duration": 0,
            "video_path": str(Path(media._episode_row(episode)["video_path"])),
        }
    )
    assert item["prepared"] is True
    assert {option["id"] for option in item["qualities"] if option["prepared"]} == {"original", "smaller"}


def test_a_stream_opens_on_the_quality_asked_for(service, tmp_path, monkeypatch) -> None:  # noqa: F811
    episode = _episode(service, tmp_path)
    _fake_worker_prepare(monkeypatch, [])
    monkeypatch.setattr(
        service.media, "episode_subtitles", lambda _id: {"schema": 1, "tokenized": True, "cues": []}
    )
    service.media._prepare_worker(episode, "saver")
    opened = service.media.handle(
        "POST", f"/api/v2/media/episodes/{episode}/stream", {}, lambda: {"quality": "saver"}, device="ipad"
    )
    assert opened["state"] == "ready" and "stream" in opened
    assert opened["video"]["sizeBytes"] == len(b"saver" * 100)
    (stream,) = service.media._streams.values()
    assert stream["path"].endswith("saver.mp4")


def test_the_mac_tells_phones_it_has_smaller_copies(service, server) -> None:  # noqa: F811
    connection = _connection(service, server)
    try:
        connection.request("GET", "/api/v2/capabilities")
        answer = json.loads(connection.getresponse().read())
    finally:
        connection.close()
    assert "video-quality" in answer["capabilities"]


def _probe(path: Path) -> dict:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,start_time,height:stream_tags=language:format=start_time,duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg")
def test_a_real_smaller_copy_keeps_the_timing_and_the_japanese_track(tmp_path) -> None:
    encoders = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if "libx265" not in encoders:
        pytest.skip("needs libx265")
    source = tmp_path / "Show - 02.mkv"
    # A capture that does not start at 0 (TV recordings), English track first, Japanese second.
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=1280x720:rate=24:duration=4",
            "-f",
            "lavfi",
            "-i",
            "sine=f=300:duration=4",
            "-f",
            "lavfi",
            "-i",
            "sine=f=600:duration=4",
            "-map",
            "0",
            "-map",
            "1",
            "-map",
            "2",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-metadata:s:a:0",
            "language=eng",
            "-metadata:s:a:1",
            "language=jpn",
            "-output_ts_offset",
            "1.5",
            str(source),
        ],
        check=True,
    )
    original = prepare_video(source, tmp_path / "video", ffmpeg="ffmpeg", ffprobe="ffprobe")
    smaller = prepare_video(source, tmp_path / "video", ffmpeg="ffmpeg", ffprobe="ffprobe", quality="saver")
    a, b = _probe(original["path"]), _probe(smaller["path"])
    assert abs(float(a["format"]["duration"]) - float(b["format"]["duration"])) < 0.1
    assert abs(float(a["format"].get("start_time", 0)) - float(b["format"].get("start_time", 0))) < 0.05
    for kind in ("video", "audio"):
        first = next(s for s in a["streams"] if s["codec_type"] == kind)
        second = next(s for s in b["streams"] if s["codec_type"] == kind)
        assert abs(float(first.get("start_time", 0)) - float(second.get("start_time", 0))) < 0.05, kind
    video = next(s for s in b["streams"] if s["codec_type"] == "video")
    assert video["codec_name"] == "hevc" and video["height"] == 540
    audio = [s for s in b["streams"] if s["codec_type"] == "audio"]
    assert len(audio) == 1
    assert smaller["durationMs"] == original["durationMs"]
    # The same 300/600 Hz choice as the original: the Japanese one (600 Hz) in both.
    assert len([s for s in a["streams"] if s["codec_type"] == "audio"]) == 1
    from test_fix_iphone_video_stream import _top_level_boxes

    boxes = _top_level_boxes(smaller["path"])
    assert boxes.index("moov") < boxes.index("mdat")
