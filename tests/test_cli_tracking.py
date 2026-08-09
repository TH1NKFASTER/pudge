from anime_mpv.cli import _anilist_episode
from anime_mpv.models import AniListAnime, VideoIdentity


def _anime(*, episodes=12, format="TV"):
    return AniListAnime(
        id=1,
        titles=["Example"],
        synonyms=[],
        season_year=2026,
        episodes=episodes,
        format=format,
    )


def test_anilist_episode_uses_parsed_episode():
    assert _anilist_episode(VideoIdentity(title="Example", episode=5), _anime()) == 5


def test_anilist_episode_marks_movie_as_one():
    assert _anilist_episode(VideoIdentity(title="Movie"), _anime(episodes=1, format="MOVIE")) == 1


def test_anilist_episode_rejects_out_of_range_episode():
    assert _anilist_episode(VideoIdentity(title="Example", episode=13), _anime()) is None
