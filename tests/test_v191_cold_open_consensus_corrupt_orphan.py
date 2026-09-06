from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge.filename import parse_anime_filename
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem
from pudge.providers.aria2 import Aria2Client
from pudge.providers.jimaku import subtitle_filename_language_profile
from pudge.syncing import (
    _choose_plateau_shift,
    _restore_embedded_opening_clock_scaffold,
)
from pudge.subtitle_formats import write_srt


def test_sparse_cold_open_accepts_cross_modal_consensus_from_real_hyakkano_shape() -> None:
    onset = {
        "accepted": False,
        "reason": "no_clear_improvement",
        "shift_seconds": 0.0,
        "best_shift_seconds": 7.9,
        "baseline": {"matched": 0, "coverage": 0.0, "mean_error_seconds": None},
        "best": {"matched": 3, "coverage": 0.375, "mean_error_seconds": 0.254},
        "matched_gain": 3,
        "mean_error_gain_seconds": 0.0,
    }
    semantic = {
        "accepted": False,
        "reason": "too_few_semantic_anchors",
        "shift_seconds": 0.0,
        "best_shift_seconds": 8.365,
        "eligible_cues": 8,
        "anchor_count": 1,
        "coverage": 0.125,
        "anchors": [
            {
                "cue_index": 7,
                "shift_seconds": 8.365,
                "similarity": 0.75,
                "margin": 0.4166666667,
                "reference_start_seconds": 35.6,
            }
        ],
    }
    shift, choice = _choose_plateau_shift(
        onset,
        semantic,
        onset_limit_seconds=8.0,
        semantic_limit_seconds=8.0,
        allow_sparse_cross_modal_consensus=True,
    )
    assert abs(shift - 7.9) < 0.001
    assert choice["mode"] == "sparse_cross_modal_consensus"
    assert float(choice["agreement_seconds"]) < 0.5


def test_verified_sparse_speech_plateau_is_not_overwritten_by_coarse_embedded_clock(tmp_path: Path) -> None:
    aligned = tmp_path / "speech-aligned.srt"
    write_srt(
        [
            (17.9, 18.7, "a"),
            (23.9, 24.7, "b"),
            (29.9, 30.7, "c"),
            (35.9, 36.7, "d"),
            (41.9, 42.7, "e"),
            (47.9, 48.7, "f"),
            (53.9, 54.7, "g"),
            (61.9, 62.7, "h"),
            (142.0, 142.8, "main-a"),
            (150.0, 150.8, "main-b"),
            (158.0, 158.8, "main-c"),
            (166.0, 166.8, "main-d"),
            (174.0, 174.8, "main-e"),
            (182.0, 182.8, "main-f"),
            (190.0, 190.8, "main-g"),
        ],
        aligned,
    )
    embedded = {
        "timeline_segments": [
            {"offset_seconds": 3.5, "support": 1, "mean_score": 2.4294, "mean_coverage": 0.8571, "kind": "stable"},
            {"offset_seconds": 0.0, "support": 23, "mean_score": 3.0926, "mean_coverage": 0.8244, "kind": "stable"},
        ],
        "timeline_cold_start": {
            "reason": "cold_start_overlaps_main_boundary",
            "delta_seconds": 4.488,
            "gap_seconds": 105.172,
            "boundary_source_time": 83.417,
        },
        "timeline_early_edit_audio_verification": {
            "required": True,
            "reasons": ["opening_gap_clock_ambiguity"],
            "early_offset_span_seconds": 3.5,
            "early_max_jump_seconds": 3.5,
        },
    }
    speech_result = {
        "offset_seconds": 0.208,
        "stt_opening_plateau_refinement": {
            "applied": True,
            "pre_shift_seconds": 7.9,
            "post_shift_seconds": 0.0,
            "pre_choice": {
                "mode": "sparse_cross_modal_consensus",
                "onset_shift_seconds": 7.9,
                "semantic_shift_seconds": 8.365,
            },
        },
    }
    output, result = _restore_embedded_opening_clock_scaffold(
        aligned,
        embedded,
        speech_result,
        tmp_path / "cache",
    )
    assert output == aligned
    assert result["reason"] == "speech_plateau_authoritative"
    assert result["applied"] is False
    assert abs(float(result["speech_relative_clock_seconds"]) - 7.9) < 0.001


