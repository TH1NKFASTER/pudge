"""Refresh, launch and the subtitle inbox requeue subtitle jobs without undoing other work.

- A file that waits for the person's answer (an extra file of a download: Delete or Keep)
  stayed in Action required only until the next Refresh or launch, which turned it back
  into pending (priority 200, "Manual refresh") and counted another attempt each time.
- A launch or an inbox change took the jobs another process (the agent) was preparing
  right then: both prepared the same episode and one result was thrown away.
- Every launch made every waiting job due at once and dropped the Jimaku answers, so
  titles without Jimaku subtitles were prepared again on each launch, past their backoff.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryEpisode
from test_fix_batch_extra_files import _batch, _no_prepare, _no_subtitles, _ova_job, _series
from test_web_app import make_api


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True)
    cfg.anilist.enabled = False
    return AnimeManager(cfg, log=lambda _message: None)


def _waiting(manager: AnimeManager, tmp_path: Path, name: str = "Show - 05.mkv") -> Path:
    video = (tmp_path / name).resolve()
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(media_id=None, title="Show", episode=5, video_path=video, state="waiting_subtitles")
    )
    manager.db.queue_subtitle_job(video, None, 5)
    return video


def test_refresh_keeps_extra_file_in_action_required(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    db = api.manager.db
    _series(db)
    _no_subtitles(monkeypatch)
    folder, item = _batch(tmp_path, ["[SubsPlease] Dorohedoro - OVA (1080p) [63C766E6].mkv"])
    api.manager._register_completed_download(item)
    ova = (folder / "[SubsPlease] Dorohedoro - OVA (1080p) [63C766E6].mkv").resolve()
    before = _ova_job(api, ova)
    assert (before["state"], before["action_code"]) == ("needs_action", "review_extra_file")

    for _ in range(3):
        db.force_requeue_unresolved_subtitle_jobs(priority=200, recover_processing=False)
        db.force_requeue_unresolved_subtitle_jobs(priority=20)  # launch, subtitle inbox
        job = _ova_job(api, ova)
        assert (job["state"], job["action_code"]) == ("needs_action", "review_extra_file")
        assert int(job["attempts"]) == int(before["attempts"])
        assert int(job["priority"]) == 0
        cards = api.get_state()["home"]["needs_action"]
        assert [card["action_code"] for card in cards] == ["review_extra_file"]
        _no_prepare(monkeypatch)
        api.manager.process_subtitle_jobs(limit=5, preferred_paths=[ova], wait_for_slot=True)
    assert int(_ova_job(api, ova)["attempts"]) == int(before["attempts"])


def test_refresh_still_retries_settings_actions_and_resets_stale_progress(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    db = manager.db
    jimaku = _waiting(manager, tmp_path, "Jimaku - 05.mkv")
    kept = _waiting(manager, tmp_path, "Kept - 05.mkv")
    with db.connect() as conn:
        conn.execute(
            "UPDATE subtitle_jobs SET state='needs_action',action_code='configure_jimaku',progress_json=? "
            "WHERE video_path=?",
            ('{"candidates": 38}', str(jimaku)),
        )
        conn.execute(
            "UPDATE subtitle_jobs SET state='needs_action',action_code='extra_file_kept' WHERE video_path=?",
            (str(kept),),
        )

    assert db.force_requeue_unresolved_subtitle_jobs(priority=200) == 1
    jobs = {Path(row["video_path"]).name: row for row in db.subtitle_jobs()}
    # A key may have been set since: retried, without the progress of an old run.
    assert (jobs["Jimaku - 05.mkv"]["state"], jobs["Jimaku - 05.mkv"]["action_code"]) == ("pending", "")
    assert json.loads(jobs["Jimaku - 05.mkv"]["progress_json"]) == {}
    assert (jobs["Kept - 05.mkv"]["state"], jobs["Kept - 05.mkv"]["action_code"]) == (
        "needs_action",
        "extra_file_kept",
    )


def test_launch_and_inbox_requeue_leave_a_live_processing_job_to_its_worker(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    db = manager.db
    video = _waiting(manager, tmp_path)
    [agent_job] = db.claim_due_subtitle_jobs(1)  # the agent is preparing it now

    db.force_requeue_unresolved_subtitle_jobs(priority=200)  # launch (recovers processing rows)
    db.force_requeue_unresolved_subtitle_jobs()  # a subtitle inbox change
    [row] = db.subtitle_jobs()
    assert row["state"] == "processing"
    assert int(row["priority"]) == 200  # the requested check is still shown as queued
    assert db.claim_due_subtitle_jobs(1) == []
    assert db.subtitle_job_owned(video, int(agent_job["generation"]), str(agent_job["owner_token"]))


def test_recovery_takes_a_processing_job_whose_lease_ran_out(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    db = manager.db
    _waiting(manager, tmp_path)
    db.claim_due_subtitle_jobs(1)
    with db.connect() as conn:  # the worker was killed 30 minutes ago
        conn.execute(
            "UPDATE subtitle_jobs SET lease_until=?,next_check=?", (time.time() - 5, time.time() - 5)
        )

    db.force_requeue_unresolved_subtitle_jobs(priority=200)
    [row] = db.subtitle_jobs()
    assert (row["state"], int(row["priority"])) == ("pending", 200)
    assert len(db.claim_due_subtitle_jobs(1)) == 1


def test_refresh_without_recovery_never_flips_a_processing_row_through_its_episode(tmp_path: Path) -> None:
    # The episode row of a job being prepared is waiting too: the "reconstruct missing
    # jobs" part of the requeue used to turn the processing row into pending.
    manager = _manager(tmp_path)
    db = manager.db
    _waiting(manager, tmp_path)
    db.claim_due_subtitle_jobs(1)
    db.force_requeue_unresolved_subtitle_jobs(priority=200, recover_processing=False)
    assert db.subtitle_jobs()[0]["state"] == "processing"


def test_launch_is_a_normal_pass_and_keeps_backoff_and_jimaku_answers(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    manager.config.agent.enabled = True
    video = tmp_path / "Show - 03.mkv"
    video.write_bytes(b"video")
    manager.db.queue_subtitle_job(video, 7, 3, delay_seconds=6 * 3600)
    manager.db.set_state("subtitle_resolver_generation", "9")  # no one-time resolver migration due
    cached = manager.config.paths.cache_dir / "jimaku-api" / ("0f" * 16 + ".json")
    cached.parent.mkdir(parents=True)
    cached.write_text("[]", encoding="utf-8")
    for name in (
        "sync_downloads",
        "cleanup_duplicate_torrents",
        "auto_upgrade_downloaded",
        "finalize_ready_upgrades",
        "reconcile_duplicate_versions",
        "cleanup",
        "enforce_disk_limit",
        "poll_release_feeds",
        "refresh_anilist_if_due",
        "auto_search_current",
    ):
        monkeypatch.setattr(manager, name, lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(manager, "scan_library", lambda **_kwargs: [])
    processed: list[int] = []
    monkeypatch.setattr(manager, "process_subtitle_jobs", lambda *a, **k: processed.append(1) or 0)

    stats = manager.run_startup_once()

    [job] = manager.db.subtitle_jobs()
    assert (job["state"], int(job["priority"])) == ("pending", 0)
    assert float(job["next_check"]) > time.time() + 5 * 3600  # still on its 6 h backoff
    assert cached.stat().st_mtime > 1  # Jimaku answers stay fresh
    assert processed == [] and stats["subs"] == 0
    assert stats["subtitle_check_queued"] == 0

    # Refresh is the explicit "check now": it still makes the job due.
    manager.run_interactive_refresh()
    [job] = manager.db.subtitle_jobs()
    assert (job["state"], int(job["priority"])) == ("pending", 200)
    assert cached.stat().st_mtime <= 1


def test_a_new_inbox_subtitle_retries_a_failing_job_but_not_an_extra_file(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    db = manager.db
    failing = _waiting(manager, tmp_path, "Failing - 05.mkv")
    extra = _waiting(manager, tmp_path, "Extra - OVA.mkv")
    with db.connect() as conn:
        conn.execute(
            "UPDATE subtitle_jobs SET state='needs_action',action_code='subtitle_retry_exhausted' WHERE video_path=?",
            (str(failing),),
        )
        conn.execute(
            "UPDATE subtitle_jobs SET state='needs_action',action_code='review_extra_file' WHERE video_path=?",
            (str(extra),),
        )
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    manager.config.paths.subtitle_dirs = [inbox]
    manager.scan_subtitle_inbox()
    (inbox / "Failing - 05.ja.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nはい\n", encoding="utf-8")
    assert manager.scan_subtitle_inbox()["changed"] is True
    jobs = {Path(row["video_path"]).name: (row["state"], row["action_code"]) for row in db.subtitle_jobs()}
    assert jobs == {
        "Failing - 05.mkv": ("pending", ""),
        "Extra - OVA.mkv": ("needs_action", "review_extra_file"),
    }
    # A Refresh, though, leaves the failing job to the person (its card has Retry).
    with db.connect() as conn:
        conn.execute(
            "UPDATE subtitle_jobs SET state='needs_action',action_code='subtitle_retry_exhausted' WHERE video_path=?",
            (str(failing),),
        )
    db.force_requeue_unresolved_subtitle_jobs(priority=200)
    assert {row["state"] for row in db.subtitle_jobs()} == {"needs_action"}
