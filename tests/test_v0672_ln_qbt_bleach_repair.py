from pathlib import Path

import httpx

from anime_mpv import __version__
from anime_mpv.config import AppConfig
from anime_mpv.manager import AnimeManager
from anime_mpv.manager_models import DownloadItem, LibraryEpisode
from anime_mpv.providers.qbittorrent import QBittorrentClient
from anime_mpv.subtitle_formats import clean_srt_for_playback


def _srt(text: str = "日本語") -> str:
    return f"1\n00:00:01,000 --> 00:00:02,000\n{text}\n"


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "Movies" / "pudge"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    return cfg


def test_v0672_version() -> None:
    assert __version__ == "0.6.74"


def test_alignment_generation_uses_history_when_old_sync_source_was_pruned(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    manager = AnimeManager(cfg, log=lambda _message: None)

    raw = cfg.paths.cache_dir / "jimaku" / "bleach" / "candidate.srt"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(_srt("raw"), encoding="utf-8")

    aligned = cfg.paths.cache_dir / "synced" / "legacy-ffsubsync.srt"
    aligned.parent.mkdir(parents=True, exist_ok=True)
    aligned.write_text(_srt("aligned"), encoding="utf-8")

    stale_playback, _ = clean_srt_for_playback(aligned, cfg.paths.cache_dir)
    direct_playback, _ = clean_srt_for_playback(raw, cfg.paths.cache_dir)
    assert stale_playback.name != direct_playback.name

    video = cfg.library.root_dir / "Bleach.2004.S17E43.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=185874,
            title="BLEACH: Sennen Kessen-hen - Kashin-tan",
            episode=43,
            video_path=video,
            subtitle_path=stale_playback,
            subtitle_origin="jimaku",
            state="ready",
        )
    )
    manager.db.record_subtitle_history(
        video_path=video,
        media_id=185874,
        episode=43,
        source="jimaku",
        candidate_name=raw.name,
        candidate_path=raw,
        score=98.36,
        status="selected",
        reason="Preparation completed",
        details={"candidate_path": str(raw), "final_path": str(stale_playback)},
    )

    clean_video = cfg.library.root_dir / "Direct - 01.mkv"
    clean_video.write_bytes(b"video")
    clean_raw = cfg.paths.cache_dir / "jimaku" / "direct" / "candidate.srt"
    clean_raw.parent.mkdir(parents=True, exist_ok=True)
    clean_raw.write_text(_srt("direct"), encoding="utf-8")
    clean_playback, _ = clean_srt_for_playback(clean_raw, cfg.paths.cache_dir)
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=2,
            title="Direct",
            episode=1,
            video_path=clean_video,
            subtitle_path=clean_playback,
            subtitle_origin="jimaku",
            state="ready",
        )
    )
    manager.db.record_subtitle_history(
        video_path=clean_video,
        media_id=2,
        episode=1,
        source="jimaku",
        candidate_name=clean_raw.name,
        candidate_path=clean_raw,
        status="selected",
        reason="Preparation completed",
        details={"candidate_path": str(clean_raw), "final_path": str(clean_playback)},
    )

    manager.db.set_state("subtitle_validation_generation", "15")
    assert manager._requeue_legacy_generated_subtitles() == 1

    bleach = manager.db.episode_by_path(video)
    direct = manager.db.episode_by_path(clean_video)
    assert bleach is not None and bleach.subtitle_path is None
    assert bleach.state == "waiting_subtitles"
    assert direct is not None and direct.subtitle_path == clean_playback.resolve()
    assert direct.state == "ready"
    assert manager.db.get_state("subtitle_validation_generation", "") == "16"


def test_qbittorrent_can_set_location_and_recheck() -> None:
    seen: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        seen.append((request.url.path, body))
        return httpx.Response(200, text="Ok.")

    client = QBittorrentClient("http://qbt.local", api_key="key")
    client.client.close()
    client.client = httpx.Client(
        base_url="http://qbt.local",
        transport=httpx.MockTransport(handler),
    )
    client.set_location("abc", Path("/tmp/pudge/Bleach"))
    client.recheck("abc")
    client.close()

    assert [path for path, _body in seen] == [
        "/api/v2/torrents/setLocation",
        "/api/v2/torrents/recheck",
    ]
    assert b"hashes=abc" in seen[0][1]
    assert b"location=" in seen[0][1]
    assert b"hashes=abc" in seen[1][1]


def test_manager_repairs_missingfiles_after_brand_rename(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    manager = AnimeManager(cfg, log=lambda _message: None)
    target = cfg.library.root_dir / "BLEACH_ Sennen Kessen-hen - Kashin-tan"
    target.mkdir(parents=True)
    old = tmp_path / "Movies" / "Anime MPV" / target.name

    item = DownloadItem(
        torrent_hash="bleach",
        name="Bleach E43",
        state="missingFiles",
        progress=0.0,
        save_path=str(old),
        content_path=str(old / "Bleach.2004.S17E43.mkv"),
        raw={"category": "anime-mpv", "_tag_set": ["anime-mpv"]},
    )

    class FakeClient:
        def __init__(self) -> None:
            self.locations: list[tuple[str, Path]] = []
            self.rechecks: list[str] = []

        def set_location(self, torrent_hash: str, location: Path) -> None:
            self.locations.append((torrent_hash, location))

        def recheck(self, torrent_hash: str) -> None:
            self.rechecks.append(torrent_hash)

    client = FakeClient()
    assert manager._repair_legacy_qbittorrent_paths(client, [item]) == 1
    assert client.locations == [("bleach", target.resolve())]
    assert client.rechecks == ["bleach"]


def test_ln_cards_and_settings_match_v0672_ui() -> None:
    html = Path("anime_mpv/web/index.html").read_text(encoding="utf-8")

    assert ".ln-card-body{display:flex;min-width:0;flex-direction:column}" in html
    assert ".ln-card-actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:auto}" in html
    assert 'data-ln-anilist-url="${escapeHtml(anilistUrl)}"' in html
    assert "https://anilist.co/manga/${Number(book.anilist_id)}" in html
    assert "await pywebview.api.open_url(target.dataset.lnAnilistUrl)" in html

    assert 'data-ln-action="nyaa"' not in html
    assert 'id="lnAutoNyaa"' in html
    assert 'id="s_ln_auto_download"' in html
    assert 'id="s_ln_nyaa_category"' in html

    settings_block = html.split('<input id="s_anilist_enabled"', 1)[1].split(
        '<div class="setting-block"><h3>${t(\'settings.lightNovels\')}</h3>', 1
    )[0]
    assert "action.anilistDocs" not in settings_block
    assert html.count("https://docs.anilist.co/guide/auth/") == 1

    assert "semantically validate subtitle sync candidates" in html
    assert "translate selected Japanese text in the Light Novel reader" in html
    assert "семантически проверять варианты синхронизации субтитров" in html
    assert "переводить выделенный японский текст" in html
