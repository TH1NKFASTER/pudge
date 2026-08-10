from __future__ import annotations

from pathlib import Path

from pudge.cli import _find_online_subtitles
from pudge.config import AppConfig, write_config
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode
from pudge.models import AniListAnime, JimakuEntry, JimakuFile, SubtitleCandidate, VideoIdentity
from pudge.web_app import WebAppApi


ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def relation(media_id: int, title: str) -> dict:
    return {
        "relation_type": "SIDE_STORY",
        "media_id": media_id,
        "title": title,
        "episodes": 1,
        "relations": [],
    }


def test_sequence_list_only_opens_from_entry_count_and_cover_is_polychrome() -> None:
    html = HTML.read_text(encoding="utf-8")

    assert ".ready-sequence-count-wrap:hover + .ready-sequence-list" in html
    assert ".ready-sequence-card:hover .ready-sequence-list" not in html
    assert "ready-sequence-cover cover-shell polychrome" in html
    assert 'class="ready-sequence-card" data-media-id=' in html
    assert 'data-action="play" data-path="${escapeHtml(leadPath)}"' in html


def test_sequence_rows_keep_titles_when_play_state_changes() -> None:
    html = HTML.read_text(encoding="utf-8")
    state_source = html[html.index("function setPathPlayState"):html.index("function applyKnownPlayStates")]

    assert "el.tagName==='BUTTON'&&el.classList.contains('play-button')" in state_source
    assert "if(el.tagName==='BUTTON')el.disabled=state!=='idle'" in state_source
    assert "ready-sequence-row" not in state_source


def test_single_ready_related_entry_is_not_wrapped_in_watch_order(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    first = LibraryAnime(
        media_id=1,
        title="Example Movie 1",
        episodes=1,
        start_date="2020-01-01",
        relations=[relation(2, "Example Movie 2")],
    )
    second = LibraryAnime(
        media_id=2,
        title="Example Movie 2",
        episodes=1,
        start_date="2021-01-01",
        relations=[relation(1, "Example Movie 1")],
    )
    api.manager.db.upsert_anime(first)
    api.manager.db.upsert_anime(second)

    video = tmp_path / "second.mkv"
    video.write_bytes(b"video")
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=2,
            title=second.title,
            episode=1,
            video_path=video,
            state="ready",
        )
    )

    completed = api.get_state()["home"]["completed_ready"]
    assert len(completed) == 1
    assert completed[0].get("kind") != "watch_sequence"
    assert completed[0]["media_id"] == 2


def test_exact_anilist_link_accepts_single_special_with_generic_file_name(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = AppConfig()
    cfg.jimaku.api_key = "token"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.matching.jimaku_min_score = 70.0
    video = tmp_path / "Boku no Hero Academia - I am a hero too.mkv"
    video.write_bytes(b"video")
    subtitle = tmp_path / "subtitle.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nエリ\n", encoding="utf-8")

    entry = JimakuEntry(
        id=12479,
        name="A generic special entry title",
        english_name=None,
        japanese_name=None,
        anilist_id=211711,
        flags={},
    )
    file = JimakuFile(
        url="https://jimaku.cc/api/files/one",
        name="僕のヒーローアカデミア.SP.I.am.a.hero.too.WEBRip.DMMTV.ja[cc].srt",
        size=100,
        last_modified="2026-08-04T00:00:00+00:00",
    )

    monkeypatch.setattr(
        "pudge.cli.JimakuClient.search_entries",
        lambda self, *, anilist_id=None, query=None: [entry] if anilist_id else [],
    )
    monkeypatch.setattr(
        "pudge.cli.JimakuClient.files_for_episode",
        lambda self, entry_id, episode, alternative_episodes=(): [file],
    )
    monkeypatch.setattr("pudge.cli.JimakuClient.close", lambda self: None)

    def fake_materialize(client, item, identity, video, cache_dir, **kwargs):
        return [
            SubtitleCandidate(
                subtitle,
                "jimaku",
                item.score,
                item.name,
                details=dict(item.details),
            )
        ]

    monkeypatch.setattr("pudge.cli.materialize_jimaku_files", fake_materialize)

    candidates = _find_online_subtitles(
        video,
        VideoIdentity(title="Boku no Hero Academia: I am a hero too"),
        cfg,
        None,
        False,
        anime_hint=AniListAnime(
            id=211711,
            titles=["Boku no Hero Academia: I am a hero too"],
            synonyms=[],
            season_year=2026,
            episodes=1,
            format="SPECIAL",
        ),
        skip_airing_lookup=True,
    )

    assert candidates
    assert candidates[0].details["entry_id"] == 12479
    assert candidates[0].details["entry_anilist_match"] is True


def test_fuzzy_zero_progress_torrent_does_not_block_new_download(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    manager = AnimeManager(cfg, log=lambda _message: None)
    anime = LibraryAnime(media_id=42, title="Example Anime", episodes=12, format="TV")
    manager.db.upsert_anime(anime)

    item = DownloadItem(
        torrent_hash="old",
        name="Example Animu random unrelated release",
        state="stalledDL",
        progress=0.0,
        save_path=str(tmp_path),
        content_path=str(tmp_path / "nothing"),
        media_id=42,
        episode=1,
        raw={"_media_id_source": "fuzzy_title", "_media_id_score": 83.0},
    )

    class Client:
        def torrents(self, *, category=""):
            return [item]

    assert manager._existing_download_for_request(
        Client(), anime, episode=1, batch=False, release_hash="new"
    ) is None


def test_missing_files_torrent_does_not_block_new_download(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    manager = AnimeManager(cfg, log=lambda _message: None)
    anime = LibraryAnime(media_id=42, title="Example Anime", episodes=1, format="SPECIAL")
    manager.db.upsert_anime(anime)

    item = DownloadItem(
        torrent_hash="old",
        name="Example Anime",
        state="missingFiles",
        progress=0.5,
        save_path=str(tmp_path),
        content_path=str(tmp_path / "missing"),
        media_id=42,
        episode=None,
        raw={"_media_id_source": "tag", "_tag_set": ["anilist: 42"]},
    )

    class Client:
        def torrents(self, *, category=""):
            return [item]

    assert manager._existing_download_for_request(
        Client(), anime, episode=None, batch=True, release_hash="new"
    ) is None
