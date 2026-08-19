from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode
from pudge.web_app import WebAppApi

ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"
MEDIA_JS = ROOT / "pudge" / "web" / "media.js"
SELECT_JS = ROOT / "pudge" / "web" / "pudge_select.js"
SELECT_CSS = ROOT / "pudge" / "web" / "pudge_select.css"
APP_ENTRY = ROOT / "pudge" / "app_entry.py"
MANAGER = ROOT / "pudge" / "manager.py"
WEB_APP = ROOT / "pudge" / "web_app.py"


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.qbittorrent.enabled = True
    cfg.aria2.enabled = False
    return AnimeManager(cfg, log=lambda _message: None)


def test_audiobooks_share_top_import_and_have_no_duplicate_header_or_folder_button() -> None:
    html = HTML.read_text(encoding="utf-8")
    media = MEDIA_JS.read_text(encoding="utf-8")
    assert "['lightnovels','manga','audiobooks'].includes(page)" in html
    assert "else if(ui.page==='audiobooks')$('audiobookImport')?.click()" in html
    assert 'id="audiobookImportFolder"' not in media
    assert "<h2>${ru()?'Аудиокниги':'Audiobooks'}</h2>" not in media
    assert 'id="audiobookImport" hidden' in media
    assert 'data-audio-import-kind=\"file\"' in media
    assert 'data-audio-import-kind=\"folder\"' in media
    assert 'choose_audiobook_folder()' in media


def test_torrent_toggle_is_red_off_green_on_and_download_modal_has_no_duplicate_global_controls() -> None:
    html = HTML.read_text(encoding="utf-8")
    assert "#torrentToggleButton.torrent-on" in html
    assert "#torrentToggleButton.torrent-off" in html
    assert "classList.toggle('torrent-on',enabled)" in html
    assert 'data-action="torrent-toggle"' not in html
    assert 'data-action="torrent-stop-all"' not in html
    center = html[html.index("function renderDownloadCenter"):html.index("function stopDownloadCenterPoll")]
    assert "Client" not in center and "Клиент" not in center


def test_system_selects_are_wrapped_in_pudge_dropdown_without_replacing_ln_chapter_picker() -> None:
    html = HTML.read_text(encoding="utf-8")
    script = SELECT_JS.read_text(encoding="utf-8")
    css = SELECT_CSS.read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="pudge_select.css">' in html
    assert '<script src="pudge_select.js"></script>' in html
    assert "querySelectorAll?.('select').forEach(enhance)" in script
    assert "select.id === 'lnChapterSelect'" in script
    assert "ln-chapter-native-select" in script
    assert "MutationObserver" in script
    assert "enhanced.get(select)?.button?.blur()" in script
    assert ".pudge-select-menu" in css and ".pudge-select-option.selected" in css


def test_macos_identity_is_set_before_nsapplication_registration() -> None:
    source = APP_ENTRY.read_text(encoding="utf-8")
    process = source.index("setProcessName_(APP_NAME)")
    display = source.index('setObject_forKey_(APP_NAME, "CFBundleDisplayName")')
    application = source.index("NSApplication.sharedApplication()")
    assert process < application
    assert display < application
    assert 'setObject_forKey_(APP_BUNDLE_ID, "CFBundleIdentifier")' in source


def test_torrent_off_still_reconciles_completed_episode_and_pauses_backend(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    manager.config.nyaa.torrents_enabled = False
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=210031,
            title="Seihantai na Kimi to Boku 2nd Season",
            status="CURRENT",
            progress=6,
            episodes=13,
        )
    )
    target = manager.config.library.root_dir / "Seihantai na Kimi to Boku 2nd Season"
    target.mkdir(parents=True)
    video = target / "[SubsPlease] Seihantai na Kimi to Boku - 19 (1080p) [C9121DE6].mkv"
    video.write_bytes(b"video")
    item = DownloadItem(
        torrent_hash="f3a099e5c9a9519b1b406a23202f94e1e9d07157",
        name=video.name,
        state="complete",
        progress=1.0,
        save_path=str(target),
        content_path=str(video),
        media_id=210031,
        episode=7,
        media_episode=7,
        release_episode=19,
        completed_on=1234,
        raw={"category": manager.config.qbittorrent.category, "_tag_set": ["pudge"]},
    )
    paused: list[str] = []

    class Client:
        def torrents(self, *, category: str = ""):
            return [item]

        def pause(self, torrent_hash: str) -> None:
            paused.append(torrent_hash)

        def close(self) -> None:
            pass

    monkeypatch.setattr(manager, "torrent_clients", lambda: [("qbittorrent", Client())])
    monkeypatch.setattr("pudge.manager.japanese_subtitle_source", lambda *_args, **_kwargs: ("none", None))

    assert manager.downloads_enabled() is False
    assert manager.sync_downloads() == 1
    episodes = manager.db.episodes(210031)
    assert len(episodes) == 1
    assert episodes[0].episode == 7
    assert episodes[0].release_episode == 19
    assert episodes[0].video_path == video.resolve()
    assert paused == [item.torrent_hash]


