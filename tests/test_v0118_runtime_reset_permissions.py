from __future__ import annotations

import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from desktop_isolation import patch_webapp_popen
from pudge.config import AppConfig, load_config
from pudge.database import Database
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.subtitle_runtime import repair_episode_subtitle, resolve_episode_subtitle
from pudge.web_app import WebAppApi


def test_reset_progress_clears_partial_ready_episode(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    video = tmp_path / "Odd Taxi - 01.mkv"
    subtitle = tmp_path / "Odd Taxi - 01.srt"
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語\n", encoding="utf-8")
    db.upsert_anime(LibraryAnime(media_id=128547, title="Odd Taxi", status="CURRENT", progress=4))
    db.upsert_episode(LibraryEpisode(128547, "Odd Taxi", 1, video, subtitle_path=subtitle, state="ready"))
    db.record_playback(video, 130.0, 1430.0, active_seconds=80.0)

    changed = db.reset_anime_progress(128547)
    episode = db.episode_by_path(video)

    assert changed >= 1
    assert episode is not None
    assert episode.state == "ready"
    assert episode.playback_position is None
    assert episode.playback_duration is None
    assert episode.playback_updated_at is None
    assert episode.playback_active_seconds == 0
    assert db.get_anime(128547).progress == 0


def test_runtime_subtitle_recovers_selected_history_and_repairs_db(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    video = tmp_path / "Odd Taxi - 01.mkv"
    good = tmp_path / "prepared.srt"
    missing = tmp_path / "missing.srt"
    video.write_bytes(b"video")
    good.write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語です\n", encoding="utf-8")
    db.upsert_anime(LibraryAnime(media_id=128547, title="Odd Taxi", status="CURRENT"))
    db.upsert_episode(LibraryEpisode(128547, "Odd Taxi", 1, video, subtitle_path=missing, state="ready"))
    db.record_subtitle_history(
        video_path=video,
        media_id=128547,
        episode=1,
        source="jimaku",
        candidate_name="Odd.Taxi.S01E01.ja.srt",
        candidate_path=good,
        score=113.0,
        status="selected",
        details={"final_path": str(good)},
    )

    selection = resolve_episode_subtitle(
        db,
        video_path=video,
        media_id=128547,
        episode=1,
        stored_path=missing,
        stored_embedded_id=None,
        stored_origin="jimaku",
        ffprobe="missing-ffprobe",
        ffmpeg="missing-ffmpeg",
    )
    assert selection.external_path == good.resolve()
    assert selection.reason == "subtitle_history"
    assert selection.recovered is True
    assert repair_episode_subtitle(db, video_path=video, selection=selection) is True
    repaired = db.episode_by_path(video)
    assert repaired is not None
    assert repaired.subtitle_path == good.resolve()


def test_runtime_resolver_preserves_explicit_bitmap_sid_only_when_allowed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "library.sqlite3")
    video = tmp_path / "bitmap.mkv"
    video.write_bytes(b"video")

    monkeypatch.setattr(
        "pudge.subtitle_runtime.probe_media",
        lambda *_args, **_kwargs: {
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264"},
                {"index": 1, "codec_type": "subtitle", "codec_name": "subrip"},
                {"index": 2, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"},
            ]
        },
    )
    monkeypatch.setattr(
        "pudge.subtitle_runtime.find_embedded_japanese_subtitles",
        lambda *_args, **_kwargs: [],
    )

    blocked = resolve_episode_subtitle(
        db,
        video_path=video,
        media_id=88,
        episode=1,
        stored_embedded_id=2,
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        allow_bitmap=False,
    )
    assert blocked.found is False

    allowed = resolve_episode_subtitle(
        db,
        video_path=video,
        media_id=88,
        episode=1,
        stored_embedded_id=2,
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        allow_bitmap=True,
    )
    assert allowed.embedded_subtitle_id == 2
    assert allowed.embedded_stream_index == 2
    assert allowed.codec == "hdmv_pgs_subtitle"
    assert allowed.is_text is False


def test_runtime_resolver_trusts_stored_sid_for_explicit_bitmap_playback_when_probe_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "library.sqlite3")
    video = tmp_path / "library.mkv"
    video.write_bytes(b"video")

    def fail_probe(*_args, **_kwargs):
        raise RuntimeError("ffprobe cannot inspect test fixture")

    monkeypatch.setattr("pudge.subtitle_runtime.probe_media", fail_probe)
    monkeypatch.setattr(
        "pudge.subtitle_runtime.find_embedded_japanese_subtitles",
        lambda *_args, **_kwargs: [],
    )

    selected = resolve_episode_subtitle(
        db,
        video_path=video,
        media_id=88,
        episode=1,
        stored_embedded_id=2,
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        allow_bitmap=True,
    )
    assert selected.embedded_subtitle_id == 2
    assert selected.embedded_stream_index is None
    assert selected.is_text is False


