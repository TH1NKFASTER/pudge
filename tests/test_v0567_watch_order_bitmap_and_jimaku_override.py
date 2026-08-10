from __future__ import annotations

from pathlib import Path

from pudge.cli import _find_online_subtitles
from pudge.config import AppConfig, write_config
from pudge.database import Database
from pudge.library import scan_library
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.models import (
    AniListAnime,
    EmbeddedSubtitle,
    JimakuEntry,
    JimakuFile,
    SubtitleCandidate,
    VideoIdentity,
)
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


def test_watch_order_rows_use_immediate_tooltip_and_hide_order_mode() -> None:
    html = HTML.read_text(encoding="utf-8")
    source = html[html.index("function readySequenceCard"):html.index("function readyHomeRenderer")]

    assert 'class="ready-sequence-row-title" data-tooltip="${escapeHtml(a.title)}"' in source
    assert "const mode=" not in source
    assert "label.releaseOrder" not in source
    assert "label.anilistOrder" not in source
    assert "<span>${mode}</span>" not in source


def test_bitmap_embedded_subtitle_is_waiting_for_text_and_kept_for_library(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(LibraryAnime(media_id=77, title="Bitmap Anime", status="CURRENT"))
    root = tmp_path / "library"
    root.mkdir()
    video = root / "Bitmap Anime - 01.mkv"
    video.write_bytes(b"video")

    monkeypatch.setattr(
        "pudge.library.find_embedded_japanese_subtitles",
        lambda *args, **kwargs: [
            EmbeddedSubtitle(
                stream_index=3,
                subtitle_id=2,
                codec="hdmv_pgs_subtitle",
                language="ja",
                score=120.0,
            )
        ],
    )

    items = scan_library(root, db, ffprobe="ffprobe", ffmpeg="ffmpeg")

    assert len(items) == 1
    assert items[0].state == "waiting_text_subtitles"
    assert items[0].embedded_subtitle_id == 2
    stored = db.episode_by_path(video.resolve())
    assert stored is not None
    assert stored.state == "waiting_text_subtitles"
    assert stored.embedded_subtitle_id == 2
    assert len(db.subtitle_jobs()) == 1


def test_bitmap_subtitle_is_not_on_ready_home_and_only_library_can_enable_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=88,
            title="Bitmap Anime",
            status="PLANNING",
            media_status="FINISHED",
            episodes=1,
        )
    )
    normal_video = tmp_path / "normal.mkv"
    library_video = tmp_path / "library.mkv"
    normal_video.write_bytes(b"video")
    library_video.write_bytes(b"video")
    for video in (normal_video, library_video):
        api.manager.db.upsert_episode(
            LibraryEpisode(
                media_id=88,
                title="Bitmap Anime",
                episode=1,
                video_path=video,
                embedded_subtitle_id=2,
                state="waiting_text_subtitles",
            )
        )

    state = api.get_state()
    assert state["home"]["completed_ready"] == []
    assert state["home"]["waiting"][0]["local"]["state"] == "waiting_text_subtitles"
    library_episode = state["library"][0]["episodes"][0]
    assert library_episode["subtitle_source"] == "image"

    class FakeProcess:
        pid = 123

        def poll(self):
            return None

    commands: list[list[str]] = []
    monkeypatch.setattr(
        "pudge.web_app.subprocess.Popen",
        lambda command, **kwargs: commands.append(command) or FakeProcess(),
    )

    api.play(str(normal_video), allow_image_subtitles=False)
    api.play(str(library_video), allow_image_subtitles=True)

    assert "--embedded-sid" not in commands[0]
    assert commands[1][commands[1].index("--embedded-sid") + 1] == "2"


def test_jimaku_entry_12479_uses_normal_exact_anilist_mapping(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = AppConfig()
    cfg.jimaku.api_key = "token"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.matching.jimaku_min_score = 70.0
    video = tmp_path / "Boku no Hero Academia - I am a hero too.mkv"
    video.write_bytes(b"video")
    subtitle = tmp_path / "hero.srt"
    subtitle.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nエリちゃん\n",
        encoding="utf-8",
    )
    entry = JimakuEntry(
        id=12479,
        name="Boku no Hero Academia: I am a hero too",
        english_name="My Hero Academia: I am a hero too",
        japanese_name="僕のヒーローアカデミア I am a hero too",
        anilist_id=211711,
        flags={},
    )
    jimaku_file = JimakuFile(
        url="https://jimaku.cc/entry/12479/download/file.srt",
        name="僕のヒーローアカデミア.SP.I.am.a.hero.too.WEBRip.DMMTV.ja[cc].srt",
        size=6008,
        last_modified="2026-08-04T02:41:05+00:00",
    )

    monkeypatch.setattr(
        "pudge.cli.JimakuClient.search_entries",
        lambda self, *, anilist_id=None, query=None: [entry]
        if anilist_id == 211711 or "I am a hero too" in str(query or "")
        else [],
    )
    monkeypatch.setattr(
        "pudge.cli.JimakuClient.files_for_episode",
        lambda self, entry_id, episode, alternative_episodes=(): [jimaku_file]
        if entry_id == 12479
        else [],
    )
    monkeypatch.setattr("pudge.cli.JimakuClient.close", lambda self: None)

    def fake_materialize(client, item, identity, video, cache_dir, **kwargs):
        return [
            SubtitleCandidate(
                path=subtitle,
                source="jimaku",
                score=item.score,
                name=item.name,
                verified_japanese=True,
                details=dict(item.details),
            )
        ]

    monkeypatch.setattr("pudge.cli.materialize_jimaku_files", fake_materialize)

    candidates = _find_online_subtitles(
        video,
        VideoIdentity(title="Boku no Hero Academia: I am a hero too", episode=1),
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
    assert candidates[0].details["single_special_exact_entry"] is True
    assert "KNOWN_JIMAKU_ENTRY_OVERRIDES" not in (
        ROOT / "pudge" / "cli.py"
    ).read_text(encoding="utf-8")
