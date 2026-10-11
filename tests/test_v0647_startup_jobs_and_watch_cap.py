"""Startup runs the Refresh pipeline; the "watched" cap (85 %, at most 10 minutes left) is
fixed, not a setting, and reaches the mpv tracker (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import time
from pathlib import Path

from pudge.config import AppConfig, load_config
from pudge.models import AniListAnime
from pudge.web_app import WebAppApi
from tests.i18n_support import en, ru
from tests.lua_harness import ANILIST, needs_lua, run_mpv_scenario


def _maintenance(tmp_path: Path, monkeypatch, run: str) -> dict:
    """One maintenance pass (`run_startup_once` or `run_interactive_refresh`) with the
    background agent on, a subtitle job delayed by six hours and a cached Jimaku answer."""
    from pudge.manager import AnimeManager

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.agent.enabled = True
    manager = AnimeManager(cfg, log=lambda _message: None)
    video = tmp_path / "Show - 03.mkv"
    video.write_bytes(b"video")
    manager.db.queue_subtitle_job(video, 7, 3, delay_seconds=6 * 3600)
    cached = cfg.paths.cache_dir / "jimaku-api" / "search.json"
    cached.parent.mkdir(parents=True)
    cached.write_text("{}", encoding="utf-8")
    steps: list[str] = []
    for name in (
        "sync_downloads",
        "cleanup_duplicate_torrents",
        "auto_upgrade_downloaded",
        "finalize_ready_upgrades",
        "reconcile_duplicate_versions",
        "cleanup",
        "enforce_disk_limit",
        "poll_release_feeds",
    ):
        monkeypatch.setattr(manager, name, lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(manager, "scan_library", lambda **_kwargs: steps.append("scan") or [])
    monkeypatch.setattr(manager, "refresh_anilist_if_due", lambda: steps.append("anilist") or 0)
    monkeypatch.setattr(manager, "auto_search_current", lambda: steps.append("missing releases") or 0)
    monkeypatch.setattr(
        manager, "process_subtitle_jobs", lambda *_args, **_kwargs: steps.append("subtitles") or 0
    )
    stats = getattr(manager, run)()
    [job] = manager.db.subtitle_jobs()
    return {
        "steps": steps,
        "subs": stats.get("subs"),
        "queued": stats.get("subtitle_check_queued"),
        "job": (job["state"], int(job["priority"]), float(job["next_check"]) <= time.time() + 1),
        "jimaku_cache_kept": cached.exists(),
    }


def test_startup_uses_same_forced_subtitle_retry_pipeline_as_refresh(tmp_path: Path, monkeypatch) -> None:
    # Startup used to leave delayed subtitle jobs alone while the background agent was on.
    startup = _maintenance(tmp_path / "startup", monkeypatch, "run_startup_once")
    refresh = _maintenance(tmp_path / "refresh", monkeypatch, "run_interactive_refresh")
    assert startup == refresh
    # The delayed job is due now with the manual-refresh priority; Jimaku answers are fetched fresh.
    assert startup["job"] == ("pending", 200, True) and startup["queued"] == 1
    assert startup["jimaku_cache_kept"] is False
    # New releases are searched first; subtitles are left to the foreground worker.
    assert startup["steps"] == ["scan", "missing releases", "anilist"]
    assert startup["subs"] == 0


SETTINGS = """
const out = {fields: ['s_anilist_threshold', 's_anilist_max_remaining'].filter(id => document.getElementById(id)),
             hint: __text('#settings-anilist-auto-fields p'),
             expected: PudgeI18n.t('ui.episodeIsMarkedWatchedOnce', {shortcut: '⌃W'})};
