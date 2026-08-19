from __future__ import annotations

from pathlib import Path

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode, NyaaRelease

ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"
MEDIA = ROOT / "pudge" / "web" / "media.js"
WEB_APP = ROOT / "pudge" / "web_app.py"
AUDIOBOOKS = ROOT / "pudge" / "audiobooks.py"


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.nyaa.enabled = True
    cfg.nyaa.auto_download_current = True
    cfg.nyaa.max_auto_download_per_anime = 2
    cfg.qbittorrent.enabled = True
    return AnimeManager(cfg, log=lambda _message: None)


def _release() -> NyaaRelease:
    return NyaaRelease(
        title="[SubsPlease] Sayonara Lara - 07 (1080p)",
        link="https://nyaa.si/view/7",
        torrent_url="https://nyaa.si/download/7.torrent",
        info_hash="7" * 40,
        size_text="1.2 GiB",
        size_bytes=1200 * 1024 * 1024,
        seeders=100,
        leechers=10,
        downloads=200,
        trusted=True,
        remake=False,
        group="SubsPlease",
        score=200.0,
        reasons=["exact-title-phrase", "single-episode=7", "1080p", "size-floor-ok"],
    )


def test_auto_search_skips_owned_episodes_without_consuming_search_budget(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = _manager(tmp_path)
    media_id = 7007
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=media_id,
            title="Sayonara Lara",
            status="CURRENT",
            progress=1,
            episodes=7,
        )
    )
    for episode in range(2, 7):
        path = manager.config.library.root_dir / f"Sayonara Lara - {episode:02d}.mkv"
        path.write_bytes(b"video")
        manager.db.upsert_episode(
            LibraryEpisode(
                media_id=media_id,
                title="Sayonara Lara",
                episode=episode,
                video_path=path,
            )
        )

    release = _release()
    searched: list[int] = []
    added: list[int] = []

    def fake_search(_media_id: int, *, episode: int | None, batch: bool, automatic: bool = False):
        assert batch is False
        assert automatic is True
        searched.append(int(episode or 0))
        return [release]

    def fake_add(_media_id: int, _item: NyaaRelease, *, episode: int | None, batch: bool):
        assert batch is False
        added.append(int(episode or 0))
        return release

    monkeypatch.setattr(manager, "search_releases", fake_search)
    monkeypatch.setattr(manager, "add_release", fake_add)
    monkeypatch.setattr(manager, "_release_is_allowed_for_auto", lambda _item: True)

    assert manager.auto_search_current() == 1
    assert searched == [7]
    assert added == [7]


def test_queue_next_is_removed_but_franchise_queue_remains() -> None:
    html = HTML.read_text(encoding="utf-8")
    assert "data-context-action=\"queue-next\"" not in html
    assert "action.createNextQueueCount" not in html
    assert "create_next_episodes_queue(a.media_id,5)" not in html
    assert "data-context-action=\"queue-franchise\"" in html


def test_light_novel_toolbar_uses_import_only_and_common_refresh_scans_local_first() -> None:
    html = HTML.read_text(encoding="utf-8")
    app = WEB_APP.read_text(encoding="utf-8")
    assert '<button id="pageImportButton" class="primary" hidden>Import</button>' in html
    assert '<button id="lnImport" hidden>Import</button>' in html
    assert "Import EPUB/TXT</button>" not in html
    assert 'id="lnRefresh"' not in html
    refresh_js = html[html.index("async function refreshAll()") : html.index("function animeFromId")]
    assert "light_novel_refresh" not in refresh_js

    refresh_py = app[app.index("    def refresh_local(") : app.index("    def refresh_all(")]
    assert refresh_py.index("self.light_novels.scan_downloaded()") < refresh_py.index(
        "self.manager.run_interactive_refresh()"
    )


def test_audiobook_tempo_uses_explicit_quality_preserving_wsola(tmp_path: Path) -> None:
    service = AudiobookService(
        Database(tmp_path / "db.sqlite3"),
        ffprobe="ffprobe",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )
    expected = ["--audio-pitch-correction=yes"]
    assert service._tempo_filter_args() == expected
    assert service._tempo_filter_args() == expected


def test_tempo_filter_is_kept_for_runtime_speed_changes() -> None:
    source = AUDIOBOOKS.read_text(encoding="utf-8")
    play = source[source.index("    def play(") : source.index("    def set_paused(")]
    speed = source[source.index("    def set_speed(") : source.index("    def seek(")]
    assert "command.extend(self._tempo_filter_args())" in play
    assert 'f"--speed={speed:.3f}"' in play
    assert '["set_property", "speed", value]' in speed


def test_speed_controls_blur_so_space_returns_to_transport() -> None:
    html = HTML.read_text(encoding="utf-8")
    media = MEDIA.read_text(encoding="utf-8")
    assert "control.blur();" in media
    assert "if(e.target.id==='lnPairedSpeed'){const speed=Number(e.target.value||1);e.target.blur();" in html
