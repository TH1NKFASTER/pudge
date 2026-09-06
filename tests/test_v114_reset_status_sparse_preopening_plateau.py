from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

from pudge.database import Database
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.subtitle_formats import write_srt
from pudge.syncing import _refine_sparse_preopening_cues
from pudge.web_app import WebAppApi


def _api_for_database(db: Database) -> WebAppApi:
    api = WebAppApi.__new__(WebAppApi)
    api.manager = SimpleNamespace(db=db)
    api.logger = logging.getLogger("test-v114")
    api.get_state = lambda: {}  # type: ignore[method-assign]
    return api


def test_reset_progress_does_not_create_watching_status_for_new_anime(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "library.sqlite3")
    video = tmp_path / "New Anime - 01.mkv"
    video.write_bytes(b"video")
    db.upsert_anime(
        LibraryAnime(
            media_id=99114,
            title="New Anime",
            status="",
            progress=0,
        )
    )
    db.upsert_episode(
        LibraryEpisode(99114, "New Anime", 1, video, state="local")
    )
    db.record_playback(video, 120.0, 1400.0, active_seconds=60.0)

    api = _api_for_database(db)

    def unexpected_client():
        raise AssertionError("AniList must not be mutated for an anime with no list status")

    api._anilist_client = unexpected_client  # type: ignore[method-assign]
    result = api.reset_anime_progress(99114)

    assert result["progress"] == 0
    assert db.get_anime(99114).status == ""
    episode = db.episode_by_path(video)
    assert episode is not None
    assert episode.playback_position is None
    assert episode.playback_active_seconds == 0


def test_reset_progress_preserves_planning_status(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(
        LibraryAnime(
            media_id=99115,
            title="Planned Anime",
            status="PLANNING",
            progress=0,
        )
    )
    calls: list[tuple[int, int, str]] = []

    class Client:
        def set_progress(self, media_id: int, progress: int, status: str):
            calls.append((media_id, progress, status))
            return {"progress": progress, "status": status}

        def close(self) -> None:
            pass

    api = _api_for_database(db)
    api._anilist_client = lambda: Client()  # type: ignore[method-assign]
    api.reset_anime_progress(99115)

    assert calls == [(99115, 0, "PLANNING")]
    assert db.get_anime(99115).status == "PLANNING"


def _write_reference(path: Path, starts: list[float]) -> None:
    write_srt(
        [
            (start, start + 0.8, f"line {index}")
            for index, start in enumerate(starts, start=1)
        ],
        path,
    )


def test_sparse_preopening_uses_first_cue_then_second_plateau(
    tmp_path: Path,
) -> None:
    # The coarse scaffold left every pre-opening cue on one +9.3s clock.
    # Two independently confirmed speech cues show residuals -0.8s and +0.5s.
    cues = [
        (10.3, 11.1, "なんじゃ"),
        (20.3, 21.1, "やれやれ"),
        (30.3, 31.1, "pre-opening sign"),
        (40.3, 41.1, "pre-opening sign 2"),
        (150.0, 151.0, "post-opening dialogue"),
    ]
    embedded = tmp_path / "embedded.srt"
    speech = tmp_path / "speech.srt"
    _write_reference(embedded, [9.5, 20.8, 150.0])
    _write_reference(speech, [9.5, 20.8, 150.0])

    refined, result = _refine_sparse_preopening_cues(
        cues,
        4,
        {"timing_reference": str(speech)},
        embedded,
    )

    assert result["accepted"] is True
    assert result["reason"] == "dual_reference_sparse_two_plateaus"
    assert result["first_cue_shift_seconds"] == -0.8
    assert result["later_plateau_shift_seconds"] == 0.5
    assert [round(row[0], 3) for row in refined] == [9.5, 20.8, 30.8, 40.8, 150.0]
    assert refined[-1] == cues[-1]
