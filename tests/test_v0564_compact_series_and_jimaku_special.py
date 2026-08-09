from __future__ import annotations

from pathlib import Path

from anime_mpv.cli import _find_online_subtitles
from anime_mpv.config import AppConfig, write_config
from anime_mpv.manager_models import LibraryAnime, LibraryEpisode
from anime_mpv.models import AniListAnime, JimakuEntry, JimakuFile, SubtitleCandidate, VideoIdentity
from anime_mpv.web_app import WebAppApi


ROOT = Path(__file__).parents[1]
HTML = ROOT / "anime_mpv" / "web" / "index.html"


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


def test_monogatari_sequence_gets_franchise_title_and_compact_card(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    entries = [
        (1, "Bakemonogatari", 2),
        (2, "Nisemonogatari", 3),
        (3, "Kizumonogatari", 1),
    ]
    for media_id, title, related_id in entries:
        api.manager.db.upsert_anime(
            LibraryAnime(
                media_id=media_id,
                title=title,
                status="PLANNING",
                media_status="FINISHED",
                episodes=1,
                start_date=f"20{media_id:02d}-01-01",
                relations=[relation(related_id, f"Related {related_id}")],
            )
        )
        video = tmp_path / f"{title}.mkv"
        video.write_bytes(b"video")
        api.manager.db.upsert_episode(
            LibraryEpisode(
                media_id=media_id,
                title=title,
                episode=1,
                video_path=video,
                state="ready",
            )
        )

    group = api.get_state()["home"]["completed_ready"][0]
    assert group["kind"] == "watch_sequence"
    assert group["title"] == "Monogatari Series"

    html = HTML.read_text(encoding="utf-8")
    assert ".ready-sequence-card { position:relative;" in html
    assert "grid-column:1/-1" not in html
    assert "group.title||items[0].title" in html
    assert "ready-sequence-list" in html
    assert "t('label.watchSequence')" not in html


def test_waiting_card_never_prints_episode_null() -> None:
    html = HTML.read_text(encoding="utf-8")
    assert "validEpisodeNumber" in html
    assert "episode!==null?t('label.episodeNotReady'" in html
    assert "t('label.notReady')" in html
    waiting_source = html[html.index('function waitingHomeCard'):html.index('function downloadAvailableHomeCard')]
    assert "episode:a.next_episode" not in waiting_source


def test_exact_title_single_special_is_accepted_without_jimaku_anilist_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = AppConfig()
    cfg.jimaku.api_key = "token"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.matching.jimaku_min_score = 45.0
    video = tmp_path / "Boku no Hero Academia - I am a hero too.mkv"
    video.write_bytes(b"video")
    subtitle = tmp_path / "subtitle.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nエリ\n", encoding="utf-8")

    entry = JimakuEntry(
        id=12479,
        name="Boku no Hero Academia: I am a hero too",
        english_name="My Hero Academia: I am a hero too",
        japanese_name="僕のヒーローアカデミア I am a hero too",
        anilist_id=None,
        flags={},
    )
    file = JimakuFile(
        url="https://jimaku.cc/api/files/one",
        name="subtitle.srt",
        size=100,
        last_modified="2026-08-04T00:00:00+00:00",
    )

    def fake_search(self, *, anilist_id=None, query=None):
        return [] if anilist_id is not None else [entry]

    monkeypatch.setattr("anime_mpv.cli.JimakuClient.search_entries", fake_search)
    monkeypatch.setattr(
        "anime_mpv.cli.JimakuClient.files_for_episode",
        lambda self, entry_id, episode, alternative_episodes=(): [file],
    )
    monkeypatch.setattr("anime_mpv.cli.JimakuClient.close", lambda self: None)

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

    monkeypatch.setattr("anime_mpv.cli.materialize_jimaku_files", fake_materialize)

    candidates = _find_online_subtitles(
        video,
        VideoIdentity(title="Boku no Hero Academia: I am a hero too"),
        cfg,
        None,
        False,
        anime_hint=AniListAnime(
            id=211711,
            titles=["Boku no Hero Academia: I am a hero too"],
            synonyms=["My Hero Academia: I am a hero too"],
            season_year=2026,
            episodes=1,
            format="SPECIAL",
        ),
        skip_airing_lookup=True,
    )

    assert candidates
    assert candidates[0].details["entry_id"] == 12479
    assert candidates[0].details["entry_anilist_match"] is True
    assert candidates[0].details["single_special_exact_title_match"] is True
    assert candidates[0].details["episode_match"] == "exact"
