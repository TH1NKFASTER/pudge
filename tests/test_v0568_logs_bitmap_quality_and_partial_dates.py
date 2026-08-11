from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig, write_config
from pudge.database import Database
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.providers.anilist import _as_date_string
from pudge.syncing import subtitle_quality_accepted
from pudge.web_app import WebAppApi


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def test_exact_single_special_ignores_semantic_cut_mismatch_when_timing_is_strong() -> None:
    accepted, reason = subtitle_quality_accepted(
        {
            "sync_was_successful": True,
            "timing_reference_validation": {
                "accepted": False,
                "total_samples": 6,
                "reason": "different edit after recap inserts",
                "alignment_mode": "alass-timestamp",
                "structure_reason": "ok",
                "reference_output_structure": {
                    "retained_ratio": 1.0,
                    "source_cues": 70,
                    "aligned_cues": 70,
                },
                "reference_activity": {"available": True, "weighted": 0.8513},
            },
            "candidate_context": {
                "source": "jimaku",
                "entry_anilist_match": True,
                "entry_exact_title_match": True,
                "single_special_exact_entry": True,
                "subtitle_suffix": ".srt",
            },
        }
    )

    assert accepted is True
    assert "одноэпизодный SPECIAL" in reason


def test_exact_single_special_override_never_accepts_bitmap_subtitle() -> None:
    accepted, reason = subtitle_quality_accepted(
        {
            "sync_was_successful": True,
            "timing_reference_validation": {
                "accepted": False,
                "total_samples": 6,
                "reason": "different edit",
                "alignment_mode": "alass-timestamp",
                "structure_reason": "ok",
                "reference_output_structure": {"retained_ratio": 1.0},
                "reference_activity": {"available": True, "weighted": 0.95},
            },
            "candidate_context": {
                "source": "jimaku",
                "entry_anilist_match": True,
                "entry_exact_title_match": True,
                "single_special_exact_entry": True,
                "subtitle_suffix": ".sup",
            },
        }
    )

    assert accepted is False
    assert reason == "different edit"


def test_legacy_ready_sup_row_moves_to_waiting_text_and_requeues(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    video = tmp_path / "Kimetsu.mkv"
    subtitle = tmp_path / "Kimetsu.synced.sup"
    video.write_bytes(b"video")
    subtitle.write_bytes(b"PG")
    db.upsert_episode(
        LibraryEpisode(
            media_id=178788,
            title="Kimetsu no Yaiba: Mugenjou-hen Movie 1 - Akaza Sairai",
            episode=None,
            video_path=video.resolve(),
            subtitle_path=subtitle.resolve(),
            state="ready",
        )
    )

    assert db.repair_bitmap_ready_rows() == 1
    stored = db.episode_by_path(video.resolve())
    assert stored is not None
    assert stored.state == "waiting_text_subtitles"
    assert stored.subtitle_path == subtitle.resolve()
    jobs = db.subtitle_jobs()
    assert len(jobs) == 1
    assert Path(str(jobs[0]["video_path"])) == video.resolve()


def test_manager_refuses_ready_exit_code_for_sup(tmp_path: Path, monkeypatch) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    manager = AnimeManager(cfg, log=lambda _message: None)
    video = tmp_path / "Kimetsu.mkv"
    subtitle = tmp_path / "Kimetsu.sup"
    video.write_bytes(b"video")
    subtitle.write_bytes(b"PG")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=178788,
            title="Kimetsu",
            episode=None,
            video_path=video.resolve(),
            state="waiting_subtitles",
        )
    )
    manager.db.queue_subtitle_job(video.resolve(), 178788, None)

    class FakeProcess:
        def __init__(self, command, **_kwargs):
            self.returncode = 0

        def poll(self):
            return self.returncode

        def communicate(self):
            return (
                f"PREPARED_SUBTITLE={subtitle}\nPREPARE_STATUS=ready\n",
                "",
            )

    monkeypatch.setattr("pudge.manager.subprocess.Popen", FakeProcess)

    assert manager.process_subtitle_jobs(limit=1) == 0
    stored = manager.db.episode_by_path(video.resolve())
    assert stored is not None
    assert stored.state == "waiting_text_subtitles"
    assert stored.subtitle_path == subtitle
    jobs = manager.db.subtitle_jobs()
    assert len(jobs) == 1
    assert jobs[0]["state"] == "needs_action"
    assert jobs[0]["action_code"] == "enable_subtitle_ocr"


def test_anilist_partial_dates_are_preserved() -> None:
    assert _as_date_string({"year": 2026, "month": 4, "day": None}) == "2026-04"
    assert _as_date_string({"year": 2026, "month": None, "day": None}) == "2026"
    assert _as_date_string({"year": 2026, "month": 7, "day": 10}) == "2026-07-10"


def test_release_order_places_summer_followup_after_spring_series(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    root = LibraryAnime(
        media_id=180746,
        title="Shibou Yuugi de Meshi wo Kuu.",
        status="CURRENT",
        season_year=2026,
        start_date="2026-04",
        relations=[
            {
                "relation_type": "SEQUEL",
                "media_id": 209961,
                "title": "Shibou Yuugi de Meshi wo Kuu. 44: CLOUDY BEACH",
                "season_year": 2026,
                "start_date": "2026-07-10",
                "relations": [],
            }
        ],
    )

    payload = api._relation_payload(root)

    assert payload["prequel_levels"] == []
    assert payload["sequel_levels"][0][0]["media_id"] == 209961


def test_release_order_uses_sequel_when_root_has_only_year(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    root = LibraryAnime(
        media_id=180746,
        title="Shibou Yuugi de Meshi wo Kuu.",
        status="CURRENT",
        season_year=2026,
        start_date="2026",
        relations=[
            {
                "relation_type": "SEQUEL",
                "media_id": 209961,
                "title": "Shibou Yuugi de Meshi wo Kuu. 44: CLOUDY BEACH",
                "season_year": 2026,
                "start_date": "2026-07-10",
                "relations": [],
            }
        ],
    )

    payload = api._relation_payload(root)

    assert payload["prequel_levels"] == []
    assert payload["sequel_levels"][0][0]["media_id"] == 209961