await __click('#saveSettings');
await __tick(10);
return out;
"""


def test_watch_cap_is_fixed_and_hidden_in_web_settings(page) -> None:
    assert "просмотрено не меньше 85%" in ru("ui.episodeIsMarkedWatchedOnce")
    assert "Episode is marked watched once you've viewed at least 85% and 10 minutes or less remain" in en(
        "ui.episodeIsMarkedWatchedOnce"
    )
    for lang in ("en", "ru"):
        result = page.run(
            SETTINGS,
            lang=lang,
            # An old config with its own cap and the configured "mark watched" shortcut.
            state={
                "settings": {
                    "anilist_enabled": True,
                    "anilist_auto_progress": True,
                    "anilist_threshold": 70,
                    "anilist_max_remaining_minutes": 30,
                    "shortcut_mpv_mark_watched": "Ctrl+w",
                }
            },
            before="__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});",
        )
        assert result.value["fields"] == []  # nothing to edit
        assert result.value["hint"] == result.value["expected"]  # the fixed rule, with the real shortcut
        [[values]] = result.calls_of("save_settings")
        assert (values["anilist_threshold"], values["anilist_max_remaining_minutes"]) == (85, 10)


def _tracker_env(tmp_path: Path, monkeypatch, configure=None) -> dict[str, str]:
    """The environment cli.process_video gives mpv for a tracked episode (AniList on)."""
    from pudge.cli import build_parser, process_video
    from pudge.pipeline_cache import save_final_pipeline_result

    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.database_path = tmp_path / "library.sqlite"
    cfg.anilist.enabled = True
    cfg.anilist.access_token = "token"
    cfg.llm.enabled = False
    if configure:
        configure(cfg)
    video, subtitle = tmp_path / "Show - 05.mkv", tmp_path / "show.srt"
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n字幕\n", encoding="utf-8")
    save_final_pipeline_result(
        video, cfg, subtitle=subtitle, subtitle_id=None, dependency=subtitle, source="external"
    )
    anime = AniListAnime(id=7, titles=["Show"], synonyms=[], season_year=2026, episodes=12, format="TV")
    monkeypatch.setattr("pudge.cli._resolve_tracking_anilist", lambda *_args, **_kwargs: (anime, "show"))
    launched: list[dict[str, str]] = []
    monkeypatch.setattr(
        "pudge.cli.run_mpv", lambda _command, **kwargs: launched.append(kwargs["env_overrides"]) or 0
    )
    assert process_video(video, build_parser().parse_args([str(video)]), cfg, None) == 0
    return launched[0]


# An episode is counted once 85 % is watched AND at most 10 minutes remain.
COUNTING = """
local function counted() return #sim.commands('%-%-anilist%-action update') end
-- A 24-minute episode: 85 % leaves 3.6 minutes, so 85 % decides.
sim.set('duration', 1440); sim.set('time-pos', 1200); sim.fire('file-loaded')
sim.play(20); assert(counted() == 0, 'counted below 85 %')
sim.play(10); assert(counted() == 1, 'not counted at 85 %')
-- A two-hour film: at 85 % 18 minutes remain, so the last 10 minutes decide.
sim.set('duration', 7200); sim.set('time-pos', 6100); sim.fire('file-loaded')
sim.play(480); assert(counted() == 1, 'counted with more than 10 minutes left')
sim.play(30); assert(counted() == 2, 'not counted with less than 10 minutes left')
"""


def test_watch_cap_is_sent_to_mpv_tracker(tmp_path: Path, monkeypatch) -> None:
    env = _tracker_env(tmp_path, monkeypatch)
    assert (env["PUDGE_ANILIST_THRESHOLD"], env["PUDGE_ANILIST_MAX_REMAINING_MINUTES"]) == ("0.85", "10.0")
    # The cap is not configurable: an old config's own values are ignored.
    path = tmp_path / "config.toml"
    path.write_text(
        "[anilist]\nwatched_threshold = 0.5\nwatched_max_remaining_minutes = 45\n", encoding="utf-8"
    )
    loaded = load_config(path)
    assert (loaded.anilist.watched_threshold, loaded.anilist.watched_max_remaining_minutes) == (0.85, 10.0)


@needs_lua
def test_lua_requires_both_percent_and_remaining_time(tmp_path: Path, monkeypatch) -> None:
    run_mpv_scenario(tmp_path, COUNTING, scripts=[ANILIST], env=_tracker_env(tmp_path, monkeypatch))


def test_web_settings_round_trip_watch_cap(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    api = WebAppApi.__new__(WebAppApi)
    api.config = cfg
    api.manager = type("Manager", (), {"config": cfg})()

    # _settings_payload only needs self.config.
    payload = WebAppApi._settings_payload(api)
    assert payload["anilist_max_remaining_minutes"] == 10.0
