from pathlib import Path

from anime_mpv import cli
from anime_mpv.config import AppConfig
from anime_mpv.models import AniListAnime, VideoIdentity


def _anime(*, episodes: int | None, media_format: str) -> AniListAnime:
    return AniListAnime(
        id=211711,
        titles=["Boku no Hero Academia: I am a hero too"],
        synonyms=[],
        season_year=2026,
        episodes=episodes,
        format=media_format,
    )


def test_one_entry_special_without_filename_episode_gets_progress_one() -> None:
    identity = VideoIdentity(title="Boku no Hero Academia: I am a hero too")
    assert cli._anilist_episode(identity, _anime(episodes=1, media_format="SPECIAL")) == 1


def test_multi_entry_special_without_filename_episode_remains_ambiguous() -> None:
    identity = VideoIdentity(title="Multi-part Special")
    assert cli._anilist_episode(identity, _anime(episodes=3, media_format="SPECIAL")) is None


def test_mpv_tracker_uses_ui_language_and_defaults_to_english() -> None:
    source = Path("anime_mpv/mpv_scripts/anime_mpv_anilist.lua").read_text(encoding="utf-8")
    assert "ANIME_MPV_UI_LANGUAGE" in source
    assert "or 'en'" in source
    assert "AniList: tracking is unavailable for this file" in source
    assert "AniList: трекер недоступен для этого файла" in source
    assert "AniList: manual mode — Ctrl+A to count" in source


def test_python_anilist_osd_follows_ui_language() -> None:
    config = AppConfig()
    config.ui.language = "en"
    assert cli._osd(config, "English", "Русский") == "English"
    config.ui.language = "ru"
    assert cli._osd(config, "English", "Русский") == "Русский"
