from pathlib import Path

from pudge.anilist_tracking import (
    TrackingPayload,
    create_tracking_file,
    load_mapping,
    mapping_key,
    parse_anilist_id,
    read_tracking_file,
    save_mapping,
)
from pudge.models import AniListAnime, VideoIdentity


def test_tracking_payload_roundtrip_and_permissions(tmp_path: Path):
    payload = TrackingPayload(
        video="/tmp/episode.mkv",
        title="Odd Taxi",
        media_id=46102,
        episode=1,
        total_episodes=13,
        threshold=5 / 6,
        mapping_key="abc",
    )
    path = create_tracking_file(tmp_path, payload)
    loaded = read_tracking_file(path)

    assert loaded == payload
    assert path.stat().st_mode & 0o777 == 0o600


def test_corrected_mapping_survives_cache_read(tmp_path: Path):
    anime = AniListAnime(
        id=46102,
        titles=["Odd Taxi"],
        synonyms=["ODDTAXI"],
        season_year=2021,
        episodes=13,
        format="TV",
        score=99.0,
    )
    save_mapping(tmp_path, "key", anime, corrected=True)

    assert load_mapping(tmp_path, "key") == anime


def test_mapping_key_ignores_episode_number(tmp_path: Path):
    video1 = tmp_path / "Odd Taxi - 01.mkv"
    video2 = tmp_path / "Odd Taxi - 02.mkv"
    identity1 = VideoIdentity(title="Odd Taxi", episode=1)
    identity2 = VideoIdentity(title="Odd Taxi", episode=2)

    assert mapping_key(video1, identity1) == mapping_key(video2, identity2)


def test_parse_anilist_id_accepts_id_and_url():
    assert parse_anilist_id("46102") == 46102
    assert parse_anilist_id("https://anilist.co/anime/46102/Odd-Taxi/") == 46102
