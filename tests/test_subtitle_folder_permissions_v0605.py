from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

from pudge.config import AppConfig, load_config, write_config
from pudge.local_search import find_local_subtitles
from pudge.models import VideoIdentity
from pudge.web_app import WebAppApi


def test_new_config_does_not_implicitly_scan_downloads(tmp_path: Path):
    loaded = load_config(tmp_path / "missing.toml")
    assert loaded.paths.download_dirs == []
    assert loaded.paths.subtitle_dirs == []


def test_old_download_dirs_migrate_to_visible_subtitle_dirs(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    configured = tmp_path / "configured"
    config_path.write_text(
        f'[paths]\ndownload_dirs = ["{configured}"]\n',
        encoding="utf-8",
    )
    loaded = load_config(config_path)
    assert loaded.paths.subtitle_dirs == [configured.resolve()]
    assert loaded.paths.download_dirs == []


def test_empty_subtitle_folder_round_trips(tmp_path: Path):
    path = tmp_path / "config.toml"
    config = AppConfig(config_path=path)
    config.paths.subtitle_dirs = []
    write_config(config, path)
    loaded = load_config(path)
    assert loaded.paths.subtitle_dirs == []
    assert "subtitle_dirs = []" in path.read_text(encoding="utf-8")


def test_local_search_ignores_unconfigured_downloads(tmp_path: Path):
    video_dir = tmp_path / "library" / "Anime"
    video_dir.mkdir(parents=True)
    video = video_dir / "Example - 05.mkv"
    video.write_bytes(b"video")

    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "Example - 05.ja.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n日本語です\n",
        encoding="utf-8",
    )

    identity = VideoIdentity(title="Example", episode=5)
    candidates = find_local_subtitles(
        video,
        identity,
        subtitle_dirs=[],
        cache_dir=tmp_path / "cache",
        max_files=100,
    )
    assert candidates == []

    configured = find_local_subtitles(
        video,
        identity,
        subtitle_dirs=[downloads],
        cache_dir=tmp_path / "cache",
        max_files=100,
    )
    assert configured and configured[0].path.parent == downloads


def test_permission_preflight_is_not_repeated_after_first_run(monkeypatch, tmp_path: Path):
    library = tmp_path / "library"
    subtitles = tmp_path / "subs"
    library.mkdir()
    subtitles.mkdir()
    captured: list[Path] = []

    def fake_request(paths):
        captured.extend(Path(path) for path in paths)
        return {str(path): True for path in paths}

    monkeypatch.setattr("pudge.web_app.request_folder_access", fake_request)

    api = WebAppApi.__new__(WebAppApi)
    api.config = AppConfig()
    api.config.library.root_dir = library
    api.config.paths.download_dirs = [tmp_path / "Downloads-legacy"]
    api.config.paths.subtitle_dirs = [subtitles]
    api.config.ui.permissions_requested = True
    api.config_path = tmp_path / "config.toml"
    api.logger = logging.getLogger("test-permissions")
    api._settings_payload = lambda: {"permissions_requested": True}

    result = api.request_permissions()

    assert result["ok"] is True
    assert captured == []
    assert result["folders"] == {}


def test_web_ui_exposes_optional_subtitle_folder_and_requests_access_early():
    html = Path("pudge/web/index.html").read_text(encoding="utf-8")
    assert "settings.subtitleFolder':'Subtitle Inbox'" in html
    assert "settings.subtitleFolder':'Subtitle Inbox'" in html
    assert "id=\"s_subtitle_folders\"" in html
    assert "id=\"s_watched_folders\"" in html
    assert "onboarding.subtitleFolderTitle" in html
    assert "o_subtitle_enabled" in html
    assert "const firstPermissionRequest=!ui.state.settings.permissions_requested" in html
    assert "await pywebview.api.request_permissions()" in html