def test_kimishinu_jimaku_names_are_exact_episode_9_and_japanese() -> None:
    names = [
        "きみが死ぬまで恋をしたい.S01E09.わたしの魔法.WEBRip.Netflix.ja[cc].srt",
        "きみが死ぬまで恋をしたい.S01E09.わたしの魔法.WEBRip.ABEMA.ja[cc].srt",
    ]
    for name in names:
        identity = parse_anime_filename(name)
        profile = subtitle_filename_language_profile(name)
        assert identity.episode == 9
        assert identity.season == 1
        assert profile["purity"] == "japanese_only"
        assert profile["japanese_marker"] is True


def test_corrupt_completed_aria2_orphan_is_quarantined_and_redownloaded(tmp_path: Path) -> None:
    video = tmp_path / "Kimi ga Shinu made Koi wo Shitai - 09 [1080p].mkv"
    video.write_bytes(b"\0" * 4096)
    item = DownloadItem(
        torrent_hash="abc123",
        name=video.name,
        state="complete",
        progress=1.0,
        save_path=str(tmp_path),
        content_path=str(video),
        media_id=187260,
        episode=9,
        media_episode=9,
        release_episode=9,
        raw={
            "backend": "aria2",
            "orphaned_metadata": True,
            "total_size": 4096,
            "downloaded": 4096,
            "verified": 4096,
        },
    )

    class DB:
        def __init__(self) -> None:
            self.deleted_jobs: list[Path] = []
            self.deleted_episodes: list[Path] = []
            self.deleted_hashes: list[str] = []

        def downloads(self):
            return [item]

        def delete_subtitle_job(self, path):
            self.deleted_jobs.append(Path(path))

        def delete_episode_record(self, path):
            self.deleted_episodes.append(Path(path))

        def delete_torrent_records(self, torrent_hash):
            self.deleted_hashes.append(str(torrent_hash))

    class Client:
        def __init__(self) -> None:
            self.deleted: list[tuple[str, bool]] = []
            self.closed = False

        def delete(self, torrent_hash, *, delete_files=True):
            self.deleted.append((str(torrent_hash), bool(delete_files)))

        def close(self):
            self.closed = True

    db = DB()
    client = Client()
    manager = object.__new__(AnimeManager)
    manager.db = db
    manager.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    manager.qbt_client = lambda: client
    manager.downloads_enabled = lambda: True
    restarted: list[tuple[int, int | None, bool, bool]] = []

    def search_and_add_best(media_id, *, episode, batch, automatic=False, **_kwargs):
        restarted.append((int(media_id), episode, bool(batch), bool(automatic)))
        return SimpleNamespace(title="fresh replacement")

    manager.search_and_add_best = search_and_add_best

    repaired = manager._repair_unreadable_orphaned_download(
        video,
        media_id=187260,
        episode=9,
        attempts=107,
        probe_error="EBML header parsing failed; Invalid data found when processing input",
    )
    assert repaired is True
    assert not video.exists()
    quarantined = [
        path for path in tmp_path.iterdir()
        if path.name.startswith(video.name + ".pudge-corrupt-")
    ]
    assert len(quarantined) == 1
    assert client.deleted == [("abc123", False)]
    assert db.deleted_jobs == [video.resolve()]
    assert db.deleted_episodes == [video.resolve()]
    assert db.deleted_hashes == ["abc123"]
    assert restarted == [(187260, 9, False, True)]


def test_aria2_orphan_recovery_rejects_obvious_size_mismatch(tmp_path: Path) -> None:
    state = tmp_path / "aria2"
    save = tmp_path / "downloads"
    save.mkdir()
    path = save / "episode.mkv"
    path.write_bytes(b"x" * 1000)
    client = Aria2Client(enabled=False, state_dir=state)
    base = {
        "info_hash": "abcdef",
        "title": path.name,
        "save_path": str(save),
        "media_id": 1,
        "episode": 9,
        "release_episode": 9,
        "is_batch": False,
    }
    bad = dict(base, expected_size_bytes=3000)
    assert client._orphaned_completed_item("gid", bad) is None
    good = dict(base, expected_size_bytes=1100)
    recovered = client._orphaned_completed_item("gid", good)
    assert recovered is not None
    assert recovered.state == "complete"