def test_diagnosis_repairs_stale_waiting_row_when_prepared_text_subtitle_exists(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.config.qbittorrent.enabled = False
    manager.db.upsert_anime(LibraryAnime(media_id=200637, title="100 Girlfriends S3", status="CURRENT"))
    video = tmp_path / "episode6.mkv"
    subtitle = tmp_path / "prepared.srt"
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n日本語\n", encoding="utf-8")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=200637,
            title="100 Girlfriends S3",
            episode=6,
            video_path=video,
            subtitle_path=subtitle,
            subtitle_origin="jimaku",
            state="waiting_subtitles",
        )
    )

    diagnosis = manager.diagnose_episode(200637, 6)
    assert diagnosis["ready"] is True
    repaired = manager.db.episode_by_path(video)
    assert repaired is not None and repaired.state == "ready"
    assert repaired.subtitle_path == subtitle


def test_watched_media_folders_route_ln_manga_and_audiobooks_and_are_idempotent(tmp_path: Path, monkeypatch) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    watched = tmp_path / "watched"
    audio = watched / "Audio Book"
    watched.mkdir(parents=True)
    audio.mkdir()
    (watched / "novel.txt").write_text("これは日本語のライトノベル本文です。物語が続きます。" * 30, encoding="utf-8")
    (watched / "comic.cbz").write_bytes(b"not-an-archive-because-import-is-mocked")
    (watched / "single.m4b").write_bytes(b"audio")
    (audio / "01.mp3").write_bytes(b"one")
    (audio / "02.mp3").write_bytes(b"two")
    cfg.paths.download_dirs = [watched]
    from pudge.config import write_config
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(api.light_novels, "import_file", lambda path, **_kwargs: calls.append(("ln", Path(path))) or {})
    monkeypatch.setattr(api.manga, "import_file", lambda path: calls.append(("manga", Path(path))) or {})
    monkeypatch.setattr(api.audiobooks, "import_file", lambda path: calls.append(("audio-file", Path(path))) or {})
    monkeypatch.setattr(api.audiobooks, "import_folder", lambda path: calls.append(("audio-folder", Path(path))) or {})

    first = api.scan_watched_media_folders()
    second = api.scan_watched_media_folders()

    assert first["light_novels"] == 1
    assert first["manga"] == 1
    assert first["audiobooks"] == 2
    assert second["light_novels"] == second["manga"] == second["audiobooks"] == 0
    assert ("audio-folder", audio.resolve()) in calls
    assert ("audio-file", (watched / "single.m4b").resolve()) in calls


def test_refresh_scans_watched_and_video_sources_before_heavy_refresh() -> None:
    source = WEB_APP.read_text(encoding="utf-8")
    section = source[source.index("def refresh_local"):source.index("def refresh_all")]
    watched = section.index("self.scan_watched_media_folders()")
    videos = section.index("self.manager.scan_library()")
    heavy = section.index("self.manager.run_interactive_refresh()")
    assert watched < videos < heavy


def test_ready_diagnosis_refreshes_stale_card_state() -> None:
    html = HTML.read_text(encoding="utf-8")
    diagnosis = html[html.index("async function openEpisodeDiagnostics"):html.index("function planningReleasedEpisodeCountForAnime")]
    assert "if(data.ready){ui.state=await pywebview.api.get_state_fast()" in diagnosis
    assert "renderDataPages()" in diagnosis


def test_ln_paired_resume_snaps_visual_clock_without_catchup() -> None:
    html = HTML.read_text(encoding="utf-8")
    assert "function lnPairedResumeCatchupOffset" not in html
    section = html[html.index("function renderLnPairedPosition"):html.index("function startLnPairedInterpolation")]
    assert "resumeSnap=resumedAfterPause&&Number.isFinite(lastDisplay)" in section
    assert "resumeCatchup=false" in section
    assert "!resumeSnap&&Number.isFinite(lastDisplay)&&offset<lastDisplay" in section
    assert "lnPairedSmoothOffset(offset,state,{sameChapter,seekJump,speechActive,resumeSnap})" in section
