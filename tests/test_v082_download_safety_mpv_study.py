from __future__ import annotations

from pathlib import Path

import pytest

from pudge.config import AppConfig, load_config, write_config
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode
from pudge.providers.aria2 import Aria2Client, Aria2Error
from pudge.web_app import WebAppApi


def make_config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    return cfg


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = make_config(tmp_path)
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def test_ready_notification_skips_episode_after_nearest_unwatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path)
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(
        LibraryAnime(media_id=77, title="Example", status="CURRENT", progress=0, episodes=12)
    )
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "pudge.manager.send_native_notification",
        lambda title, message: calls.append((title, message)) or True,
    )

    manager._notify_ready_episode(
        video=tmp_path / "Example - 02.mkv", media_id=77, episode=2
    )
    assert calls == []
    assert manager.db.get_state("ready_notification:episode:77:2", "") == ""

    manager._notify_ready_episode(
        video=tmp_path / "Example - 01.mkv", media_id=77, episode=1
    )
    assert len(calls) == 1


def test_ready_notification_uses_media_episode_not_release_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path)
    manager = AnimeManager(cfg, log=lambda _message: None)
    anime = LibraryAnime(
        media_id=189046,
        title="Re:Zero 4",
        status="CURRENT",
        progress=11,
        episodes=19,
    )
    manager.db.upsert_anime(anime)
    calls: list[str] = []
    monkeypatch.setattr(
        "pudge.manager.send_native_notification",
        lambda _title, message: calls.append(message) or True,
    )
    manager._notify_ready_episode(
        video=tmp_path / "ReZero - 78.mkv", media_id=anime.media_id, episode=12
    )
    assert len(calls) == 1


def test_cleanup_uses_season_local_progress_for_absolute_episode(tmp_path: Path) -> None:
    manager = AnimeManager(make_config(tmp_path), log=lambda _message: None)
    video = tmp_path / "ReZero - 78.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=189046,
            title="Re:Zero 4",
            status="CURRENT",
            progress=11,
            episodes=19,
        )
    )
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=189046,
            title="Re:Zero 4",
            episode=12,
            media_episode=12,
            release_episode=78,
            video_path=video,
        )
    )
    manager.db.schedule_cleanup(
        video,
        24,
        list_status="CURRENT",
        media_id=189046,
        episode=None,
    )
    assert manager.db.get_anime(189046).progress == 12


def test_rating_waits_for_real_final_episode_with_absolute_numbering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = make_api(tmp_path)
    video = tmp_path / "ReZero - 79.mkv"
    video.write_bytes(b"video")
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=189046,
            title="Re:Zero 4",
            status="CURRENT",
            progress=13,
            episodes=19,
        )
    )
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=189046,
            title="Re:Zero 4",
            episode=13,
            media_episode=13,
            release_episode=79,
            video_path=video,
            state="watched",
        )
    )
    assert api.play_status(str(video))["final_episode"] is False


def test_planning_download_is_visible_in_planning_and_waiting(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=123,
            title="Planning Batch",
            status="PLANNING",
            episodes=12,
            media_status="FINISHED",
        )
    )
    api.manager.db.upsert_download(
        DownloadItem(
            torrent_hash="abc",
            name="Planning Batch [Batch]",
            state="active",
            progress=0.42,
            save_path=str(tmp_path / "downloads"),
            content_path=str(tmp_path / "downloads" / "batch"),
            media_id=123,
            is_batch=True,
            added_on=100,
            raw={"backend": "aria2", "total_size": 1000, "downloaded": 420, "dlspeed": 50},
        )
    )
    state = api.get_state_fast()
    assert state["planned"][0]["download"]["progress"] == pytest.approx(0.42)
    waiting = next(row for row in state["home"]["waiting"] if row["media_id"] == 123)
    assert waiting["download"]["state"] == "active"


def test_aria2_network_and_seeding_options_are_real_launch_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Aria2Client(
        state_dir=tmp_path / "aria2",
        seed_mode="ratio_or_time",
        seed_ratio=1.5,
        seed_time_minutes=90,
        upload_limit_kib=512,
        vpn_interface="utun9",
        vpn_kill_switch=True,
    )
    monkeypatch.setattr(client, "_interface_names", lambda: {"lo0", "utun9"})
    options = client._launch_options()
    assert "--no-conf=true" in options
    assert "--seed-ratio=1.5" in options
    assert "--seed-time=90.0" in options
    assert "--max-upload-limit=512K" in options
    assert "--interface=utun9" in options
    assert client.network_guard_status()["protected"] is True
    client.close()


def test_aria2_kill_switch_refuses_missing_vpn_interface(tmp_path: Path) -> None:
    client = Aria2Client(
        state_dir=tmp_path / "aria2",
        vpn_interface="utun9",
        vpn_kill_switch=True,
        auto_start=False,
    )
    client._interface_names = lambda: {"lo0"}  # type: ignore[method-assign]
    with pytest.raises(Aria2Error, match="заблокирован"):
        client.ensure_running()
    client.close()


def test_extended_torrent_and_subtitle_shortcut_config_round_trip(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.aria2.seed_mode = "ratio"
    cfg.aria2.seed_ratio = 2.0
    cfg.aria2.vpn_interface = "utun3"
    cfg.aria2.vpn_kill_switch = True
    cfg.shortcuts.mpv_translate_subtitle = "Ctrl+y"
    cfg.tools.mpv_study_plugin = "jpdb"
    write_config(cfg, cfg.config_path)
    loaded = load_config(cfg.config_path)
    assert loaded.aria2.seed_mode == "ratio"
    assert loaded.aria2.seed_ratio == 2.0
    assert loaded.aria2.vpn_interface == "utun3"
    assert loaded.aria2.vpn_kill_switch is True
    assert loaded.shortcuts.mpv_translate_subtitle == "Ctrl+y"
    assert loaded.tools.mpv_study_plugin == "jpdb"


def test_mpv_script_translates_in_player_with_long_context() -> None:
    source = (Path(__file__).parents[1] / "pudge" / "mpv_scripts" / "pudge_anilist.lua").read_text(
        encoding="utf-8"
    )
    assert "PUDGE_SHORTCUT_TRANSLATE_SUBTITLE" in source
    assert "'--subtitle-study'," not in source
    assert "open_subtitle_study" not in source
    assert "--subtitle-translate" in source
    assert "PUDGE_TRANSLATION_JSON" in source
    assert "create_osd_overlay('ass-events')" in source
    assert "mp.get_property('sub-text'" in source
    assert "study_subtitle_history" in source
    assert "Previous Japanese subtitles:" in source
    assert "secondary-sub-text" in source
    assert "--subtitle-prewarm-file" in source
    assert "playback_only = true" in source


def test_download_center_has_native_filters_ratio_and_vpn_state() -> None:
    page = (Path(__file__).parents[1] / "pudge" / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "torrentFilterMatch" in page
    assert "network-guard" in page
    assert "aria2_vpn_kill_switch" in page
    assert "s_mpv_study_plugin" in page
    assert "torrent-reconnect" in page
    assert "'Ratio'" in page
