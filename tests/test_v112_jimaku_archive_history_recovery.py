from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

from pudge.filename import parse_anime_filename
from pudge.manager import AnimeManager
from pudge.models import JimakuFile, VideoIdentity
from pudge.providers.jimaku import JimakuClient


def test_bdsup_archive_bare_number_is_episode() -> None:
    parsed = parse_anime_filename("Rakudai Kishi no Cavalry 02 BDSUP.7z")
    assert parsed.episode == 2
    assert parsed.title == "Rakudai Kishi no Cavalry"


def test_bdsup_archive_episode_inference_handles_supported_archives() -> None:
    assert parse_anime_filename("Series 01 BDSUP.zip").episode == 1
    assert parse_anime_filename("Series 12 BD SUP.rar").episode == 12


def test_bdsup_episode_inference_is_narrow() -> None:
    assert parse_anime_filename("Series 02 BDSUP.mkv").episode is None
    assert parse_anime_filename("Series 2025 BDSUP.7z").episode is None
    assert parse_anime_filename("Series 1080p.7z").episode is None


def test_rakudai_bdsup_archive_ranks_as_exact_episode() -> None:
    client = JimakuClient.__new__(JimakuClient)
    item = JimakuFile(
        url="https://example.test/Rakudai%2002%20BDSUP.7z",
        name="Rakudai Kishi no Cavalry 02 BDSUP.7z",
        size=1,
        last_modified="",
    )
    ranked = client.rank_files(
        [item],
        VideoIdentity(title="Rakudai Kishi no Cavalry", episode=2),
        Path("[Sav1or&Lumen] Rakudai Kishi no Cavalry - S01E02 [BD][1080p].mkv"),
    )
    assert ranked[0].details["parsed_episode"] == 2
    assert ranked[0].details["episode_match"] == "exact"
    assert ranked[0].score >= 45.0


class _HistoryDB:
    def __init__(self, history: dict[str, object] | None) -> None:
        self.history = history
        self.ready_calls: list[tuple[Path, Path | None, int | None, str]] = []
        self.item = SimpleNamespace(subtitle_path=None, embedded_subtitle_id=None)

    def episode_by_path(self, video: Path):
        return self.item

    def latest_selected_subtitle(self, video: Path):
        return self.history

    def set_subtitle_ready(
        self,
        video: Path,
        subtitle: Path | None,
        embedded_id: int | None,
        *,
        origin: str = "",
    ) -> None:
        self.ready_calls.append((video, subtitle, embedded_id, origin))


def _manager(db: _HistoryDB, *, ocr_counts_as_ready: bool = False) -> AnimeManager:
    manager = AnimeManager.__new__(AnimeManager)
    manager.db = db
    manager.config = SimpleNamespace(
        matching=SimpleNamespace(ocr_counts_as_ready=ocr_counts_as_ready)
    )
    manager.logger = logging.getLogger("pudge-test-v112")
    return manager


def test_history_recovery_restores_prepared_final_path(tmp_path: Path) -> None:
    video = tmp_path / "Hyakkano - 32.mkv"
    video.write_bytes(b"video")
    final = tmp_path / "aligned.srt"
    final.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n", encoding="utf-8")
    db = _HistoryDB(
        {
            "source": "jimaku",
            "candidate_path": str(tmp_path / "raw-jimaku.srt"),
            "details": {
                "final_path": str(final),
                "generated_by_ocr": False,
                "quality": {"accepted": True},
            },
        }
    )

    recovered = _manager(db)._recover_selected_text_subtitle_from_history(
        video, media_id=200637, episode=8
    )

    assert recovered == final.resolve()
    assert db.ready_calls == [(video, final.resolve(), None, "jimaku")]


def test_history_recovery_accepts_pipeline_cache_prepared_path(tmp_path: Path) -> None:
    video = tmp_path / "Hyakkano - 32.mkv"
    video.write_bytes(b"video")
    prepared = tmp_path / "v14-cache.srt"
    prepared.write_text("cached", encoding="utf-8")
    db = _HistoryDB(
        {
            "source": "pipeline_cache",
            "candidate_path": str(prepared),
            "details": {"quality": {"accepted": True}},
        }
    )

    recovered = _manager(db)._recover_selected_text_subtitle_from_history(
        video, media_id=200637, episode=8
    )

    assert recovered == prepared.resolve()
    assert db.ready_calls[0][3] == "pipeline_cache"


def test_history_recovery_does_not_restore_raw_jimaku_candidate(tmp_path: Path) -> None:
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    raw = tmp_path / "raw.srt"
    raw.write_text("raw", encoding="utf-8")
    db = _HistoryDB(
        {
            "source": "jimaku",
            "candidate_path": str(raw),
            "details": {"quality": {"accepted": True}},
        }
    )

    assert _manager(db)._recover_selected_text_subtitle_from_history(
        video, media_id=1, episode=1
    ) is None
    assert db.ready_calls == []


def test_history_recovery_respects_ocr_not_ready_policy(tmp_path: Path) -> None:
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"video")
    final = tmp_path / "ocr.srt"
    final.write_text("ocr", encoding="utf-8")
    db = _HistoryDB(
        {
            "source": "jimaku",
            "details": {
                "final_path": str(final),
                "generated_by_ocr": True,
                "quality": {"accepted": True},
            },
        }
    )

    assert _manager(db)._recover_selected_text_subtitle_from_history(
        video, media_id=178788, episode=None
    ) is None
    assert db.ready_calls == []


def test_history_recovery_ignores_missing_or_rejected_prepared_path(tmp_path: Path) -> None:
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    missing = tmp_path / "missing.srt"
    db = _HistoryDB(
        {
            "source": "jimaku",
            "details": {
                "final_path": str(missing),
                "quality": {"accepted": True},
            },
        }
    )
    assert _manager(db)._recover_selected_text_subtitle_from_history(
        video, media_id=1, episode=1
    ) is None

    present = tmp_path / "rejected.srt"
    present.write_text("bad", encoding="utf-8")
    db.history = {
        "source": "jimaku",
        "details": {
            "final_path": str(present),
            "quality": {"accepted": False},
        },
    }
    assert _manager(db)._recover_selected_text_subtitle_from_history(
        video, media_id=1, episode=1
    ) is None
    assert db.ready_calls == []
