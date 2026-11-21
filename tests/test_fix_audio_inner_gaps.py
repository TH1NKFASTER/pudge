"""A timestamp gap inside an audio track that starts on time (a remux seam).

Raw PCM/FLAC keeps no timestamps: a whole-track decode drops the gap and everything
after it comes out early by the gap, unless the decode fills it. Synthetic releases
(made with FFmpeg here: tone bursts, the track's timestamps jump by 0.95 s at 9.6 s)
must come out of the STT clock, ALASS on the video, the segment windows (also one
across the gap), the OP/ED fingerprints and the audiobook activity clock on the
playback timeline, within 0.05 s. Already prepared episodes of such files are
prepared again once; files without gaps keep their caches.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from pudge import audio_timeline, intro_skipper, subtitle_migrations, syncing  # noqa: E402
from pudge.subtitle_formats import parse_srt  # noqa: E402
from pudge.subtitles import stt  # noqa: E402
from tests.media_tools import requires_ffmpeg  # noqa: E402
from tests.test_fix_audio_playback_timeline import (  # noqa: E402
    FAKE_ALASS,
    FAKE_WORKER,
    FFMPEG,
    FFPROBE,
    Release,
    _assert_matches,
    _config,
    _make,
    _onsets,
    _onsets_of,
    _starts_within,
)

pytestmark = requires_ffmpeg

GAP_AT = 9.6
GAP = 0.95
AAC = ("-c:a", "aac", "-b:a", "96k")


@pytest.fixture(scope="module")
def gapped(tmp_path_factory):
    folder = tmp_path_factory.mktemp("inner-gaps")
    mkv = _make(folder, "gap.mkv", gap_at=GAP_AT, gap=GAP, audio_codec=AAC)
    releases = {"mkv": mkv, "plain": _make(folder, "plain.mkv", audio_codec=AAC)}
    # MP4 keeps the gap as the duration of the packet before it; MPEG-TS has its own start.
    for suffix in ("mp4", "ts"):
        path = folder / f"gap.{suffix}"
        subprocess.run(
            [FFMPEG, "-v", "error", "-y", "-i", str(mkv.path), "-map", "0", "-c", "copy", str(path)],
            check=True,
        )
        release = Release(path, 0.0, gap_at=GAP_AT, gap=GAP)
        release.audio_zero = mkv.audio_zero  # a remux keeps the streams' starts apart
        releases[suffix] = release
    return releases


GAPPED = ["mkv", "mp4", "ts"]


# ------------------------------------------------------------------- the scan ---


def test_gaps_from_packets() -> None:
    frame = 1024 / 48000
    plain = [(index * frame, frame) for index in range(500)]
    assert audio_timeline.gaps_from_packets(plain) == ()
    # Matroska/MPEG-TS: the next packet comes later.
    seam = plain[:200] + [(pts + 0.95, duration) for pts, duration in plain[200:]]
    ((at, length),) = audio_timeline.gaps_from_packets(seam)
    assert at == pytest.approx(200 * frame) and length == pytest.approx(0.95)
    # MP4: the packet before the gap lasts the gap.
    stretched = list(seam)
    stretched[199] = (stretched[199][0], frame + 0.95)
    assert audio_timeline.gaps_from_packets(stretched) == ((pytest.approx(200 * frame), pytest.approx(0.95)),)
    # No durations: measured from the usual packet spacing. Jitter below 0.1 s is no gap.
    assert len(audio_timeline.gaps_from_packets([(pts, None) for pts, _d in seam])) == 1
    jitter = plain[:100] + [(pts + 0.05, duration) for pts, duration in plain[100:]]
    assert audio_timeline.gaps_from_packets(jitter) == ()


@pytest.mark.parametrize("key", GAPPED)
def test_the_scan_finds_the_gap_in_every_container(gapped, key: str) -> None:
    gaps = audio_timeline.inner_gaps(gapped[key].path, FFPROBE, position=0)
    assert gaps is not None and len(gaps) == 1, gaps
    assert gaps[0][1] == pytest.approx(GAP, abs=0.03)


def test_a_file_without_gaps_has_none_and_keeps_its_cache_names(gapped) -> None:
    assert audio_timeline.inner_gaps(gapped["plain"].path, FFPROBE, position=0) == ()
    assert audio_timeline.gaps_marker(()) == ""
    assert stt._cache_key("x", "m", 1, 0.0, ()) == stt._cache_key("x", "m", 1)
    # Late tracks keep the key they got when they were first padded.
    assert stt._cache_key("x", "m", 1, 1.0) == stt._cache_key("x", "m", 1, 1.0, ())
    assert stt._cache_key("x", "m", 1, 0.0, ((9.6, 0.95),)) != stt._cache_key("x", "m", 1)


# --------------------------------------------------------- whole-track decodes ---


@pytest.mark.parametrize("key", GAPPED)
def test_the_stt_clock_after_a_gap(gapped, tmp_path: Path, monkeypatch, key: str) -> None:
    release = gapped[key]
    worker = tmp_path / "worker.py"
    worker.write_text(FAKE_WORKER, encoding="utf-8")
    python = tmp_path / "python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{worker}" "$@"\n', encoding="utf-8")
    python.chmod(0o755)
    monkeypatch.setenv("PUDGE_PYTHON", str(python))
    reference, info = stt.prepare_japanese_stt_reference(
        release.path,
        tmp_path / "cache",
        ffmpeg_path=FFMPEG,
        ffprobe_path=FFPROBE,
        model="fake",
        timeout_seconds=60,
    )
    assert reference is not None, info
    _assert_matches([cue[0] for cue in parse_srt(reference)], [a for a, _b in release.playback_bursts()])


@pytest.mark.parametrize("key", GAPPED)
def test_alass_on_the_video_after_a_gap(gapped, tmp_path: Path, key: str) -> None:
    release = gapped[key]
    script = tmp_path / "alass"
    script.write_text(
        f"#!{sys.executable}\n" + FAKE_ALASS.format(root=str(Path(__file__).resolve().parents[1])),
        encoding="utf-8",
    )
    script.chmod(0o755)
    output, result = syncing.synchronize_with_alass(
        release.path,
        release.subtitle(tmp_path),
        tmp_path / "cache",
        _config(),
        alass_path=str(script),
        ffmpeg_path=FFMPEG,
        ffprobe_path=FFPROBE,
    )
    assert result["sync_was_successful"] is True, result
    # The stand-in shifts the whole block: one offset fits only when the gap is kept.
    _assert_matches([cue[0] for cue in parse_srt(output)], [a for a, _b in release.playback_bursts()])


@pytest.mark.parametrize("key", GAPPED)
@pytest.mark.parametrize("start", [0.0, 6.0, 12.0])
def test_windows_across_and_after_the_gap(gapped, tmp_path: Path, key: str, start: float) -> None:
    release = gapped[key]
    audio, error = syncing._extract_audio_segment(
        release.path, start, 8.0, tmp_path / "cache", ffmpeg_path=FFMPEG, force=False
    )
    assert audio is not None, error
    _assert_matches([start + value for value in _onsets(audio)], _starts_within(release, start, start + 8.0))


def test_the_opening_fingerprints_after_a_gap(tmp_path: Path, monkeypatch) -> None:
    # OP/ED detection needs 2 minutes; the gap is inside the opening region.
    release = _make(tmp_path, "Show - 01.mkv", length=125.0, gap_at=GAP_AT, gap=GAP, audio_codec=AAC)
    regions: list[tuple[float, object]] = []
    real = intro_skipper.decode_region

    def recording(video, **kwargs):
        samples = real(video, **kwargs)
        regions.append((float(kwargs["start_seconds"]), samples))
        return samples

    monkeypatch.setattr(intro_skipper, "decode_region", recording)
    item = intro_skipper.EpisodeFile(
        path=release.path, episode=1, identity=intro_skipper.identity_of(release.path)
    )
    cache = intro_skipper.IntroSkipperCache(tmp_path / "cache")
    assert intro_skipper._fingerprints_for(
        item, cache, ffmpeg=FFMPEG, ffprobe=FFPROBE, allow_compute=True, cancel_check=None
    )
    from pudge.audio_fingerprints import SAMPLE_RATE

    head_start, head = regions[0]
    _assert_matches(
        [head_start + value for value in _onsets_of(head, SAMPLE_RATE)],
        [a for a, _b in release.playback_bursts()],
    )
    meta = json.loads(intro_skipper._meta_path(cache, item.identity).read_text())
    assert meta["timeline"] == audio_timeline.VERSION


def test_fingerprints_made_before_gaps_were_filled_are_made_again(tmp_path: Path) -> None:
    cache = intro_skipper.IntroSkipperCache(tmp_path / "cache")

    def legacy(release, episode: int, timeline: int) -> intro_skipper.EpisodeFile:
        item = intro_skipper.EpisodeFile(
            path=release.path, episode=episode, identity=intro_skipper.identity_of(release.path)
        )
        meta = intro_skipper._meta_path(cache, item.identity)
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(
            json.dumps({"engine": "pudge-hk-1", "stream": None, "duration": 23.0, "timeline": timeline})
        )
        cache.write_result(item.identity, {"status": "found", "segments": []})
        return item

    seam = legacy(_make(tmp_path, "Show - 02.mkv", gap_at=GAP_AT, gap=GAP, audio_codec=AAC), 2, 1)
    assert intro_skipper._early_legacy_fingerprints(cache, seam, FFPROBE) is True
    assert cache.read_result(seam.identity) is None
    plain = legacy(_make(tmp_path, "Show - 03.mkv", audio_codec=AAC), 3, 1)
    assert intro_skipper._early_legacy_fingerprints(cache, plain, FFPROBE) is False
    assert json.loads(intro_skipper._meta_path(cache, plain.identity).read_text())["timeline"] == 2
    assert cache.read_result(plain.identity) is not None


def test_the_activity_clock_after_a_gap(tmp_path: Path) -> None:
    from pudge.audio_activity import analyze_audio_activity

    release = _make(tmp_path, "book.mka", video_offset=None, gap_at=GAP_AT, gap=GAP, audio_codec=AAC)
    result = analyze_audio_activity(release.path, ffmpeg=FFMPEG, timeout=60)
    last_end = release.playback_bursts()[-1][1]
    assert result["regions"][-1]["end"] == pytest.approx(last_end, abs=0.1), result["regions"][-1]


# ---------------------------------------------------------------- the rebuild ---


def test_unwatched_episodes_with_a_gap_are_prepared_again_once(gapped, tmp_path: Path, monkeypatch) -> None:
    from t71g08_support import subtitle_library

    from pudge.manager_models import LibraryEpisode

    manager, video, _commands = subtitle_library(tmp_path, monkeypatch)
    manager.config.tools.ffprobe = FFPROBE
    shutil.copyfile(gapped["mkv"].path, video)  # aligned with timeline version 1
    rows = {
        video: (gapped["mkv"], "ready", {"engine": "ffsubsync", "audio_timeline": 1}),
        "Show - 02.mkv": (gapped["plain"], "ready", {"engine": "ffsubsync", "audio_timeline": 1}),
        "Show - 03.mkv": (gapped["mkv"], "ready", {"engine": "ffsubsync", "audio_timeline": 2}),
        "Show - 04.mkv": (gapped["mkv"], "watched", {"engine": "ffsubsync"}),
        "Show - 05.mkv": (gapped["mkv"], "ready", {}),
    }
    for number, (name, (release, state, alignment)) in enumerate(rows.items(), 1):
        path = name if isinstance(name, Path) else (manager.config.library.root_dir / name).resolve()
        if not isinstance(name, Path):
            shutil.copyfile(release.path, path)
            subtitle = tmp_path / f"{number}.srt"
            subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n古い\n", encoding="utf-8")
            manager.db.upsert_episode(
                LibraryEpisode(
                    media_id=5,
                    title="Show",
                    episode=number,
                    video_path=path,
                    subtitle_path=subtitle,
                    subtitle_origin="jimaku",
                    state=state,
                )
            )
        manager.db.record_subtitle_history(
            video_path=path,
            media_id=5,
            episode=number,
            source="jimaku",
            candidate_name="old.srt",
            candidate_path=manager.db.episode_by_path(path).subtitle_path,
            status="selected",
            details={"alignment": alignment},
        )
    # The late-track rule does not take them (none starts late); episodes it prepared
    # (mark 1) are not prepared again for that.
    assert subtitle_migrations.run_rule(manager, "late_audio_track") == 0
    assert manager._requeue_audio_gap_tracks() == 1
    queued = {Path(str(job["video_path"])).resolve() for job in manager.db.subtitle_jobs()}
    assert queued == {video}
    assert manager._requeue_audio_gap_tracks() == 0  # once
