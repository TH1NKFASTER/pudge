from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig, write_config
from pudge.web_app import WebAppApi
from tests.i18n_support import ru


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def test_foreground_poll_reports_manual_subtitle_queue_blocked_by_playback(
    tmp_path: Path, monkeypatch
) -> None:
    api = make_api(tmp_path)
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    api.manager.db.queue_subtitle_job(video, 123, 1, priority=260)

    monkeypatch.setattr(api.manager, "sync_downloads", lambda: 0)
    monkeypatch.setattr(api.manager, "scan_subtitle_inbox", lambda: {"requeued": 0})
    monkeypatch.setattr(api.manager, "cleanup_qbittorrent_tags", lambda: {})
    monkeypatch.setattr(api.manager.work_scheduler, "background_allowed", lambda: False)
    monkeypatch.setattr(
        api.manager,
        "process_subtitle_jobs",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("blocked playback must not invoke subtitle processing")
        ),
    )
    api.manager._last_missing_episode_rows = 0

    result = api._poll_downloads_and_subtitles_sync()

    assert result["skipped"] is False
    assert result["stats"]["subtitle_waiting_for_foreground"] == 1
    assert result["stats"]["subtitle_check_queued"] == 1
    assert result["stats"]["subs"] == 0


# A manual subtitle check (priority >= 200) that is due now, for the waiting episode on Home.
JOB = {"video": "/v/1.mkv", "state": "pending", "priority": 260, "next_check": 0, "attempts": 0}
SHOW = {
    "media_id": 1,
    "title": "Waiting Show",
    "next_episode": 3,
    "site_url": "https://anilist.co/anime/1",
    "local": {"state": "waiting_subtitles", "episode": 3, "video_path": "/v/1.mkv"},
}
POLLS = (
    "const polls = () => __apiCalls.filter(c => c.name === 'poll_downloads_and_subtitles').map(c => c.at);"
)


def test_subtitle_checks_held_by_playback_are_explained_and_polled_slower(page) -> None:
    # While a video plays, the backend keeps due subtitle checks for later; the window used to
    # poll every second and show "checking subtitles" although nothing was checked.
    result = page.run(
        POLLS
        + r"""
        await __tick(20000);
        const blocked = {polls: polls(), status: __text('#status')};
        window.BLOCKED = false; await __tick(5000);
        const free = {polls: polls().slice(blocked.polls.length), status: __text('#status')};
        await __setHidden(true); await __tick(130000);
        return {blocked, free, hidden: polls().slice(blocked.polls.length + free.polls.length),
                texts: {waiting: t('status.subtitleWaitingPlayback'), checking: t('status.subtitleCheckingBackground')}};
        """,
        state={"subtitle_jobs": [JOB], "home": {"waiting": [SHOW]}, "current": [SHOW]},
        before="window.BLOCKED = true; __api.poll_downloads_and_subtitles = async () => ({stats: {subtitle_waiting_for_foreground: window.BLOCKED ? 1 : 0}});",
        api={"startup_maintenance": {"running": False}},
    )
    out, texts = result.value, result.value["texts"]
    assert out["blocked"] == {"polls": [1000, 6000, 11000, 16000], "status": texts["waiting"]}
    assert out["free"] == {"polls": [21000, 22000, 23000, 24000, 25000], "status": texts["checking"]}
    assert out["hidden"] == [85000, 145000]  # in the background: once a minute


# Refresh answers the same Home, only the countdown to the next episode moved on (countdowns
# are left out of the "did Home change" signature, so only a forced redraw shows them).
AIRING = {
    "media_id": 2,
    "title": "Airing Show",
    "next_airing_episode": 4,
    "remaining_seconds": 7200,
    "site_url": "https://anilist.co/anime/2",
}
REFRESH = r"""
__api.refresh_local = async () => { const state = await __api.get_state();
  return {state: {...state, subtitle_jobs: [JOB], home: {...state.home, caught_up: [{...AIRING, remaining_seconds: 3600}]}},
          stats: {subtitle_waiting_for_foreground: window.BLOCKED}}; };
""".replace("JOB", str(JOB).replace("'", '"')).replace("AIRING", str(AIRING).replace("'", '"'))


def test_refresh_redraws_home_and_says_when_new_subtitle_checks_wait_for_playback(page) -> None:
    for blocked, next_poll in ((1, 5000), (0, 500)):
        result = page.run(
            POLLS
            + r"""
            setPage('current'); await __tick(20000);                    // the start-up checks are over
            const airing = () => __text('#currentContent [data-media-id="2"] .airing-state');
            const out = {before: airing()};
            const before = polls().length;
            await __click('#refreshAll'); await __tick(10);
            Object.assign(out, {after: airing(), status: __text('#status'), toasts: __toasts()});
            await __tick(6000);
            out.nextPoll = polls()[before] - __apiCalls.find(c => c.name === 'refresh_local').at;
            out.texts = {waiting: t('status.subtitleWaitingPlayback'), checking: t('status.subtitleCheckingBackground'),
                         queued: t('toast.refreshedSubtitlesQueued'), hour: formatRemaining(3600), twoHours: formatRemaining(7200)};
            return out;
            """,
            state={"home": {"caught_up": [AIRING]}, "current": [AIRING]},
            before=f"window.BLOCKED = {blocked};" + REFRESH,
            api={
                "startup_maintenance": {"running": False},
                "poll_downloads_and_subtitles": {"stats": {"subtitle_waiting_for_foreground": blocked}},
            },
        )
        out, texts = result.value, result.value["texts"]
        assert texts["twoHours"] in out["before"] and texts["hour"] in out["after"]
        assert out["status"] == (texts["waiting"] if blocked else texts["checking"])
        assert texts["queued"] in out["toasts"]
        assert out["nextPoll"] == next_poll
    assert ru("status.subtitleWaitingPlayback") == "Субтитры — после просмотра"
