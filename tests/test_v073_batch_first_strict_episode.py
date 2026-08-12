from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.providers.nyaa import (
    _episode_size_quality_score,
    release_episode,
    score_release,
    search_ranked,
)
from pudge.web_app import WebAppApi


ANSATSU = LibraryAnime(
    media_id=20755,
    title="Ansatsu Kyoushitsu",
    titles=["Ansatsu Kyoushitsu", "Assassination Classroom"],
    episodes=22,
    format="TV",
)


def _release(
    title: str,
    *,
    info_hash: str = "hash",
    size_gib: float = 1.4,
    is_batch: bool = False,
    score: float = 0.0,
) -> NyaaRelease:
    return NyaaRelease(
        title=title,
        link="https://nyaa.si/view/1",
        torrent_url="https://nyaa.si/download/1.torrent",
        info_hash=info_hash,
        size_text=f"{size_gib:.1f} GiB",
        size_bytes=int(size_gib * 1024**3),
        seeders=100,
        leechers=1,
        downloads=1000,
        trusted=True,
        remake=False,
        is_batch=is_batch,
        group="SubsPlease",
        score=score,
    )


def _score(item: NyaaRelease, episode: int) -> NyaaRelease:
    return score_release(
        item,
        ANSATSU,
        episode=episode,
        batch=False,
        trusted_groups=["SubsPlease", "FFF", "hchcsen"],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024 * 1024,
        target_episode_max_bytes=3500 * 1024 * 1024,
    )


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.nyaa.min_release_score = -1000.0
    cfg.nyaa.min_seeders = 1
    return AnimeManager(cfg, log=lambda _message: None)


def test_volume_marker_is_not_treated_as_episode_number() -> None:
    assert release_episode(
        "[FFF] Assassination Classroom - Vol.04 [BD][1080p-FLAC]"
    ) is None


def test_s00_special_is_removed_before_episode_ranking() -> None:
    special = _release(
        "[hchcsen] Assassination Classroom S00E02 Episode 0 Meeting Time "
        "[BD 1080p x265 2xOPUS]"
    )

    class FakeClient:
        def search(self, _query: str) -> list[NyaaRelease]:
            return [special]

    ranked = search_ranked(
        FakeClient(),
        ANSATSU,
        episode=2,
        batch=False,
        trusted_groups=["hchcsen"],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024 * 1024,
        target_episode_max_bytes=3500 * 1024 * 1024,
        max_queries=1,
    )

    assert ranked == []


@pytest.mark.parametrize(
    ("episode", "title", "size_gib"),
    [
        (
            1,
            "[SubsPlease] Ansatsu Kyoushitsu Movie - Minna no Jikan "
            "(1080p) [2C7F205D]",
            2.0,
        ),
        (
            4,
            "[FFF] Assassination Classroom - Vol.04 [BD][1080p-FLAC]",
            8.0,
        ),
        (
            2,
            "[hchcsen] Assassination Classroom S00E02 Episode 0 Meeting Time "
            "[BD 1080p x265 2xOPUS]",
            2.0,
        ),
        (
            2,
            "[SubsPlease] Ansatsu Kyoushitsu Movie - Minna no Jikan "
            "(720p) [2F06A4F7]",
            1.2,
        ),
        (
            3,
            "Assassination.Classroom.365.Days.2016.1080p.BluRay."
            "10-Bit.Dual-Audio.TrueHD.x265-iAHD",
            5.0,
        ),
    ],
)
def test_bad_ansatsu_candidates_cannot_be_selected_as_episode(
    tmp_path: Path,
    episode: int,
    title: str,
    size_gib: float,
) -> None:
    manager = _manager(tmp_path)
    scored = _score(_release(title, size_gib=size_gib), episode)
    added: list[NyaaRelease] = []
    manager.search_releases = lambda *_args, **_kwargs: [scored]  # type: ignore[method-assign]
    manager.add_release = (  # type: ignore[method-assign]
        lambda _media_id, item, **_kwargs: added.append(item) or True
    )

    selected = manager.search_and_add_best(
        ANSATSU.media_id,
        episode=episode,
        batch=False,
        automatic=False,
    )

    assert selected is None
    assert added == []


