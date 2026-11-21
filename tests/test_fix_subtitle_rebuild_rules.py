"""Subtitle rebuild rules after an aligner fix (pudge/subtitle_migrations.py).

- A rebuild kept no subtitles meanwhile: the old working file was cleared first, and a failed
  rebuild (Jimaku outage, stricter validation) left the episode without any (MGR-4).
- Watched and dropped episodes went back to Waiting and then Ready, so Home showed them as
  new again (MGR-5).
- Only exact algorithm strings matched: v6.0-v6.19 selections with the same defect stayed (ARCH-1).
- The 17 passes ran whenever any episode row changed (a playback heartbeat), each one reading
  and decoding the latest history row of every episode (MGR-8/ARCH-4).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from pudge import subtitle_migrations
from pudge.i18n import err
from pudge.manager_models import LibraryEpisode
from subtitle_rebuild_support import assert_not_prepared_again, assert_rebuilding_with_subtitles_kept
from t71g08_support import subtitle_library

ENGINE = "embedded-reference+timeline"
BRIDGE = [
    {"kind": "stable", "offset_seconds": -1.0, "support": 23},
    {"kind": "transition_bridge", "offset_seconds": -5.0, "support": 52},
    {"kind": "stable", "offset_seconds": -4.0, "support": 1},
]
OPENING_GAP = {
    "applied": False,
    "reason": "later_clock_not_clearly_better",
    "gap_seconds": 100.0,
    "current_offset_seconds": -31.5,
    "candidate_offset_seconds": -41.75,
    "candidate_support": 34,
}

# One recorded selection per rule, shaped like the defect it rebuilds.
SAMPLES: dict[str, tuple[str, dict[str, object]]] = {
    "_requeue_false_positive_tail_clocks": (
        "jimaku",
        {
            "timeline_segments": [
                {"offset_seconds": 0.0, "support": 8},
                {"offset_seconds": 20.0, "support": 1},
            ],
            "timeline_weak_tail_guard": {"boundary_ratio": 0.9},
        },
    ),
    "_requeue_decreasing_gap_timeline_upgrade": (
        "jimaku",
        {
            "timeline_algorithm": "timeline-v6.16-tail-clock-duration-anchors",
            "timeline_monotonic_refinements": [
                {
                    "applied": True,
                    "reason": "decreasing_boundary_extended_for_monotonicity",
                    "boundary_index": 1,
                    "old_source_time": 100.0,
                    "new_source_time": 112.0,
                    "left_offset_seconds": 5.0,
                    "right_offset_seconds": 0.0,
                }
            ],
        },
    ),
    "_requeue_local_edge_timeline_upgrade": (
        "jimaku",
        {
            "timeline_algorithm": "timeline-v6.17-decreasing-gap-anchor",
            "timeline_early_edit_audio_verification": {"required": False, "cold_start_delta_seconds": 8.5},
        },
    ),
    "_requeue_shared_opening_timeline_upgrade": (
        "jimaku",
        {
            "engine": ENGINE,
            "timeline_algorithm": "timeline-v6.19-edge-zones-audio-verify",
            "timeline_opening_gap_reacquire": OPENING_GAP,
        },
    ),
    "_requeue_early_crossover_timeline_upgrade": (
        "jimaku",
        {
            "engine": ENGINE,
            "timeline_algorithm": "timeline-v6.20-shared-opening-anchors",
            "timeline_boundaries": [
                {"source_time": 952.5, "refinement": {"method": "fixed_offset_crossover"}}
            ],
        },
    ),
    "_requeue_bridged_singleton_tail_timeline_upgrade": (
        "jimaku",
        {
            "engine": ENGINE,
            "timeline_algorithm": "timeline-v6.21-agreed-crossover-fine-offset",
            "timeline_segments": BRIDGE,
        },
    ),
    "_requeue_speech_maps_without_late_cut": (
        "jimaku",
        {
            "selection_reason": "early_edit_japanese_speech_verification",
            "embedded_timeline_attempt": {
                "timeline_boundaries": [
                    {"source_time": 1022.5, "left_offset_seconds": -5.0, "right_offset_seconds": 1.5}
                ]
            },
        },
    ),
    "_requeue_unverified_timeline_edits": ("jimaku", {"unverified_timeline_edit": True}),
    "_requeue_stt_timeline_clock_conflicts": (
        "jimaku",
        {"embedded_opening_clock_scaffold": {"reason_detail": "speech_clock_disagrees_with_post_plateau"}},
    ),
    "_requeue_stt_opening_plateau_semantic_upgrade": (
        "jimaku",
        {
            "engine": "japanese-stt+alass+opening-plateau",
            "stt_opening_plateau_refinement": {"applied": True, "gap_seconds": 60.0},
        },
    ),
    "_requeue_unsafe_stt_transition_maps": (
        "jimaku",
        {
            "engine": "japanese-stt+alass",
            "stt_alass_transition_safety": {
                "accepted": False,
                "transitions": [{"jump_seconds": 30.0, "gap_supported": False}],
            },
        },
    ),
    "_requeue_opening_gap_timeline_upgrade": (
        "jimaku",
        {
            "timeline_algorithm": "timeline-v5.9-early-edit-speech-gate",
            "timeline_early_edit_audio_verification": {"reasons": ["opening_gap_clock_ambiguity"]},
        },
    ),
    "_requeue_large_cold_open_subtitles": (
        "jimaku",
        {"timeline_cold_start": {"reason": "edge_hint_not_local", "delta_seconds": 17.0}},
    ),
}


def _select(manager, video: Path, alignment: dict[str, object], *, source: str = "jimaku") -> int:
    return manager.db.record_subtitle_history(
        video_path=video,
        media_id=5,
        episode=1,
        source=source,
        candidate_name="old.srt",
        candidate_path=manager.db.episode_by_path(video).subtitle_path,
        status="selected",
        details={"alignment": alignment},
    )


def _episode(
    manager, tmp_path: Path, name: str, alignment: dict[str, object], *, state: str = "ready"
) -> Path:
    video = (manager.config.library.root_dir / f"{name}.mkv").resolve()
    video.write_bytes(b"video")
    subtitle = (tmp_path / f"{name}.srt").resolve()
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n古い\n", encoding="utf-8")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=5,
            title="Show",
            episode=len(name),
            video_path=video,
            subtitle_path=subtitle,
            subtitle_origin="jimaku",
            state=state,
        )
    )
    _select(manager, video, alignment)
    return video


def _failing_validation(monkeypatch, commands: list[list[str]]) -> None:
    """A definitive failure (a passing one, such as a Jimaku timeout, retries later)."""
    import subprocess
    from types import SimpleNamespace

    import pudge.manager as manager_module

    class Preparation:
        def __init__(self, command, **_kwargs):
            commands.append([str(part) for part in command])
            self.returncode = 1

        def poll(self):
            return self.returncode

        def communicate(self, *_args, **_kwargs):
            return "Error: all subtitle candidates failed synchronization/validation\n", ""

    monkeypatch.setattr(
        manager_module, "subprocess", SimpleNamespace(**{**vars(subprocess), "Popen": Preparation})
    )


@pytest.fixture
def stt_available(monkeypatch):
    monkeypatch.setattr("pudge.subtitles.stt.japanese_stt_available", lambda **_kwargs: True)


# --- MGR-4: the subtitles stay playable while they are rebuilt, and after a failure ----------


@pytest.mark.parametrize("method", sorted(SAMPLES))
def test_every_rule_keeps_the_subtitles_while_rebuilding_and_after_a_failure(
    tmp_path: Path, monkeypatch, stt_available, method: str
) -> None:
    manager, video, _commands = subtitle_library(tmp_path, monkeypatch)
    source, alignment = SAMPLES[method]
    _select(manager, video, alignment, source=source)

    assert getattr(manager, method)() == 1
    assert_rebuilding_with_subtitles_kept(manager, video)
    # Maintenance keeps the playable selection and the job of the rebuild.
    paths = manager._active_subtitle_upgrade_paths()
    manager.db.repair_spurious_ready_subtitle_jobs(preserve_paths=paths)
    manager.db.repair_stale_subtitle_selections(preserve_paths=paths)
    assert_rebuilding_with_subtitles_kept(manager, video)

    commands: list[list[str]] = []
    _failing_validation(monkeypatch, commands)  # the new aligner rejects every candidate
    manager.process_subtitle_jobs(limit=1)
    [command] = commands
    assert "--force-search" in command and "--resync" in command  # a real rebuild, not the cache
    episode = manager.db.episode_by_path(video)
    assert episode.state == "ready" and episode.subtitle_path.name == "old.srt"
    assert episode.subtitle_path.read_text(encoding="utf-8").endswith("古い\n")
    # The rebuild is over: no job, no forced rebuild on the next check.
    assert manager.db.subtitle_jobs() == []
    assert manager.db.get_state(manager._subtitle_upgrade_state_key(video), "") == ""
    assert manager.db.get_state(manager._subtitle_force_rebuild_state_key(video), "") == ""


def test_a_rebuild_takes_the_new_subtitles_without_a_score_comparison(tmp_path: Path, monkeypatch) -> None:
    manager, video, commands = subtitle_library(tmp_path, monkeypatch)
    _select(manager, video, SAMPLES["_requeue_bridged_singleton_tail_timeline_upgrade"][1])
    assert manager._requeue_bridged_singleton_tail_timeline_upgrade() == 1

    assert manager.process_subtitle_jobs(limit=1) == 1
    episode = manager.db.episode_by_path(video)
    assert episode.state == "ready" and episode.subtitle_path.read_text(encoding="utf-8").endswith("新しい\n")
    assert manager.db.latest_selected_subtitle(video)["candidate_name"] == "new.srt"
    assert manager.db.subtitle_jobs() == []


def test_a_pending_upgrade_check_becomes_the_rebuild_and_a_rebuild_is_not_queued_twice(
    tmp_path: Path, monkeypatch
) -> None:
    manager, video, _commands = subtitle_library(tmp_path, monkeypatch)
    item = manager.db.episode_by_path(video)
    assert manager._keep_subtitles_during_rebuild(item, fresh=False)  # an ordinary upgrade check
    manager.db.queue_subtitle_job(video, 5, 1, priority=120, error="upgrade")
    [running] = manager.db.claim_due_subtitle_jobs(1)

    _select(manager, video, SAMPLES["_requeue_shared_opening_timeline_upgrade"][1])
    assert manager._requeue_shared_opening_timeline_upgrade() == 1
    assert_rebuilding_with_subtitles_kept(manager, video)
    # The check that was running does not commit its comparison any more.
    assert not manager.db.subtitle_job_owned(video, running["generation"], running["owner_token"])

    # Another rule matching the same old selection does not queue it again.
    manager.db.set_state("subtitle_tail_clock_guard_generation", "")
    alignment = {
        **SAMPLES["_requeue_shared_opening_timeline_upgrade"][1],
        **SAMPLES["_requeue_false_positive_tail_clocks"][1],
    }
    _select(manager, video, alignment)
    assert manager._requeue_false_positive_tail_clocks() == 0


def test_a_throttled_sync_update_rebuild_stays_ready_while_it_waits(tmp_path: Path, monkeypatch) -> None:
    manager, video, _commands = subtitle_library(tmp_path, monkeypatch)
    waiting = (manager.config.library.root_dir / "Show - 02.mkv").resolve()
    waiting.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(media_id=5, title="Show", episode=2, video_path=waiting, state="waiting_subtitles")
    )
    manager.db.queue_subtitle_job(waiting, 5, 2)
    assert manager._requeue_rebuild(manager.db.episode_by_path(video), err("jobs.requeueSyncUpdate"))

    jobs = manager.db.claim_due_subtitle_jobs(2)
    runnable = manager._throttle_sync_update_requeues(jobs)
    assert [Path(str(job["video_path"])) for job in runnable] == [waiting]  # throttling as before
    episode = manager.db.episode_by_path(video)
    assert episode.state == "ready" and episode.subtitle_path.name == "old.srt"
    [deferred] = [job for job in manager.db.subtitle_jobs() if Path(str(job["video_path"])) == video]
    assert deferred["state"] == "pending" and float(deferred["next_check"]) > time.time() + 60


# --- MGR-5: watched and dropped episodes keep their state ----------------------------------


def test_a_watched_episode_is_rebuilt_but_stays_watched(tmp_path: Path, monkeypatch) -> None:
    manager, video, commands = subtitle_library(tmp_path, monkeypatch)
    manager.db.schedule_cleanup(video, 24 * 365)
    watched_at = manager.db.episode_by_path(video).watched_at
    _select(manager, video, SAMPLES["_requeue_shared_opening_timeline_upgrade"][1])

    assert manager._requeue_shared_opening_timeline_upgrade() == 1
    assert_rebuilding_with_subtitles_kept(manager, video, state="watched")
    assert manager.process_subtitle_jobs(limit=1) == 1
    episode = manager.db.episode_by_path(video)
    assert episode.state == "watched" and episode.watched_at == watched_at
    assert episode.subtitle_path.read_text(encoding="utf-8").endswith("新しい\n")


def test_unwatched_only_rules_and_dropped_episodes_are_left_alone(tmp_path: Path, monkeypatch) -> None:
    manager, video, _commands = subtitle_library(tmp_path, monkeypatch)
    manager.db.schedule_cleanup(video, 24 * 365)
    dropped = _episode(manager, tmp_path, "dropped", SAMPLES["_requeue_shared_opening_timeline_upgrade"][1])
    with manager.db.connect() as conn:
        conn.execute("UPDATE episodes SET state='dropped' WHERE video_path=?", (str(dropped),))
    _select(manager, video, SAMPLES["_requeue_bridged_singleton_tail_timeline_upgrade"][1])

    assert manager._requeue_bridged_singleton_tail_timeline_upgrade() == 0  # not for watched ones
    assert manager._requeue_shared_opening_timeline_upgrade() == 0
    assert manager.db.episode_by_path(video).state == "watched"
    assert manager.db.episode_by_path(dropped).state == "dropped"
    assert manager.db.subtitle_jobs() == []


@pytest.mark.parametrize("state", ["watched", "dropped"])
def test_job_writes_keep_watched_and_dropped_states(tmp_path: Path, monkeypatch, state: str) -> None:
    manager, video, _commands = subtitle_library(tmp_path, monkeypatch)
    db = manager.db

    def reset() -> None:
        with db.connect() as conn:
            conn.execute(
                "UPDATE episodes SET state=?,subtitle_path=? WHERE video_path=?",
                (state, str(tmp_path / "old.srt"), str(video)),
            )
        db.queue_subtitle_job(video, 5, 1)

    for write in (
        lambda: db.defer_subtitle_job(video, "later", 60),
        lambda: db.postpone_subtitle_job(video, "later", 60),
        lambda: db.mark_subtitle_job_needs_action(video, "key", "jimaku_key"),
        lambda: db.repair_stale_subtitle_selections(),
        lambda: db.invalidate_subtitle(video, 5, 1, "rebuild"),
    ):
        reset()
        write()
        assert db.episode_by_path(video).state == state


def test_a_deferred_upgrade_check_keeps_its_ready_episode_ready(tmp_path: Path, monkeypatch) -> None:
    manager, video, _commands = subtitle_library(tmp_path, monkeypatch)
    db = manager.db
    db.queue_subtitle_job(video, 5, 1)
    for write in (
        lambda: db.defer_subtitle_job(video, "later", 60),
        lambda: db.postpone_subtitle_job(video, "later", 60),
        lambda: db.mark_subtitle_job_needs_action(video, "key", "jimaku_key"),
    ):
        write()
        episode = db.episode_by_path(video)
        assert episode.state == "ready" and episode.subtitle_path.name == "old.srt"
    # Without a selection the episode is waiting, as before.
    db.clear_subtitle_selection(video)
    db.queue_subtitle_job(video, 5, 1)
    db.defer_subtitle_job(video, "later", 60)
    assert db.episode_by_path(video).state == "waiting_subtitles"


# --- ARCH-1: older algorithm versions with the same defect --------------------------------


def test_timeline_versions_are_compared_as_numbers() -> None:
    version = subtitle_migrations.timeline_version
    assert version({"timeline_algorithm": "timeline-v6.9-cold-overlap-verification"}) == (6, 9)
    assert version({"timeline_algorithm": "timeline-v6.21-agreed-crossover-fine-offset"}) == (6, 21)
    assert version({"timeline_algorithm": "timeline-v5.9"}) == (5, 9)
    assert version({"timeline_algorithm": "alass"}) is None and version({}) is None
    assert (6, 9) < (6, 20)


def test_old_versions_with_a_bridged_one_window_tail_are_rebuilt(tmp_path: Path, monkeypatch) -> None:
    manager, _video, _commands = subtitle_library(tmp_path, monkeypatch)
    affected = [
        _episode(
            manager,
            tmp_path,
            name,
            {"engine": ENGINE, "timeline_algorithm": algorithm, "timeline_segments": BRIDGE},
        )
        for name, algorithm in (
            ("v6_0", "timeline-v6.0-opening-gap-reacquire"),
            ("v6_14", "timeline-v6.14-opening-preclock-holdout"),
            ("v6_19", "timeline-v6.19-edge-zones-audio-verify"),
            ("v6_20", "timeline-v6.20-shared-opening-anchors"),
        )
    ]
    fixed = _episode(
        manager,
        tmp_path,
        "v6_22",
        {
            "engine": ENGINE,
            "timeline_algorithm": "timeline-v6.22-bridged-singleton-tail",
            "timeline_segments": BRIDGE,
        },
    )
    assert manager._requeue_bridged_singleton_tail_timeline_upgrade() == 4
    for video in affected:
        assert_rebuilding_with_subtitles_kept(manager, video)
    assert_not_prepared_again(manager, fixed)


def test_old_versions_with_the_same_weak_opening_clock_are_rebuilt(tmp_path: Path, monkeypatch) -> None:
    manager, _video, _commands = subtitle_library(tmp_path, monkeypatch)
    old = _episode(
        manager,
        tmp_path,
        "v6_14",
        {
            "engine": ENGINE,
            "timeline_algorithm": "timeline-v6.14-opening-preclock-holdout",
            # Before v6.19 only with a later clock that fits the minute after the opening better.
            "timeline_opening_gap_reacquire": {
                **OPENING_GAP,
                "activity_gain": 0.0073,
                "mean_error_gain_seconds": 0.2834,
            },
        },
    )
    fixed = _episode(
        manager,
        tmp_path,
        "v6_20",
        {
            "engine": ENGINE,
            "timeline_algorithm": "timeline-v6.20-shared-opening-anchors",
            "timeline_opening_gap_reacquire": OPENING_GAP,
        },
    )
    assert manager._requeue_shared_opening_timeline_upgrade() == 1
    assert_rebuilding_with_subtitles_kept(manager, old)
    assert_not_prepared_again(manager, fixed)


def _kurumi_alignment(tmp_path: Path) -> dict[str, object]:
    from pudge.subtitles.timeline_alignment import align_subtitle_timelines
    from test_fix_timeline_late_crossover_and_tail_offset import FIXTURE, _srt

    data = json.loads(FIXTURE.read_text())
    source = _srt(tmp_path / "kurumi-source.srt", data["source"], "ja")
    reference = _srt(tmp_path / "kurumi-reference.srt", data["reference"], "en")
    _out, result = align_subtitle_timelines(source, reference, tmp_path / "kurumi-cache", force=True)
    return json.loads(json.dumps(result, default=str))  # as the history stores it


def test_before_v6_20_only_a_crossover_far_from_its_estimate_is_rebuilt(tmp_path: Path, monkeypatch) -> None:
    manager, _video, _commands = subtitle_library(tmp_path, monkeypatch)
    # FX Senshi Kurumi-chan E03 as v6.0-v6.20 recorded it: the first local crossover, 77 s
    # before the cost-based estimate of the same boundary.
    old_boundary = {
        "source_time": 952.5,
        "left_offset_seconds": -5.0,
        "right_offset_seconds": 2.0,
        "jump_seconds": 7.0,
        "refinement": {
            "method": "fixed_offset_crossover",
            "fallback_source_time": 1029.76,
            "last_left_center": 952.0,
            "first_right_center": 953.0,
        },
    }
    late = _episode(
        manager,
        tmp_path,
        "v6_14_late",
        {
            "engine": ENGINE,
            "timeline_algorithm": "timeline-v6.14-opening-preclock-holdout",
            "timeline_boundaries": [old_boundary],
        },
    )
    # The boundary the fixed aligner finds for the same episode, if an old version had found it.
    current = _kurumi_alignment(tmp_path)
    _opening, boundary = current["timeline_boundaries"]  # the cold open's cut, then the late one
    assert boundary["refinement"]["method"] == "fixed_offset_crossover"
    agreeing = _episode(
        manager,
        tmp_path,
        "v6_14_agreeing",
        {**current, "timeline_algorithm": "timeline-v6.14-opening-preclock-holdout"},
    )
    no_estimate = _episode(
        manager,
        tmp_path,
        "v6_19_bare",
        {
            "engine": ENGINE,
            "timeline_algorithm": "timeline-v6.19-edge-zones-audio-verify",
            "timeline_boundaries": [
                {"source_time": 952.5, "refinement": {"method": "fixed_offset_crossover"}}
            ],
        },
    )
    assert manager._requeue_early_crossover_timeline_upgrade() == 1
    assert_rebuilding_with_subtitles_kept(manager, late)
    assert_not_prepared_again(manager, agreeing)
    assert_not_prepared_again(manager, no_estimate)


# --- MGR-8 / ARCH-4: one pass, completion per rule -----------------------------------------


def _count_row_reads(manager, monkeypatch) -> list[int]:
    calls: list[int] = []
    original = manager.db.latest_selected_subtitle_rows

    def rows(*, after_id: int = 0, **kwargs):
        calls.append(after_id)
        return original(after_id=after_id, **kwargs)

    monkeypatch.setattr(manager.db, "latest_selected_subtitle_rows", rows)
    return calls


def test_one_pass_records_every_rule_and_later_passes_read_only_new_selections(
    tmp_path: Path, monkeypatch, stt_available
) -> None:
    manager, video, _commands = subtitle_library(tmp_path, monkeypatch)
    # The late-audio rule runs only once it can probe (also on a runner without FFmpeg).
    monkeypatch.setattr(subtitle_migrations, "_configured_ffprobe", lambda _manager: "/missing/ffprobe")
    _select(manager, video, SAMPLES["_requeue_bridged_singleton_tail_timeline_upgrade"][1])
    plain = _episode(
        manager,
        tmp_path,
        "plain",
        # A current selection (v6.24): older timeline selections are prepared again once by
        # the "precise_clocks" rule, which would count this one too.
        {"engine": ENGINE, "timeline_algorithm": "timeline-v6.24-precise-clocks"},
    )
    reads = _count_row_reads(manager, monkeypatch)

    assert manager._run_subtitle_rebuild_rules() == {
        **{rule.id: 0 for rule in subtitle_migrations.RULES},
        "bridged_singleton_tail": 1,
    }
    assert reads == [0]  # one query for every rule
    top = manager.db.subtitle_history_max_id()
    for rule in subtitle_migrations.RULES:
        expected = rule.generation if rule.kind == "generation" else f"{rule.generation}:{top}"
        assert manager.db.get_state(rule.progress_key, "") == expected, rule.id

    # Playback heartbeats and refreshes change episode rows: nothing to read.
    manager.db.record_playback(plain, 300.0, 1420.0)
    assert manager._run_subtitle_rebuild_rules() == {}
    assert reads == [0]

    # A new selection is read alone (the rules that look at new selections only).
    newer = _select(manager, plain, SAMPLES["_requeue_unverified_timeline_edits"][1])
    assert manager._run_subtitle_rebuild_rules() == {
        rule.id: int(rule.id == "unverified_timeline_edit")
        for rule in subtitle_migrations.RULES
        if rule.kind == "selection"
    }
    assert reads == [0, top]
    assert_rebuilding_with_subtitles_kept(manager, plain)
    assert manager.db.get_state("subtitle_rebuild_rule:unverified_timeline_edit", "") == f"1:{newer}"
    assert manager._run_subtitle_rebuild_rules() == {}

    # A restored database whose history is older than what was seen: everything again.
    manager.db.set_state("subtitle_rebuild_rule:shared_opening", "1:999999")
    manager._run_subtitle_rebuild_rules()
    assert reads[-1] == 0


def test_maintenance_runs_the_rules_without_a_fingerprint_gate(
    tmp_path: Path, monkeypatch, stt_available
) -> None:
    from test_v0730_acquisition_safety import manager_for

    manager = manager_for(tmp_path)
    for name in (
        "_repair_brand_moved_subtitle_selections",
        "invalidate_disabled_ocr_subtitles",
        "cleanup_duplicate_torrents",
        "schedule_subtitle_upgrades",
        "_clear_jimaku_api_cache",
        "auto_search_current",
        "refresh_anilist_if_due",
        "auto_upgrade_downloaded",
        "finalize_ready_upgrades",
        "reconcile_duplicate_versions",
        "cleanup",
        "enforce_disk_limit",
        "run_intro_detection",
        "enforce_derived_caches",
        "maintain_database_storage",
    ):
        monkeypatch.setattr(manager, name, lambda *args, **kwargs: 0)
    monkeypatch.setattr(manager, "_sync_downloads_for_stats", lambda stats: None)
    monkeypatch.setattr(manager, "scan_library", lambda **_kwargs: [])
    monkeypatch.setattr(manager, "scan_subtitle_inbox", lambda: {})
    monkeypatch.setattr(manager, "repair_library_if_due", lambda: {})
    monkeypatch.setattr(manager, "cleanup_qbittorrent_tags", lambda: {})
    monkeypatch.setattr(manager, "process_subtitle_jobs", lambda **_kwargs: 0)
    reads = _count_row_reads(manager, monkeypatch)

    manager._run_once_unlocked(force_subtitle_retry=True)
    manager._run_once_unlocked(force_subtitle_retry=True)  # Refresh: nothing pending, nothing read
    assert reads == [0]
    assert manager.db.get_state("legacy_subtitle_repair:v32", "") == ""


def test_the_registry_covers_every_rule_method_with_translated_reasons(monkeypatch) -> None:
    from types import SimpleNamespace

    from pudge.i18n import t
    from pudge.manager import AnimeManager

    ids = [rule.id for rule in subtitle_migrations.RULES]
    assert len(ids) == len(set(ids))
    called: list[str] = []
    manager = AnimeManager.__new__(AnimeManager)
    manager.db = SimpleNamespace()
    for name in dir(AnimeManager):
        if not name.startswith("_requeue_") or name in {
            "_requeue_rebuild",
            "_requeue_legacy_generated_subtitles",
            "_requeue_after_resolver_upgrade",
        }:
            continue
        assert getattr(getattr(AnimeManager, name), "__wrapped__", None) is not None, name  # one connection
        monkeypatch.setattr(
            subtitle_migrations,
            "run_rule",
            lambda _manager, rule_id: called.append(rule_id) or 0,
        )
        getattr(manager, name)()
    assert sorted(called) == sorted(ids)
    for rule in subtitle_migrations.RULES:
        for lang in ("en", "ru"):
            assert not t(f"error.{rule.reason}", lang=lang).startswith("error."), (rule.id, lang)


def test_the_integrity_repair_keeps_the_job_of_a_rebuild_and_drops_other_ready_jobs(
    tmp_path: Path, monkeypatch
) -> None:
    manager, video, _commands = subtitle_library(tmp_path, monkeypatch)
    assert manager._requeue_rebuild(
        manager.db.episode_by_path(video), err("jobs.requeueSyncUpdate"), priority=120
    )
    spurious = _episode(manager, tmp_path, "spurious", {})
    manager.db.queue_subtitle_job(spurious, 5, 8)  # an ordinary job left on a Ready row
    monkeypatch.setattr(manager, "scan_library", lambda **_kwargs: [])

    result = manager.repair_library_integrity(automatic=True, scan=False)
    assert result["spurious_jobs"] == 1
    assert_rebuilding_with_subtitles_kept(manager, video)
    [job] = manager.db.subtitle_jobs()
    assert Path(str(job["video_path"])) == video
    assert int(job["priority"]) == 120 and err("jobs.requeueSyncUpdate") in str(job["last_error"])
    episode = manager.db.episode_by_path(spurious)
    assert episode.state == "ready" and episode.subtitle_path is not None


# --- precise clocks (sync benchmark, 2026-10) ------------------------------------------------


@pytest.mark.parametrize(
    ("alignment", "details", "expected"),
    [
        (
            {"engine": ENGINE, "timeline_algorithm": "timeline-v6.23-line-evidence-tails-small-edits"},
            {},
            True,
        ),
        ({"engine": ENGINE}, {}, True),  # saved before versions were recorded: stale once
        ({"engine": ENGINE, "timeline_algorithm": "timeline-v6.24-precise-clocks"}, {}, False),
        ({"engine": "alass"}, {"language_purity": "mixed_japanese_english"}, True),
        ({"engine": "alass"}, {"language_purity": "japanese_only"}, False),
    ],
)
def test_window_grid_clocks_and_mixed_language_files_are_prepared_again_once(
    tmp_path: Path, monkeypatch, alignment: dict, details: dict, expected: bool
) -> None:
    manager, video, _commands = subtitle_library(tmp_path, monkeypatch)
    manager.db.record_subtitle_history(
        video_path=video,
        media_id=5,
        episode=1,
        source="jimaku",
        candidate_name="old.srt",
        candidate_path=manager.db.episode_by_path(video).subtitle_path,
        status="selected",
        details={"alignment": alignment, "details": details},
    )
    assert manager._requeue_imprecise_timeline_clocks() == int(expected)
    assert manager._requeue_imprecise_timeline_clocks() == 0  # once


def test_manual_selections_are_never_prepared_again_for_precise_clocks(tmp_path: Path, monkeypatch) -> None:
    manager, video, _commands = subtitle_library(tmp_path, monkeypatch)
    _select(manager, video, {"engine": ENGINE, "timeline_algorithm": "timeline-v6.20-x"}, source="manual")
    assert manager._requeue_imprecise_timeline_clocks() == 0
