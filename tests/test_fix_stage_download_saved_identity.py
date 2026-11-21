"""A completed single-episode split-stage download (absolute S06E07 = 2nd STAGE episode 6)
kept "Episode record missing" after restart: its downloads row carried a persisted
``_identity_conflict`` (written once while the offline numbering was unknown), which
made every reconcile skip it, and with no offset cache / relation graph the offline
numbering stays unknown. Row shapes mirror the real downloads/episodes rows; names
are invented."""

from __future__ import annotations

from pathlib import Path

import pytest

from pudge import episode_numbering as en
from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode

MEDIA = 210482  # carries the vetted (toonshub, s6) continuous-stage scope
HASH = "ab" * 20
TITLE = "Invented Saga: Long Road - 2nd & 3rd STAGE"
TORRENT = (
    "[ToonsHub] Bizarre Saga S06E07 1080p WEB-DL DUAL AAC2.0 H.264 (Invented Saga: Long Road, Multi-Subs)"
)
FILE = "Bizarre.Saga.S06E07.Part.1.1080p.WEB-DL.DUAL.AAC2.0.H.264.MSubs-ToonsHub.mkv"
CONFLICT = "Saved release identity contradicts current numbering"


@pytest.fixture(autouse=True)
def _no_probe(monkeypatch):
    monkeypatch.setattr("pudge.manager.japanese_subtitle_source", lambda *_a, **_k: ("embedded", None))
    monkeypatch.setattr("pudge.manager.japanese_subtitle_details", lambda *_a, **_k: ("embedded", None, 20))


def _numbering(mapped: int = 6, raw: int = 7) -> dict:
    return {
        "mapped_media_episode": mapped,
        "raw_release_episode": raw,
        "numbering_status": "resolved",
        "numbering_scheme": "continuous-stage:invented saga long road:toonshub:s6",
        "numbering_rule": "continuous-stage",
        "numbering_revision": 1234,
        "group": "ToonsHub",
    }


def _setup(tmp_path: Path, *, saved: dict | None = None):
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.enabled = False
    manager = AnimeManager(cfg, log=lambda _m: None)
    anime = LibraryAnime(
        MEDIA, TITLE, titles=[TITLE], episodes=11, format="ONA", season_year=2026, status="CURRENT"
    )
    manager.db.upsert_anime(anime)  # no relation graph stored, no offset cache
    folder = cfg.library.root_dir / "Invented Saga_ Long Road - 2nd & 3rd STAGE"
    folder.mkdir(parents=True)
    video = (folder / FILE).resolve()
    video.write_bytes(b"video")
    raw = {
        "backend": "aria2",
        "gid": "0123456789abcdef",
        "infohash_v1": HASH,
        "orphaned_metadata": True,
        "video_episodes": [7],
        "single_video_file": False,
        "_media_id_source": "aria2_metadata_orphan",
        "_anime_title_tag": TITLE,
        "_release_numbering": saved if saved is not None else _numbering(),
        "_identity_conflict": CONFLICT,  # persisted by an earlier pass
    }
    manager.db.upsert_download(
        DownloadItem(
            torrent_hash=HASH,
            name=TORRENT,
            state="complete",
            progress=1.0,
            save_path=str(folder),
            content_path=str(video),
            media_id=MEDIA,
            episode=6,
            media_episode=6,
            release_episode=7,
            is_batch=False,
            added_on=100,
            completed_on=200,
            raw=raw,
        )
    )
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=MEDIA,
            title=TITLE,
            episode=None,
            media_episode=None,
            release_episode=7,
            video_path=video,
            embedded_subtitle_id=20,
            subtitle_origin="embedded",
            state="ready",
            torrent_hash=HASH,
        )
    )
    return manager, anime, video


def _cache(manager, anime, offset: int) -> None:
    en._write_caches(
        manager.config,
        anime,
        en.EpisodeNumbering(
            media_episode=1,
            release_episode=1 + offset,
            offset=offset,
            aliases=(1 + offset,),
            prequel_titles=(),
            chain=(1, MEDIA),
            source="relation_graph",
            rule="continuous-stage",
            alias_offsets=(offset,),
        ),
    )


def test_persisted_conflict_flag_is_recomputed_not_sticky(tmp_path):
    manager, anime, video = _setup(tmp_path)
    _cache(manager, anime, 1)  # numbering known again (offset 1)
    item = manager.db.download_by_hash(HASH)
    assert item.raw.get("_identity_conflict") == CONFLICT
    manager._resolve_download_media(item)
    assert "_identity_conflict" not in item.raw
    manager.reconcile_completed_download_rows()
    row = manager.db.episode_by_path(video)
    assert (row.media_id, row.media_episode, row.episode, row.release_episode) == (MEDIA, 6, 6, 7)


def test_unknown_offline_numbering_keeps_saved_resolved_identity(tmp_path):
    manager, _anime, video = _setup(tmp_path)  # no cache, no graph: numbering unknown
    manager.reconcile_completed_download_rows()
    row = manager.db.episode_by_path(video)
    assert (row.media_episode, row.episode, row.release_episode) == (6, 6, 7)
    assert manager.reconcile_completed_download_rows() == 0  # stable


def test_saved_numbering_for_another_episode_is_not_borrowed(tmp_path):
    manager, _anime, video = _setup(tmp_path, saved=_numbering(mapped=5))
    item = manager.db.download_by_hash(HASH)
    manager._resolve_download_media(item)
    assert item.raw.get("_identity_conflict") == CONFLICT
    manager.reconcile_completed_download_rows()
    assert manager.db.episode_by_path(video).media_episode is None


def test_current_numbering_contradicting_saved_identity_still_wins(tmp_path):
    manager, anime, video = _setup(tmp_path)
    _cache(manager, anime, 2)  # today release 7 = local 5, not the saved 6
    item = manager.db.download_by_hash(HASH)
    manager._resolve_download_media(item)
    assert item.raw.get("_identity_conflict") == CONFLICT
    manager.reconcile_completed_download_rows()
    assert manager.db.episode_by_path(video).media_episode is None
