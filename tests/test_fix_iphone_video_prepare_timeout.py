"""IOS-4: preparing an episode for the phone read ffmpeg's progress (stdout) to the end before
its messages (stderr), both through pipes, with no time limit. A damaged file whose decoder
errors filled the stderr pipe (~64 KB) blocked ffmpeg and Pudge for good: the episode stayed
"preparing" until the Mac app restarted. Now the messages go to a file, the run has a time
limit, and a failure is reported ("failed") so the phone can ask again."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from pudge import iphone_media
from pudge.iphone_media import MediaError, prepare_video, video_prepare_timeout

PROBE = """#!{python}
import json
print(json.dumps({{"streams": [{{"index": 0, "codec_type": "video", "codec_name": "vp9", "pix_fmt": "yuv420p"}},
                              {{"index": 1, "codec_type": "audio", "codec_name": "opus"}}],
                  "format": {{"duration": "60"}}}}))
"""

NOISY = """#!{python}
import sys, time
# A damaged input with "-v error": many decoder errors before any progress.
for i in range(3000):
    sys.stderr.write(f"[vp9 @ 0x7f8] error while decoding MB {{i % 120}} {{i % 68}}, bytestream -7\\n")
sys.stderr.flush()
for i in range(10):
    print(f"out_time_us={{i * 1000000}}", flush=True)
open(sys.argv[-1], "wb").write(b"x")
sys.exit({code})
"""

STUCK = """#!{python}
import time
print("out_time_us=0", flush=True)
time.sleep(60)
"""


def _tool(folder: Path, name: str, text: str) -> str:
    path = folder / name
    path.write_text(text.format(python=sys.executable, code=0))
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def tools(tmp_path, monkeypatch):
    from pudge.subtitles import stt

    monkeypatch.setattr(stt, "_select_japanese_audio_stream", lambda _source, _ffprobe: (1, {}))
    source = tmp_path / "Show - 01.mkv"
    source.write_bytes(b"not really a video")
    return tmp_path, source, _tool(tmp_path, "ffprobe", PROBE)


def test_many_ffmpeg_messages_no_longer_block_the_preparation(tools):
    folder, source, ffprobe = tools
    ffmpeg = _tool(folder, "ffmpeg", NOISY)
    seen: list[float] = []
    started = time.monotonic()
    result = prepare_video(source, folder / "video", ffmpeg=ffmpeg, ffprobe=ffprobe, progress=seen.append)
    assert time.monotonic() - started < 20
    assert result["path"].is_file() and result["durationMs"] == 60000
    assert seen and max(seen) > 0


def test_a_failed_run_reports_the_end_of_ffmpegs_messages(tools):
    folder, source, ffprobe = tools
    ffmpeg = folder / "ffmpeg"
    ffmpeg.write_text(NOISY.format(python=sys.executable, code=1))
    ffmpeg.chmod(0o755)
    with pytest.raises(MediaError, match="error while decoding MB"):
        prepare_video(source, folder / "video", ffmpeg=str(ffmpeg), ffprobe=ffprobe)
    assert not list((folder / "video").glob("*.mp4"))


def test_a_stuck_ffmpeg_is_stopped_at_the_time_limit(tools, monkeypatch):
    folder, source, ffprobe = tools
    ffmpeg = _tool(folder, "ffmpeg", STUCK)
    monkeypatch.setattr(iphone_media, "video_prepare_timeout", lambda _duration: 1.0)
    started = time.monotonic()
    with pytest.raises(MediaError, match="did not finish"):
        prepare_video(source, folder / "video", ffmpeg=ffmpeg, ffprobe=ffprobe)
    assert time.monotonic() - started < 15


def test_the_time_limit_follows_the_episode_length():
    assert video_prepare_timeout(0) == 6 * 3600
    assert video_prepare_timeout(5 * 60) == 30 * 60  # a short clip: at least half an hour
    assert video_prepare_timeout(24 * 60) == 24 * 60 * 3 + 600  # a TV episode
    assert video_prepare_timeout(2 * 3600) == 2 * 3600 * 3 + 600  # a film re-encoded slowly
