"""A completed single-episode download of a later split stage ("S06E07" = absolute 7 =
2nd STAGE episode 6) whose file name drops the storyline title and the bracketed
uploader must still be linked to the episode it was downloaded for; rows already
left without a media episode are repaired by the next reconcile."""

from __future__ import annotations

from pathlib import Path

import pytest

from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode
from test_v0730_acquisition_safety import manager_for
from test_v0730_release_identity import stage_fixture

FILE = "Bizarre.Show.S06E07.Part.1.1080p.WEB-DL.AAC2.0.H.264.MSubs-ToonsHub.mkv"
TORRENT = (
    "[ToonsHub] Bizarre Show S06E07 1080p WEB-DL AAC2.0 H.264 "
    "(JoJo no Kimyou na Bouken: Steel Ball Run, Multi-Subs)"
)


@pytest.fixture(autouse=True)
def _no_probe(monkeypatch):
    monkeypatch.setattr("pudge.manager.japanese_subtitle_source", lambda *_a, **_k: ("none", None))
    monkeypatch.setattr("pudge.manager.japanese_subtitle_details", lambda *_a, **_k: ("none", None, None))


def _setup(
    tmp_path: Path,
    *,
    torrent: str = TORRENT,
    file: str = FILE,
    episode: int = 6,
    release: int = 7,
    media=None,
):
    manager = manager_for(tmp_path)
    anime, _graph = stage_fixture(manager)
    if media is not None:
        anime = media
        manager.db.upsert_anime(anime)
    folder = manager.config.library.root_dir / "Stage Show"
    folder.mkdir(parents=True)
    video = folder / file
    video.write_bytes(b"video")
    item = DownloadItem(
        torrent_hash="fd" * 20,
        name=torrent,
        state="complete",
        progress=1.0,
        save_path=str(folder),
        content_path=str(video.resolve()),
        media_id=anime.media_id,
        episode=episode,
        media_episode=episode,
        release_episode=release,
        completed_on=10,
    )
    manager.db.upsert_download(item)
    return manager, anime, item, video.resolve()


def test_completed_stage_download_file_gets_its_requested_episode(tmp_path):
    manager, anime, item, video = _setup(tmp_path)
    manager._register_completed_download(manager.db.download_by_hash(item.torrent_hash))
    row = manager.db.episode_by_path(video)
    assert row is not None
    assert (row.media_id, row.media_episode, row.episode, row.release_episode) == (anime.media_id, 6, 6, 7)


def test_existing_row_without_media_episode_is_repaired_by_reconcile(tmp_path):
    manager, anime, item, video = _setup(tmp_path)
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=anime.media_id,
            title=anime.title,
            episode=None,
            media_episode=None,
            release_episode=7,
            video_path=video,
            state="ready",
            torrent_hash=item.torrent_hash,
        )
    )
    manager.reconcile_completed_download_rows()
    row = manager.db.episode_by_path(video)
    assert (row.media_episode, row.episode, row.release_episode) == (6, 6, 7)
    # Stable afterwards: a second pass changes nothing.
    assert manager.reconcile_completed_download_rows() == 0


@pytest.mark.parametrize(
    "torrent,file,episode",
    [
        # torrent name of another storyline: no evidence to borrow
        ("[ToonsHub] Bizarre Show S06E07 1080p (Unrelated Story)", FILE, 6),
        # file numbered differently from its torrent name
        (TORRENT.replace("S06E07", "S06E08"), FILE, 6),
        # torrent name maps to another local episode than the one requested
        (TORRENT, FILE, 5),
    ],
)
def test_torrent_name_is_used_only_when_it_proves_the_requested_episode(tmp_path, torrent, file, episode):
    manager, _anime, item, video = _setup(tmp_path, torrent=torrent, file=file, episode=episode)
    manager._register_completed_download(manager.db.download_by_hash(item.torrent_hash))
    row = manager.db.episode_by_path(video)
    assert row is None or row.media_episode is None


def test_learned_rule_of_another_stage_title_applies_through_torrent_name(tmp_path):
    media = LibraryAnime(
        880001,
        "Invented Saga: Long Road - 2nd STAGE",
        titles=["Invented Saga: Long Road"],
        episodes=10,
        format="ONA",
        season_year=2026,
    )
    manager, anime, item, video = _setup(
        tmp_path,
        torrent="[SomeGroup] Invented Saga S02E05 1080p (Invented Saga: Long Road)",
        file="Invented.Saga.S02E05.1080p.WEB-DL-SomeGroup.mkv",
        episode=3,
        release=5,
        media=media,
    )
    manager.db.store_release_numbering_rule(anime.media_id, "SomeGroup", "s2", 2)
    manager._register_completed_download(manager.db.download_by_hash(item.torrent_hash))
    row = manager.db.episode_by_path(video)
    assert (row.media_episode, row.release_episode) == (3, 5)
