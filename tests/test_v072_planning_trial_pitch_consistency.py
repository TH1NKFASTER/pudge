from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig, load_config, write_config
from pudge.database import Database
from pudge.light_novels import LightNovelService
from pudge.jimaku_trial import apply_jimaku_trial
from pudge.manager_models import LibraryAnime, LibraryEpisode, NyaaRelease
from pudge.web_app import WebAppApi


ROOT = Path(__file__).resolve().parents[1]


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.cover_cache_dir = tmp_path / "cache" / "covers"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True, exist_ok=True)
    config.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return config


def test_jimaku_bundled_key_is_runtime_only_and_expires(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    write_config(config, config.config_path)
    monkeypatch.setenv("PUDGE_BUNDLED_JIMAKU_API_KEY", "trial-secret")

    loaded = load_config(config.config_path)
    assert loaded.jimaku.api_key == "trial-secret"
    assert loaded.jimaku.personal_api_key == ""
    assert loaded.jimaku.trial_active is True

    write_config(loaded, config.config_path)
    assert "trial-secret" not in config.config_path.read_text(encoding="utf-8")

    marker = config.paths.cache_dir / "jimaku-trial.json"
    marker.write_text(json.dumps({"started_at": 1.0}), encoding="utf-8")
    expired = load_config(config.config_path)
    assert expired.jimaku.api_key == ""
    assert expired.jimaku.trial_active is False
    assert expired.jimaku.trial_expires_at == 1.0 + 48 * 60 * 60


def test_direct_app_config_jimaku_key_remains_a_personal_key(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.jimaku.api_key = "personal-key"

    apply_jimaku_trial(config, now=100.0)

    assert config.jimaku.api_key == "personal-key"
    assert config.jimaku.personal_api_key == "personal-key"
    assert config.jimaku.trial_active is False


def test_library_scan_preserves_confirmed_bitmap_state(tmp_path: Path) -> None:
    database = Database(tmp_path / "library.sqlite3")
    video = tmp_path / "movie.mkv"
    bitmap = tmp_path / "movie.sup"
    video.write_bytes(b"video")
    bitmap.write_bytes(b"bitmap")
    database.upsert_episode(
        LibraryEpisode(
            media_id=178788,
            title="Kimetsu no Yaiba: Mugenjou-hen Movie 1",
            episode=None,
            video_path=video.resolve(),
            subtitle_path=bitmap.resolve(),
            subtitle_origin="bitmap",
            state="waiting_text_subtitles",
        )
    )

    database.upsert_episode(
        LibraryEpisode(
            media_id=178788,
            title="Kimetsu no Yaiba: Mugenjou-hen Movie 1",
            episode=None,
            video_path=video.resolve(),
            state="waiting_subtitles",
        )
    )

    stored = database.episode_by_path(video.resolve())
    assert stored is not None
    assert stored.state == "waiting_text_subtitles"
    assert stored.subtitle_path == bitmap.resolve()
    assert stored.subtitle_origin == "bitmap"


def test_planning_background_job_skips_local_and_uses_full_search(tmp_path: Path) -> None:
    release = NyaaRelease(
        title="[ToonsHub] Goodbye Lara S01E05 1080p WEB-DL",
        link="https://nyaa.si/view/1",
        torrent_url="https://nyaa.si/download/1.torrent",
        info_hash="abc123",
        size_text="1.4 GiB",
        size_bytes=1_503_238_553,
        seeders=138,
        leechers=1,
        downloads=1294,
        trusted=False,
        remake=False,
    )
    calls: list[tuple[int, bool]] = []
    manager = SimpleNamespace(
        downloads_enabled=lambda: True,
        sync_downloads=lambda: None,
        scan_library=lambda: [],
        search_and_add_best=lambda _media_id, **kwargs: (
            calls.append((int(kwargs["episode"]), bool(kwargs["automatic"])))
            or (release if int(kwargs["episode"]) == 5 else None)
        ),
    )
    api = object.__new__(WebAppApi)
    api.logger = SimpleNamespace(warning=lambda *_args, **_kwargs: None, exception=lambda *_args, **_kwargs: None)
    api._planning_episode_download_lock = threading.Lock()
    api._planning_episode_download_state = {
        "status": "running",
        "running": True,
        "episodes": [],
    }
    api._planning_local_episodes = lambda *_args, **_kwargs: [1, 2, 3, 4]
    anime = LibraryAnime(
        media_id=190000,
        title="Goodbye Lara",
        episodes=5,
        media_status="FINISHED",
    )

    api._run_planning_episode_download(manager, anime, 5)

    assert calls == [(5, False)]
    assert api._planning_episode_download_state["status"] == "done"
    assert api._planning_episode_download_state["episodes"][-1] == {
        "episode": 5,
        "status": "added",
        "release": release.title,
        "error": "",
    }


def test_audiobook_stop_uses_cached_position_without_blocking_reads(
    tmp_path: Path, monkeypatch
) -> None:
    database = Database(tmp_path / "library.sqlite3")
    service = AudiobookService(
        database,
        ffprobe="ffprobe",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )
    book = service._upsert(
        path=tmp_path / "book.m4b",
        title="Book",
        duration=120.0,
        files=[],
        chapters=[],
    )
    events: list[str] = []
    ipc_commands: list[list[list[object]]] = []

    class Process:
        def poll(self):
            return None

        def terminate(self):
            events.append("terminate")

        def wait(self, timeout):
            events.append(f"wait:{timeout}")

        def kill(self):
            events.append("kill")

    book_id = int(book["id"])
    service._players[book_id] = Process()
    service._ipc_paths[book_id] = tmp_path / "book.sock"
    service._last_positions[book_id] = 42.0

    monkeypatch.setattr(
        service,
        "_ipc_commands_no_wait",
        lambda _path, commands: ipc_commands.append(commands) or True,
    )
    monkeypatch.setattr(
        service,
        "_global_position",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Stop must not perform blocking IPC position reads")
        ),
    )

    result = service.stop(book_id)

    assert result["stopped"] is True
    assert ipc_commands == [[
        ["set_property", "mute", True],
        ["set_property", "pause", True],
        ["quit"],
    ]]
    assert events[:2] == ["terminate", "wait:0.12"]
    assert service.book(book_id)["position"] == 42.0


def test_light_novel_pitch_setting_defaults_on_and_persists(tmp_path: Path) -> None:
    service = LightNovelService(_config(tmp_path))
    assert service.settings_payload()["show_pitch_accent"] is True
    assert service.save_settings({"show_pitch_accent": False})["show_pitch_accent"] is False
    assert LightNovelService(service.config).settings_payload()["show_pitch_accent"] is False


def test_inline_pitch_and_background_planning_frontend_contracts() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    reading = (ROOT / "pudge/web/reading_tools.js").read_text(encoding="utf-8")
    css = (ROOT / "pudge/web/reading_tools.css").read_text(encoding="utf-8")
    manager = (ROOT / "pudge/manager.py").read_text(encoding="utf-8")

    assert "renderInlinePitch" in reading
    assert "inlinePitch: renderInlinePitch" in reading
    assert "ln-pitch-ruby" in html and "lnrPitchAccent" in html
    assert ".ln-reader.hide-pitch-accent" in css
    assert "planning_episode_download_status" in html
    assert "resumePlanningEpisodeDownload" in html
    assert "if not self.config.matching.ocr_image_subtitles:" in manager
    assert "attempts >= 3 and not self.config.matching.ocr_image_subtitles" not in manager
