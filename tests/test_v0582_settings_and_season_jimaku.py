from __future__ import annotations

from pathlib import Path

from anime_mpv.cli import _find_online_subtitles
from anime_mpv.config import AppConfig
from anime_mpv.models import AniListAnime, JimakuEntry, VideoIdentity


def test_settings_renderer_declares_payload_before_reading_version() -> None:
    html = Path("anime_mpv/web/index.html").read_text(encoding="utf-8")
    start = html.index("function renderSettings(){")
    end = html.index("function fillSettings", start)
    function = html[start:end]

    declaration = "const s=ui.state.settings||{};"
    version_read = "escapeHtml(s.version||'—')"
    assert declaration in function
    assert version_read in function
    assert function.index(declaration) < function.index(version_read)


def test_explicit_season_keeps_exact_anilist_jimaku_entry(tmp_path: Path, monkeypatch) -> None:
    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.jimaku.api_key = "token"
    video = tmp_path / "Hyakkano S03E05.mkv"
    video.write_bytes(b"video")
    calls: list[tuple[int | None, str | None]] = []

    exact = JimakuEntry(
        id=12234,
        name="Kimi no Koto ga Dai Dai Dai Dai Daisuki na 100-nin no Kanojo 3rd Season",
        english_name="The 100 Girlfriends Who Really, Really, Really, Really, REALLY Love You Season 3",
        japanese_name="君のことが大大大大大好きな100人の彼女 第3期",
        anilist_id=200637,
        flags={},
    )

    class FakeJimaku:
        def __init__(self, *_args, **_kwargs):
            pass

        def search_entries(self, *, anilist_id=None, query=None):
            calls.append((anilist_id, query))
            if anilist_id == 200637:
                return [exact]
            raise AssertionError(f"name search must not run for explicit S03 exact ID: {query}")

        def rank_entries(self, entries, _identity, _anilist_id):
            return list(entries)

        def files_for_episode(self, _entry_id, _episode, alternative_episodes=()):
            return []

        def rank_files(self, files, *_args, **_kwargs):
            return files

        def close(self):
            pass

    monkeypatch.setattr("anime_mpv.cli.JimakuClient", FakeJimaku)
    anime = AniListAnime(
        id=200637,
        titles=[
            "Kimi no Koto ga Dai Dai Dai Dai Daisuki na 100-nin no Kanojo 3rd Season",
            "The 100 Girlfriends Who Really, Really, Really, Really, REALLY Love You Season 3",
        ],
        synonyms=["Hyakkano 3"],
        season_year=2026,
        episodes=12,
        format="TV",
    )

    result = _find_online_subtitles(
        video,
        VideoIdentity(
            title="The 100 Girlfriends Who Really Really Really Really REALLY Love You",
            season=3,
            episode=5,
        ),
        cfg,
        None,
        False,
        anime_hint=anime,
        skip_airing_lookup=True,
    )

    assert result == []
    assert calls == [(200637, None)]
