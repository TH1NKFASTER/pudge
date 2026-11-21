"""Cache folders without a limit, nested converted/ folders, and the CRC shortcut.

SYNC-14: stt/, reference-*/, piecewise/, stt-*/, semantic-offset/, speech-constant/,
source-repairs/, embedded-opening-scaffold/ and jimaku-api/ had no eviction policy.
``final_timing_runs`` and the exact-Jimaku consensus passed the output's folder as
the cache root, so ASS conversions landed in nested ``timeline-alignment/converted/``
and ``alass/converted/`` folders that no policy sees.

SYNC-15: a subtitle sharing the video's 8-digit CRC was taken at zero offset with
no check. A file timed to another source and renamed to the release name kept its
offset; the shortcut now needs the cues to line up with the embedded track.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from pudge import syncing
from pudge.cache_management import DERIVED_CACHE_POLICIES, enforce_derived_caches
from pudge.database import Database
from pudge.manager_models import LibraryEpisode
from pudge.subtitles.timing_runs import final_timing_runs

DAY = 24 * 3600.0


def _aged(path: Path, age: float, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def test_unmanaged_cache_folders_have_policies(tmp_path: Path) -> None:
    folders = {policy.folder for policy in DERIVED_CACHE_POLICIES.values()}
    for name in (
        "stt",
        "piecewise",
        "semantic-offset",
        "speech-constant",
        "source-repairs",
        "embedded-opening-scaffold",
        "reference-piecewise",
        "reference-groups",
        "stt-text-clock",
        "jimaku-api",
    ):
        assert name in folders, name

    db = Database(tmp_path / "db.sqlite3")
    cache = tmp_path / "cache"
    video = _aged(tmp_path / "library" / "Show - 01.mkv", 0)
    playing = _aged(cache / "reference-piecewise" / "live.srt", 400 * DAY)
    db.upsert_episode(
        LibraryEpisode(media_id=1, title="Show", episode=1, video_path=video, subtitle_path=playing)
    )
    old_piecewise = _aged(cache / "piecewise" / "old.srt", 200 * DAY)
    new_piecewise = _aged(cache / "piecewise" / "new.srt", DAY)
    old_stt = cache / "stt" / "aaaa"
    _aged(old_stt / "reference.ja.srt", 200 * DAY)
    _aged(old_stt / "metadata.json", 200 * DAY)
    os.utime(old_stt, (time.time() - 200 * DAY,) * 2)
    new_stt = cache / "stt" / "bbbb"
    _aged(new_stt / "reference.ja.srt", DAY)
    # An expired (mtime 0) Jimaku response is the offline copy: not removed for its age.
    response = _aged(cache / "jimaku-api" / ("a" * 32 + ".json"), time.time())
    budget = _aged(cache / "jimaku-api" / "request-budget.json", 400 * DAY)

    report = enforce_derived_caches(db, cache, force=True)

    assert playing.exists()  # an episode plays it
    assert not old_piecewise.exists() and new_piecewise.exists()
    assert not old_stt.exists() and new_stt.is_dir()
    assert response.exists() and budget.exists()
    assert report["folders"]["stt"]["removed"] == 1


ASS = """[Script Info]
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,60,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,こんにちは
Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,ありがとう
"""


def test_timing_runs_convert_into_the_real_cache_root(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    raw = tmp_path / "Show - 01.ja.ass"
    raw.write_text(ASS, encoding="utf-8")
    output = cache / "timeline-alignment" / "aligned.srt"
    output.parent.mkdir(parents=True)
    output.write_text(
        "1\n00:00:02,000 --> 00:00:03,000\nこんにちは\n\n2\n00:00:04,000 --> 00:00:05,000\nありがとう\n",
        encoding="utf-8",
    )
    runs = final_timing_runs(raw, output, cache_dir=cache)
    assert [run["offset_seconds"] for run in runs] == [1.0]
    assert not (output.parent / "converted").exists()
    assert list((cache / "converted").glob("*.srt"))


def test_exact_jimaku_consensus_uses_the_cache_root(tmp_path: Path, monkeypatch) -> None:
    from pudge.models import SubtitleCandidate

    seen: list[Path | None] = []

    def verdict(_raw, _final, *, cache_dir=None):
        seen.append(cache_dir)
        return {"status": "rejected", "reason": "test"}

    monkeypatch.setattr(syncing, "validate_alignment_output", verdict)
    aligned = tmp_path / "cache" / "alass" / "x.srt"
    candidate = SubtitleCandidate(path=tmp_path / "a.ass", source="jimaku", score=1.0, name="a.ass")
    items = [((1.0,), candidate, aligned, {}, {}, {})]
    syncing._exact_jimaku_timing_consensus(items, cache_dir=tmp_path / "cache")
    assert seen == [tmp_path / "cache"]


def _srt(path: Path, shift: float) -> Path:
    blocks = []
    for index in range(40):
        start = 10.0 + index * 7.0 + shift
        blocks.append(f"{index + 1}\n{_ts(start)} --> {_ts(start + 2.5)}\nline {index}")
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return path


def _ts(value: float) -> str:
    milliseconds = int(round(value * 1000))
    hours, rest = divmod(milliseconds, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def test_crc_shortcut_needs_the_cues_to_match_the_embedded_track(tmp_path: Path) -> None:
    video = Path("[SubsPlease] Show - 05 (1080p) [A1B2C3D4].mkv")
    subtitle = Path("[SubsPlease] Show - 05 (1080p) [A1B2C3D4].ja.srt")
    reference = _srt(tmp_path / "reference.srt", 0.0)
    timed_here = _srt(tmp_path / "here.srt", 0.0)
    timed_elsewhere = _srt(tmp_path / "netflix.srt", 3.2)

    result = syncing._exact_release_zero_offset_result(video, subtitle, timed_here, reference)
    assert result is not None and result["offset_seconds"] == 0.0
    assert result["timeline_exact_release_guard"]["reference_activity"] >= 0.8
    assert syncing._exact_release_zero_offset_result(video, subtitle, timed_elsewhere, reference) is None
    # Without an embedded track to compare with, the name alone still decides (unchanged).
    assert syncing._exact_release_zero_offset_result(video, subtitle, timed_elsewhere) is not None
    json.dumps(result)  # stays serialisable for the debug log