def test_single_episode_size_has_no_unbounded_large_file_bonus() -> None:
    normal_score, normal_reasons = _episode_size_quality_score(
        int(1.2 * 1024**3), ANSATSU
    )
    huge_score, huge_reasons = _episode_size_quality_score(
        int(12 * 1024**3), ANSATSU
    )

    assert normal_score == 5.0
    assert huge_score == normal_score
    assert "size-floor-ok" in normal_reasons
    assert "size-floor-ok" in huge_reasons
    assert "high-bitrate-size" not in huge_reasons


def test_batch_first_requires_explicit_batch_identity(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    huge_movie = _release(
        "Ansatsu Kyoushitsu Movie - Minna no Jikan 1080p BluRay",
        info_hash="movie",
        size_gib=12.0,
        score=999.0,
        is_batch=False,
    )
    real_batch = _release(
        "[Group] Ansatsu Kyoushitsu 01-22 Batch 1080p",
        info_hash="batch",
        size_gib=20.0,
        score=500.0,
        is_batch=True,
    )
    manager.search_releases = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: [huge_movie, real_batch]
    )
    added: list[NyaaRelease] = []
    manager.add_release = (  # type: ignore[method-assign]
        lambda _media_id, item, **_kwargs: added.append(item) or True
    )

    selected = manager.search_and_add_best(
        ANSATSU.media_id,
        episode=None,
        batch=True,
        automatic=False,
        require_explicit_batch=True,
    )

    assert selected is real_batch
    assert added == [real_batch]


def _api_without_init() -> WebAppApi:
    api = object.__new__(WebAppApi)
    api.logger = SimpleNamespace(
        warning=lambda *_args, **_kwargs: None,
        exception=lambda *_args, **_kwargs: None,
    )
    api._planning_episode_download_lock = threading.Lock()
    api._planning_episode_download_state = {
        "status": "running",
        "running": True,
        "episodes": [],
    }
    api._planning_episode_cancel_event = threading.Event()
    api._planning_episode_job_id = ""
    api._planning_local_episodes = lambda *_args, **_kwargs: []
    return api


def test_planning_auto_download_tries_batch_before_episode_search() -> None:
    batch = _release(
        "[Group] Ansatsu Kyoushitsu 01-22 Batch 1080p",
        info_hash="batch",
        size_gib=20.0,
        is_batch=True,
    )
    calls: list[dict[str, object]] = []

    class Manager:
        @staticmethod
        def downloads_enabled() -> bool:
            return False

        @staticmethod
        def scan_library() -> list[object]:
            return []

        @staticmethod
        def search_and_add_best(_media_id: int, **kwargs):
            calls.append(dict(kwargs))
            if kwargs["batch"]:
                return batch
            raise AssertionError("episode fallback must not run after batch succeeds")

    api = _api_without_init()
    api._run_planning_episode_download(Manager(), ANSATSU, 22)

    assert len(calls) == 1
    assert calls[0]["episode"] is None
    assert calls[0]["batch"] is True
    assert calls[0]["automatic"] is False
    assert calls[0]["require_explicit_batch"] is True
    state = api._planning_episode_download_state
    assert state["status"] == "done"
    assert len(state["episodes"]) == 22
    assert {row["source"] for row in state["episodes"]} == {"batch"}


def test_planning_auto_download_falls_back_to_episodes_without_batch() -> None:
    calls: list[tuple[object, bool]] = []

    class Manager:
        @staticmethod
        def downloads_enabled() -> bool:
            return False

        @staticmethod
        def scan_library() -> list[object]:
            return []

        @staticmethod
        def search_and_add_best(_media_id: int, **kwargs):
            calls.append((kwargs["episode"], bool(kwargs["batch"])))
            if kwargs["batch"]:
                return None
            episode = int(kwargs["episode"])
            return _release(
                f"[Group] Ansatsu Kyoushitsu - {episode:02d} [1080p]",
                info_hash=f"ep-{episode}",
            )

    api = _api_without_init()
    api._run_planning_episode_download(Manager(), ANSATSU, 3)

    assert calls == [(None, True), (1, False), (2, False), (3, False)]
    assert api._planning_episode_download_state["status"] == "done"
