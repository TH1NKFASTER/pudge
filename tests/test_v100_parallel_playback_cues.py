from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryEpisode
from pudge.subtitle_formats import (
    clean_srt_for_playback,
    parse_srt,
    subtitle_has_genuine_overlaps,
    write_srt,
)
from pudge.subtitles.timeline_alignment import _suppress_weak_post_opening_tail_transition


def _raw_srt(path: Path, cues: list[tuple[float, float, str]]) -> Path:
    blocks = []
    for index, (start, end, text) in enumerate(cues, start=1):

        def ts(value: float) -> str:
            total = int(round(value * 1000))
            h, total = divmod(total, 3_600_000)
            m, total = divmod(total, 60_000)
            s, ms = divmod(total, 1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        blocks.append(f"{index}\n{ts(start)} --> {ts(end)}\n{text}")
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return path


def test_playback_clean_keeps_simultaneous_srt_cues(tmp_path: Path) -> None:
    source = _raw_srt(
        tmp_path / "episode.srt",
        [
            (366.0, 370.0, "パカパカ…"),
            (367.2, 369.2, "別の台詞"),
            (368.0, 371.0, "三人目"),
        ],
    )

    output, result = clean_srt_for_playback(source, tmp_path / "cache")

    assert result["cleaned"] is True
    assert output.name.startswith("v15-")
    assert parse_srt(output) == [
        (366.0, 370.0, "パカパカ…"),
        (367.2, 369.2, "別の台詞"),
        (368.0, 371.0, "三人目"),
    ]


def test_touching_dialogue_still_gets_mpv_safety_gap(tmp_path: Path) -> None:
    output = tmp_path / "touch.srt"
    write_srt([(1.0, 2.0, "一"), (2.0, 3.0, "二")], output)
    assert parse_srt(output) == [(1.0, 1.9, "一"), (2.0, 3.0, "二")]


def test_overlap_detector_handles_srt_and_ass(tmp_path: Path) -> None:
    srt = _raw_srt(tmp_path / "parallel.srt", [(1.0, 3.0, "一"), (2.5, 4.0, "二")])
    assert subtitle_has_genuine_overlaps(srt) is True

    ass = tmp_path / "parallel.ass"
    ass.write_text(
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,一行目\n"
        "Dialogue: 0,0:00:02.50,0:00:04.00,Default,,0,0,0,,二行目\n",
        encoding="utf-8",
    )
    assert subtitle_has_genuine_overlaps(ass) is True


def test_generation_17_requeues_only_overlap_sources_and_forces_resync(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.set_state("subtitle_validation_generation", "16")

    for episode, overlaps in ((1, True), (2, False)):
        video = cfg.library.root_dir / f"Anime - {episode:02d}.mkv"
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"video")
        candidate = cfg.paths.cache_dir / "jimaku" / f"candidate-{episode}.srt"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        cues = [(1.0, 3.0, "一"), (2.5 if overlaps else 3.5, 4.5, "二")]
        _raw_srt(candidate, cues)
        playback = cfg.paths.cache_dir / "playback-srt" / f"v14-{episode}.srt"
        playback.parent.mkdir(parents=True, exist_ok=True)
        _raw_srt(playback, [(1.0, 2.4, "一"), (2.5, 4.5, "二")])
        manager.db.upsert_episode(
            LibraryEpisode(1, "Anime", episode, video, subtitle_path=playback, state="ready")
        )
        manager.db.record_subtitle_history(
            video_path=video,
            media_id=1,
            episode=episode,
            source="jimaku",
            candidate_name=candidate.name,
            candidate_path=candidate,
            score=80.0,
            status="selected",
            details={},
        )

    assert manager._requeue_legacy_generated_subtitles() == 1
    first = manager.db.episode_by_path(cfg.library.root_dir / "Anime - 01.mkv")
    second = manager.db.episode_by_path(cfg.library.root_dir / "Anime - 02.mkv")
    assert first is not None and first.subtitle_path is None
    assert second is not None and second.subtitle_path is not None
    assert manager.db.get_state(manager._debug_force_subtitle_state_key(first.video_path), "") == "1"
    assert manager.db.get_state("subtitle_validation_generation", "") == "17"


def test_weak_singleton_tail_clock_is_dropped_like_otomege_ending() -> None:
    from pudge.subtitles.timeline_alignment import _suppress_weak_singleton_tail_transition

    source_cues = [(float(i), float(i) + 0.8, "台詞") for i in range(0, 1420, 4)]
    segments = [
        {
            "offset_seconds": 0.0,
            "support": 23,
            "mean_score": 3.5932,
            "mean_coverage": 0.9362,
            "kind": "stable",
        },
        {
            "offset_seconds": -15.75,
            "support": 1,
            "mean_score": 2.9371,
            "mean_coverage": 1.0,
            "kind": "stable",
        },
    ]
    boundaries = [1311.446]

    result_segments, result_boundaries, diagnostics = _suppress_weak_singleton_tail_transition(
        source_cues,
        segments,
        boundaries,
    )

    assert diagnostics["applied"] is True
    assert diagnostics["reason"] == "weak_singleton_tail_clock"
    assert diagnostics["dropped_offset_seconds"] == -15.75
    assert diagnostics["kept_offset_seconds"] == 0.0
    assert len(result_segments) == 1
    assert result_segments[0]["offset_seconds"] == 0.0
    assert result_boundaries == []


def test_tail_clock_with_two_windows_is_not_suppressed() -> None:
    from pudge.subtitles.timeline_alignment import _suppress_weak_singleton_tail_transition

    source_cues = [(float(i), float(i) + 0.8, "台詞") for i in range(0, 1420, 4)]
    segments = [
        {"offset_seconds": 0.0, "support": 23, "kind": "stable"},
        {"offset_seconds": -15.75, "support": 2, "kind": "stable"},
    ]
    result_segments, result_boundaries, diagnostics = _suppress_weak_singleton_tail_transition(
        source_cues,
        segments,
        [1311.446],
    )

    assert diagnostics["applied"] is False
    assert diagnostics["reason"] == "tail_has_multiple_windows"
    assert result_segments == segments
    assert result_boundaries == [1311.446]


def _dense_cues(duration: int, *, skip: tuple[float, float] | None = None):
    cues = []
    for start in range(0, duration, 5):
        if skip is not None and skip[0] <= start <= skip[1]:
            continue
        cues.append((float(start), float(start) + 2.0, f"cue-{start}"))
    return cues


def test_v104_drops_weak_two_window_tail_after_strong_post_opening_clock() -> None:
    segments = [
        {"offset_seconds": 0.577, "support": 18, "kind": "cold_start"},
        {"offset_seconds": -9.0, "support": 19, "kind": "post_opening_reacquire"},
        {"offset_seconds": -4.0, "support": 2, "kind": "stable"},
    ]
    boundaries = [116.53, 1160.5]

    fixed_segments, fixed_boundaries, guard = _suppress_weak_post_opening_tail_transition(
        _dense_cues(1421), segments, boundaries
    )

    assert guard["applied"] is True
    assert guard["reason"] == "weak_post_opening_tail_clock"
    assert fixed_boundaries == [116.53]
    assert fixed_segments[-1]["offset_seconds"] == -9.0


def test_v104_keeps_support_two_tail_without_post_opening_reacquire() -> None:
    segments = [
        {"offset_seconds": 0.0, "support": 81, "kind": "stable"},
        {"offset_seconds": 95.25, "support": 2, "kind": "stable"},
    ]
    boundaries = [4413.5]

    fixed_segments, fixed_boundaries, guard = _suppress_weak_post_opening_tail_transition(
        _dense_cues(4900), segments, boundaries
    )

    assert guard["applied"] is False
    assert fixed_segments == segments
    assert fixed_boundaries == boundaries


def test_v104_keeps_post_opening_tail_when_boundary_has_real_silence() -> None:
    segments = [
        {"offset_seconds": -9.0, "support": 19, "kind": "post_opening_reacquire"},
        {"offset_seconds": -4.0, "support": 2, "kind": "stable"},
    ]
    boundaries = [1160.5]

    fixed_segments, fixed_boundaries, guard = _suppress_weak_post_opening_tail_transition(
        _dense_cues(1421, skip=(1120.0, 1200.0)), segments, boundaries
    )

    assert guard["applied"] is False
    assert guard["reason"] == "tail_boundary_has_real_silence"
    assert fixed_segments == segments
    assert fixed_boundaries == boundaries


def test_v108_matching_release_crc_accepts_exact_release() -> None:
    from pathlib import Path
    from pudge.syncing import _matching_release_crc

    video = Path("[Erai-raws] Fullmetal Alchemist - Brotherhood - 44 [1080p][3210D25C].mkv")
    subtitle = Path("[Erai-raws] Fullmetal Alchemist - Brotherhood - 44 [1080p][3210D25C].ja.srt")
    assert _matching_release_crc(video, subtitle) == "3210d25c"


def test_v108_matching_release_crc_rejects_different_release() -> None:
    from pathlib import Path
    from pudge.syncing import _matching_release_crc

    video = Path("[Erai-raws] Fate Strange Fake - 08 [172973FA].mkv")
    subtitle = Path("TV Anime Fate strange Fake S01E08 [AABBCCDD].ja.srt")
    assert _matching_release_crc(video, subtitle) is None


def test_v108_matching_release_crc_requires_bracketed_crc() -> None:
    from pathlib import Path
    from pudge.syncing import _matching_release_crc

    video = Path("Show - 01 3210D25C.mkv")
    subtitle = Path("Show - 01 3210D25C.ja.srt")
    assert _matching_release_crc(video, subtitle) is None


def test_v108_exact_release_result_keeps_native_zero_clock() -> None:
    from pathlib import Path
    from pudge.syncing import _exact_release_zero_offset_result

    video = Path("[Group] Show - 01 [DEADBEEF].mkv")
    subtitle = Path("[Group] Show - 01 [DEADBEEF].ja.ass")
    converted = Path("cache/converted/show.srt")
    result = _exact_release_zero_offset_result(video, subtitle, converted)
    assert result is not None
    assert result["timeline_alignment_reliable"] is True
    assert result["offset_seconds"] == 0.0
    assert result["output"] == str(converted)
    assert result["timeline_exact_release_guard"]["crc"] == "DEADBEEF"
    assert result["timeline_segments"] == [
        {
            "source_start": 0.0,
            "source_end": None,
            "offset_seconds": 0.0,
            "support": 0,
            "mean_score": 0.0,
            "mean_coverage": 0.0,
            "kind": "exact_release_crc",
        }
    ]


def test_v110_exact_anilist_identity_recovers_persisted_equal_ids() -> None:
    from types import SimpleNamespace
    from pudge.syncing import _candidate_has_exact_anilist_identity

    candidate = SimpleNamespace(
        details={"entry_anilist_id": 5114, "requested_anilist_id": "5114"}
    )
    assert _candidate_has_exact_anilist_identity(candidate) is True


def test_v110_exact_anilist_identity_rejects_different_ids() -> None:
    from types import SimpleNamespace
    from pudge.syncing import _candidate_has_exact_anilist_identity

    candidate = SimpleNamespace(
        details={"entry_anilist_id": 5114, "requested_anilist_id": 9999}
    )
    assert _candidate_has_exact_anilist_identity(candidate) is False


def test_v110_exact_anilist_identity_keeps_live_derived_flag() -> None:
    from types import SimpleNamespace
    from pudge.syncing import _candidate_has_exact_anilist_identity

    candidate = SimpleNamespace(
        details={"entry_anilist_match": True}
    )
    assert _candidate_has_exact_anilist_identity(candidate) is True
