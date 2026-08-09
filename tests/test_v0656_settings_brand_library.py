from __future__ import annotations

from pathlib import Path

from anime_mpv.config import AppConfig
from anime_mpv.database import Database
from anime_mpv.energy_diagnostics import EnergyDiagnosticsMonitor
from anime_mpv.library import scan_library
from anime_mpv.manager_models import LibraryAnime, LibraryEpisode

ROOT = Path(__file__).parents[1]
HTML = ROOT / "anime_mpv" / "web" / "index.html"


def test_shortcuts_are_last_settings_block_and_are_press_to_capture() -> None:
    html = HTML.read_text(encoding="utf-8")
    settings = html.split("function renderSettings(){", 1)[1].split("function fillSettings", 1)[0]
    assert settings.rindex("settings.shortcuts") > settings.rindex("settings.maintenance")
    assert "shortcutRecorder('s_shortcut_mpv_watched'" in settings
    assert "input('s_shortcut_mpv_watched'" not in settings
    assert "startShortcutCapture(recorder)" in html
    assert "event.key==='Backspace'||event.key==='Delete'" in html


def test_standard_app_shortcuts_are_dynamic_and_not_configurable() -> None:
    html = HTML.read_text(encoding="utf-8")
    assert "document.querySelectorAll('.nav button[data-page]')" in html
    assert "/^[1-9]$/.test(event.key)" in html
    assert "button=buttons[Number(event.key)-1]" in html
    assert "shortcut_app_" not in html
    assert "shortcutPlanningSearch" not in html


def test_subtitle_upgrade_conditions_are_hidden_when_upgrade_disabled() -> None:
    html = HTML.read_text(encoding="utf-8")
    assert 'id="s_auto_upgrade_subtitles"' in html
    assert 'id="settings-subtitle-upgrade-fields" class="conditional-settings"' in html
    assert "s_auto_upgrade_subtitles:'settings-subtitle-upgrade-fields'" in html


def test_qbittorrent_help_does_not_explain_api_key_port() -> None:
    html = HTML.read_text(encoding="utf-8")
    assert "An API key does not start the server or choose its port." not in html
    assert "API key не включает сервер и не задаёт его порт" not in html


def test_alternative_movie_covers_use_relation_hover_preview() -> None:
    html = HTML.read_text(encoding="utf-8")
    assert 'class="full-relation-alt"' in html
    assert 'data-relation-alternative="1"' in html
    assert 'data-relation-node="1"' in html
    assert "node.dataset.relationAlternative==='1'" in html
    assert "top=rect.top-pr.height-gap" in html


def test_library_shows_total_duration() -> None:
    html = HTML.read_text(encoding="utf-8")
    assert "label.totalDuration':'Duration: {duration}'" in html
    assert "formatDuration(a.duration_seconds)" in html


def test_managed_library_detaches_stale_catmahjong_false_match(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "Library"
    root.mkdir()
    video = root / "catmahjong.mp4"
    video.write_bytes(b"video")
    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(LibraryAnime(media_id=999, title="Mahoutsukai no Yoru", synonyms=["Mahoyo"]))
    db.upsert_episode(
        LibraryEpisode(
            media_id=999,
            title="Mahoutsukai no Yoru",
            episode=None,
            video_path=video,
            state="local",
        )
    )
    monkeypatch.setattr(
        "anime_mpv.library.japanese_subtitle_details",
        lambda *_a, **_k: ("none", None, None),
    )

    rows = scan_library(root, db)
    assert len(rows) == 1
    assert rows[0].media_id is None
    persisted = db.episode_by_path(video)
    assert persisted is not None
    assert persisted.media_id is None
    assert persisted.title == "catmahjong"


def test_energy_log_separates_app_and_external_context(monkeypatch) -> None:
    rows = [
        {"pid": 100, "ppid": 1, "cpu_percent": 5.0, "memory_percent": 1.0, "rss_mb": 40.0, "elapsed": "02:00", "command": "/Applications/Anime MPV.app/Contents/MacOS/Anime MPV"},
        {"pid": 101, "ppid": 100, "cpu_percent": 7.0, "memory_percent": 1.0, "rss_mb": 60.0, "elapsed": "01:59", "command": "python worker.py"},
        {"pid": 102, "ppid": 1, "cpu_percent": 3.0, "memory_percent": 1.0, "rss_mb": 30.0, "elapsed": "01:59", "command": "/System/Library/Frameworks/WebKit.framework/com.apple.WebKit.GPU"},
        {"pid": 200, "ppid": 1, "cpu_percent": 11.0, "memory_percent": 1.0, "rss_mb": 80.0, "elapsed": "10:00", "command": "/Applications/qBittorrent.app/Contents/MacOS/qbittorrent"},
        {"pid": 300, "ppid": 1, "cpu_percent": 50.0, "memory_percent": 1.0, "rss_mb": 20.0, "elapsed": "2-00:00:00", "command": "/System/Library/Frameworks/WebKit.framework/com.apple.WebKit.GPU"},
    ]
    monkeypatch.setattr("anime_mpv.energy_diagnostics.os.getpid", lambda: 100)
    monkeypatch.setattr(EnergyDiagnosticsMonitor, "_process_rows", staticmethod(lambda: rows))
    sample = EnergyDiagnosticsMonitor(interval_seconds=30).sample()
    assert sample["app_cpu_percent"] == 15.0
    assert sample["context_cpu_percent"] == 11.0
    assert sample["related_cpu_percent"] == 26.0
    scopes = {row["pid"]: row["scope"] for row in sample["processes"]}
    assert scopes == {100: "app", 101: "app", 102: "app", 200: "context"}


def test_branding_is_centralized_for_install_and_runtime() -> None:
    brand = (ROOT / "anime_mpv" / "brand.env").read_text(encoding="utf-8")
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    release = (ROOT / "build_release.sh").read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")
    assert 'APP_NAME="pudge"' in brand
    assert 'source "$PROJECT_DIR/anime_mpv/brand.env"' in installer
    assert 'APP_PATH="$APP_DIR/$APP_NAME.app"' in installer
    assert '--osx-bundle-identifier "$APP_BUNDLE_ID"' in installer
    assert 'STAGE="$PROJECT_DIR/dist/release/$APP_SLUG"' in release
    assert "ui.state?.branding?.name||document.getElementById('appBrandName')?.textContent||'__APP_NAME__'" in html
    assert (ROOT / "rename_brand.py").is_file()
