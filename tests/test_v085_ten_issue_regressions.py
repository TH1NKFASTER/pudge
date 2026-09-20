from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from pudge import agent
from pudge.config import AppConfig, write_config
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode, NyaaRelease
from pudge.web_app import WebAppApi


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def test_later_ready_episodes_do_not_make_missing_nearest_episode_ready(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=79001,
            title="Sayonara Lara",
            status="PLANNING",
            media_status="RELEASING",
            progress=0,
            episodes=12,
        )
    )
    for episode in range(2, 7):
        video = tmp_path / f"Lara - {episode:02d}.mkv"
        video.write_bytes(b"video")
        api.manager.db.upsert_episode(
            LibraryEpisode(
                media_id=79001,
                title="Sayonara Lara",
                episode=episode,
                video_path=video,
                state="ready",
            )
        )

    home = api.get_state()["home"]

    assert not any(item.get("media_id") == 79001 for item in home["new_ready"])
    assert any(item.get("media_id") == 79001 for item in home["waiting"])


def test_airing_search_skips_unwatched_local_episode_and_reaches_new_release(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.nyaa.enabled = True
    cfg.nyaa.auto_download_current = True
    cfg.qbittorrent.enabled = True
    cfg.nyaa.max_auto_download_per_anime = 2
    manager = AnimeManager(cfg)
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=79002,
            title="Airing",
            status="CURRENT",
            media_status="RELEASING",
            progress=5,
            episodes=12,
            next_airing_episode=8,
            next_airing_at=int(time.time()) + 3600,
        )
    )
    episode6 = tmp_path / "Airing - 06.mkv"
    episode6.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=79002,
            title="Airing",
            episode=6,
            video_path=episode6,
            state="ready",
        )
    )
    release = NyaaRelease(
        title="[Group] Airing - 07 [1080p]",
        link="",
        torrent_url="https://example.invalid/7.torrent",
        info_hash="7" * 40,
        size_text="1 GiB",
        size_bytes=1024**3,
        seeders=100,
        leechers=0,
        downloads=100,
        trusted=True,
        remake=False,
        score=100.0,
        group="Group",
    )
    searched: list[int] = []
    monkeypatch.setattr(
        manager,
        "search_releases",
        lambda _media_id, *, episode, batch, automatic=False: searched.append(episode) or [release],
    )
    monkeypatch.setattr(manager, "_release_is_allowed_for_auto", lambda _item: True)
    monkeypatch.setattr(manager, "add_release", lambda *args, **kwargs: release)

    assert manager.auto_search_current() == 1
    assert searched == [7]


def test_scheduled_agent_runs_due_subtitle_job_without_general_poll(monkeypatch, tmp_path: Path) -> None:
    now = time.time()
    calls: list[tuple[str, int | None]] = []

    class FakeDb:
        def get_state(self, key, default=""):
            assert key == "agent_last_run"
            return str(now)

        def subtitle_jobs(self):
            return [{"state": "pending", "next_check": now - 1}]

    class FakeManager:
        def __init__(self, _config):
            self.db = FakeDb()

        def anilist_refresh_due(self, *, now):
            return False

        def process_subtitle_jobs(self, *, limit):
            calls.append(("subs", limit))
            return 1

        def run_once(self):
            raise AssertionError("general maintenance must not be needed")

    config = SimpleNamespace(
        agent=SimpleNamespace(enabled=True, poll_minutes=60),
    )
    monkeypatch.setattr(agent, "load_config", lambda _path: config)
    monkeypatch.setattr(agent, "app_session_active", lambda: True)
    monkeypatch.setattr(agent, "app_session_window_active", lambda: False)
    monkeypatch.setattr(agent, "AnimeManager", FakeManager)
    monkeypatch.setattr(agent.time, "time", lambda: now)

    assert agent.main(["--scheduled", "--config", str(tmp_path / "config.toml")]) == 0
    assert calls == [("subs", 8)]


def test_couldnt_sync_raw_play_bypasses_resolver_and_uses_original_file(
    tmp_path: Path, monkeypatch
) -> None:
    api = make_api(tmp_path)
    video = (tmp_path / "episode.mkv").resolve()
    raw = (tmp_path / "raw-jimaku.srt").resolve()
    video.write_bytes(b"video")
    raw.write_text("1\n00:00:10,000 --> 00:00:11,000\n字幕\n", encoding="utf-8")
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=79003,
            title="Unsynced",
            episode=1,
            video_path=video,
            subtitle_path=raw,
            state="waiting_subtitles",
        )
    )
    api.manager.db.set_couldnt_sync_subtitle(video, raw, origin="jimaku")

    class FakeProcess:
        pid = 79003

        def poll(self):
            return None

    calls: list[list[str]] = []
    monkeypatch.setattr(
        "pudge.web_app.resolve_episode_subtitle",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("raw watch must bypass resolver")),
    )
    monkeypatch.setattr(
        "pudge.web_app.subprocess.Popen",
        lambda command, **kwargs: calls.append(command) or FakeProcess(),
    )

    result = api.play(str(video), allow_unsynced_subtitles=True)

    assert result["duplicate"] is False
    command = calls[0]
    assert command[command.index("--sub") + 1] == str(raw)
    assert "--no-sync" in command


def test_resume_within_two_minutes_has_no_rewind(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    video = (tmp_path / "resume.mkv").resolve()
    subtitle = (tmp_path / "resume.srt").resolve()
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n", encoding="utf-8")
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=79004,
            title="Resume",
            episode=1,
            video_path=video,
            subtitle_path=subtitle,
            state="ready",
        )
    )
    api.manager.db.record_playback(video, 321.5, 1400.0)

    class FakeProcess:
        pid = 79004

        def poll(self):
            return None

    calls: list[list[str]] = []
    monkeypatch.setattr(
        "pudge.web_app.subprocess.Popen",
        lambda command, **kwargs: calls.append(command) or FakeProcess(),
    )

    api.play(str(video), resume=True)

    command = calls[0]
    assert command[command.index("--start-at") + 1] == "321.500"


def test_active_mpv_registry_is_restored_after_webapp_restart(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    video = (tmp_path / "open.mkv").resolve()
    video.write_bytes(b"video")

    class FakeProcess:
        pid = 79123

        def poll(self):
            return None

    # Keep the mpv process mock scoped to api.play(). WebAppApi startup also uses
    # subprocess.run() for launchctl on macOS, and subprocess.run() internally
    # delegates to subprocess.Popen. Leaving this mock active would therefore
    # break unrelated scheduled-agent startup during the restart assertion.
    with monkeypatch.context() as mpv_patch:
        mpv_patch.setattr("pudge.web_app.subprocess.Popen", lambda *args, **kwargs: FakeProcess())
        api.play(str(video))

    monkeypatch.setattr("pudge.web_app.os.kill", lambda pid, signal: None)
    reopened = WebAppApi(api.config.config_path)
    active = reopened.get_state()["active_playbacks"]

    assert any(item["video_path"] == str(video) and item["pid"] == 79123 for item in active)
    html = (Path(__file__).parents[1] / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    assert "hydrateActivePlaybacks(ui.state)" in html
    assert "monitorPlay(path)" in html


def test_mpv_has_cmd_shift_l_episode_debug_export() -> None:
    root = Path(__file__).parents[1]
    lua = (root / "pudge" / "mpv_scripts" / "pudge_anilist.lua").read_text(encoding="utf-8")
    cli = (root / "pudge" / "cli.py").read_text(encoding="utf-8")
    assert "Meta+Shift+l" in lua
    assert "--export-episode-debug" in lua
    assert 'subprocess.run(["open", "-R", str(target)]' in cli
