from __future__ import annotations

import time
from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True)
    cfg.paths.cache_dir.mkdir(parents=True)
    return cfg


def test_interactive_requeue_preserves_active_processing_lease(tmp_path: Path) -> None:
    manager = AnimeManager(_cfg(tmp_path))
    video = tmp_path / "library" / "episode.mkv"
    video.write_bytes(b"x")
    assert manager.db.ensure_subtitle_job(video, 204466, 6)
    future = time.time() + 1200
    with manager.db.connect() as conn:
        conn.execute(
            "UPDATE subtitle_jobs SET state='processing',next_check=?,priority=0 WHERE video_path=?",
            (future, str(video)),
        )

    affected = manager.db.force_requeue_unresolved_subtitle_jobs(priority=200, recover_processing=False)
    assert affected == 1
    row = manager.db.subtitle_jobs()[0]
    assert row["state"] == "processing"
    assert float(row["next_check"]) == future
    assert int(row["priority"]) == 200


def test_blocking_requeue_can_still_recover_stale_processing_job(tmp_path: Path) -> None:
    manager = AnimeManager(_cfg(tmp_path))
    video = tmp_path / "library" / "episode.mkv"
    video.write_bytes(b"x")
    assert manager.db.ensure_subtitle_job(video, 204466, 6)
    with manager.db.connect() as conn:
        conn.execute(
            "UPDATE subtitle_jobs SET state='processing',next_check=? WHERE video_path=?",
            (time.time() + 1200, str(video)),
        )

    manager.db.force_requeue_unresolved_subtitle_jobs(priority=200, recover_processing=True)
    row = manager.db.subtitle_jobs()[0]
    assert row["state"] == "pending"
    assert float(row["next_check"]) <= time.time() + 1
    assert int(row["priority"]) == 200


def test_priority_job_count_tracks_manual_refresh_until_attempt(tmp_path: Path) -> None:
    manager = AnimeManager(_cfg(tmp_path))
    video = tmp_path / "library" / "episode.mkv"
    video.write_bytes(b"x")
    manager.db.ensure_subtitle_job(video, 204466, 6)
    manager.db.force_requeue_unresolved_subtitle_jobs(priority=200)
    assert manager.db.priority_subtitle_job_count(min_priority=200) == 1
    manager.db.postpone_subtitle_job(video, "attempted", 600)
    assert manager.db.priority_subtitle_job_count(min_priority=200) == 0


# Refresh queued a manual subtitle check (priority 200, due now) that the backend keeps working on.
REFRESH = r"""
const job = () => ({video: 'Show - 03.mkv', video_path: '/v/Show - 03.mkv', state: 'pending', priority: 200,
                    next_check: Date.now() / 1000, attempts: 0});
__api.refresh_local = async () => ({state: {...(await __api.get_state()), subtitle_jobs: [job()]}, stats: {subtitle_check_queued: 1}});
__api.poll_downloads_and_subtitles = async () => ({state: {...(await __api.get_state()), subtitle_jobs: [job()]}});
const button = () => ({disabled: $('refreshAll').disabled, label: $('refreshAll').innerText.trim()});
await __click('#refreshAll');
await __tick(50);
const out = {toast: __toasts().at(-1), status: __text('#status'), button: button(),
             texts: {queued: t('toast.refreshedSubtitlesQueued'), checking: t('status.subtitleCheckingBackground'),
                     refresh: t('action.refresh'), already: t('toast.alreadyRefreshing')}};
await __tick(1000);                       // the foreground worker checks the queued job
out.polls = __apiCalls.filter(call => call.name === 'poll_downloads_and_subtitles').length;
out.later = {status: __text('#status'), button: button()};
await __click('#refreshAll');             // a second Refresh is not refused
await __tick(50);
out.refreshes = __apiCalls.filter(call => call.name === 'refresh_local').length;
out.toasts = __toasts();
return out;
"""


def test_ui_keeps_background_subtitle_status_without_locking_refresh(page) -> None:
    for lang in ("en", "ru"):
        out = page.run(REFRESH, lang=lang).value
        texts = out["texts"]
        assert out["toast"] == texts["queued"]
        assert out["status"] == texts["checking"]
        assert out["button"] == {"disabled": False, "label": texts["refresh"]}
        assert out["polls"] >= 1
        assert out["later"] == {
            "status": texts["checking"],
            "button": {"disabled": False, "label": texts["refresh"]},
        }
        assert out["refreshes"] == 2 and texts["already"] not in out["toasts"]