def test_permission_preflight_skips_folder_touch_after_first_run(monkeypatch, tmp_path: Path) -> None:
    api = WebAppApi.__new__(WebAppApi)
    api.config = AppConfig()
    api.config.config_path = tmp_path / "config.toml"
    api.config.library.root_dir = tmp_path / "library"
    api.config.paths.subtitle_dirs = [Path.home() / "Downloads"]
    api.config.ui.permissions_requested = True
    api.config_path = api.config.config_path
    api.logger = logging.getLogger("v14-permissions")
    api._settings_payload = lambda: {"permissions_requested": True}

    monkeypatch.setattr(
        "pudge.web_app.request_folder_access",
        lambda _paths: (_ for _ in ()).throw(AssertionError("folder probe must be skipped")),
    )
    result = api.request_permissions()
    assert result["folders"] == {}
    assert result["notifications"]["skipped"] is True


def test_legacy_implicit_downloads_is_not_recreated_as_subtitle_folder(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    downloads = (Path.home() / "Downloads").resolve()
    config.write_text(f'[paths]\ndownload_dirs = ["{downloads}"]\n', encoding="utf-8")
    loaded = load_config(config)
    assert loaded.paths.download_dirs == []
    assert loaded.paths.subtitle_dirs == []


def test_play_recovers_a_lost_subtitle_from_history_and_repairs_the_library(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    # The Ready row points at a subtitle the cache cleanup removed; the prepared file is still
    # recorded in the history. Play starts mpv with it (not without subtitles) and fixes the row.
    manager = AnimeManager(_play_config(tmp_path), log=lambda _message: None)
    video = (tmp_path / "Odd Taxi - 01.mkv").resolve()
    good = (tmp_path / "prepared.srt").resolve()
    video.write_bytes(b"video")
    good.write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語です\n", encoding="utf-8")
    manager.db.upsert_episode(
        LibraryEpisode(128547, "Odd Taxi", 1, video, subtitle_path=tmp_path / "missing.srt", state="ready")
    )
    manager.db.record_subtitle_history(
        video_path=video,
        media_id=128547,
        episode=1,
        source="jimaku",
        candidate_name="Odd.Taxi.S01E01.ja.srt",
        candidate_path=good,
        score=113.0,
        status="selected",
        details={"final_path": str(good)},
    )
    api = WebAppApi.__new__(WebAppApi)
    api.config, api.manager, api.logger = manager.config, manager, logging.getLogger("v0118-play")
    api._play_processes, api._play_started_at, api._play_exit_codes, api._play_registry = {}, {}, {}, {}
    api._play_registry_path = manager.config.paths.cache_dir / "active-playbacks.json"
    api._play_lock = threading.Lock()
    launched: list[list[str]] = []
    patch_webapp_popen(
        monkeypatch,
        lambda command, **_kwargs: launched.append(command) or SimpleNamespace(pid=4242, poll=lambda: None),
    )

    with caplog.at_level(logging.INFO, logger="v0118-play"):
        api.play(str(video))

    [command] = launched
    assert command[command.index("--sub") + 1] == str(good)
    assert manager.db.episode_by_path(video).subtitle_path == good
    assert any(
        message.startswith("RESULT step=play.subtitle_recover video=Odd Taxi - 01.mkv source=jimaku")
        for message in caplog.messages
    )


def _play_config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    return cfg


def test_import_file_dialogs_open_in_the_home_folder_not_downloads(monkeypatch) -> None:
    # Opening Downloads triggers macOS's Downloads-folder permission prompt.
    pytest.importorskip("webview")
    api = WebAppApi.__new__(WebAppApi)
    dialogs: list[dict] = []
    api.window = SimpleNamespace(create_file_dialog=lambda *_args, **options: dialogs.append(options))
    assert api.choose_light_novel_file() == {"cancelled": True}
    assert api.choose_manga_file() == {"cancelled": True}
    assert [options["directory"] for options in dialogs] == [str(Path.home())] * 2


# Fast updates keep the native app (and its folder grants): test_install_script.py
# (test_fast_update_keeps_the_native_app_..., test_native_app_is_rebuilt_when_forced_...).
