from __future__ import annotations

import logging
import time
from pathlib import Path

from pudge.companion_streaming import CompanionStreamingService
from pudge.config import AppConfig, load_config
from pudge.database import Database
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.subtitle_runtime import repair_episode_subtitle, resolve_episode_subtitle
from pudge.web_app import WebAppApi


ROOT = Path(__file__).parents[1]


def test_reset_progress_clears_partial_ready_episode(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    video = tmp_path / "Odd Taxi - 01.mkv"
    subtitle = tmp_path / "Odd Taxi - 01.srt"
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語\n", encoding="utf-8")
    db.upsert_anime(LibraryAnime(media_id=128547, title="Odd Taxi", status="CURRENT", progress=4))
    db.upsert_episode(
        LibraryEpisode(128547, "Odd Taxi", 1, video, subtitle_path=subtitle, state="ready")
    )
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
    db.upsert_episode(
        LibraryEpisode(128547, "Odd Taxi", 1, video, subtitle_path=missing, state="ready")
    )
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

def test_companion_can_prepare_srt_after_hls_was_already_cached(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    video = tmp_path / "episode.mkv"
    subtitle = tmp_path / "episode.srt"
    video.write_bytes(b"video")
    subtitle.write_text(
        "1\n00:00:01,000 --> 00:00:02,250\n日本語です\n",
        encoding="utf-8",
    )
    service = CompanionStreamingService(db, cache_dir=tmp_path / "cache")
    out = tmp_path / "hls"
    result = service._prepare_subtitles(
        {
            "entity_id": "episode",
            "media_id": 1,
            "episode": 1,
            "video_path": video,
            "subtitle_path": str(subtitle),
            "embedded_subtitle_id": None,
            "subtitle_origin": "jimaku",
        },
        out,
    )
    assert result["ready"] is True
    vtt = (out / "subtitles.vtt").read_text(encoding="utf-8")
    assert vtt.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:02.250" in vtt
    source = (ROOT / "pudge" / "companion_streaming.py").read_text(encoding="utf-8")
    prepare = source[source.index("    def prepare("):source.index("    def media_path(")]
    assert prepare.index("_prepare_subtitles") < prepare.index("_ensure_job")


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


def test_desktop_play_uses_runtime_subtitle_recovery_and_imports_do_not_default_downloads() -> None:
    source = (ROOT / "pudge" / "web_app.py").read_text(encoding="utf-8")
    play = source[source.index("    def play("):source.index("    def play_status(")]
    assert "resolve_episode_subtitle(" in play
    assert "repair_episode_subtitle(" in play
    assert "RESULT step=play.subtitle_recover" in play

    ln = source[source.index("    def choose_light_novel_file("):source.index("    def ", source.index("    def choose_light_novel_file(") + 8)]
    manga = source[source.index("    def choose_manga_file("):source.index("    def ", source.index("    def choose_manga_file(") + 8)]
    assert "directory=str(Path.home())" in ln
    assert "directory=str(Path.home())" in manga


def test_fast_update_preserves_native_app_identity() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "PRESERVE_NATIVE_APP=0" in installer
    assert "NATIVE_SHELL_REV=1" in installer
    assert "PUDGE_FORCE_NATIVE_REBUILD" in installer
    assert "preserving native app identity and existing macOS folder grants" in installer
    assert "EXISTING_APP_VERSION" not in installer
