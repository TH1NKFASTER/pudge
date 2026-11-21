"""Rebuild rules after an update: passing failures, broad old shapes, queue order.

- A rule's rebuild that failed for a passing reason (Jimaku timed out or rate limited, no
  network, a lookup cut short by a Jimaku error, the 20-minute timeout, a worker that did
  not start) ended for good: the old subtitles came back, the request was cleared and the
  rule never looked at the episode again. A lookup cut short could also commit a rebuild
  from part of the candidates.
- The shared-opening rule, widened to v6.0-v6.19, matched ordinary right two-clock fits
  (watched ones too): a big library was prepared again for hours.
- Rebuild jobs went ahead of new downloads: the restore pass re-queued every request at
  120 as "Checking subtitle upgrade", and a paused rebuild came back at 260, above manual
  retries and Refresh, counted as a check the person was waiting for.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from pudge.i18n import err
from pudge.manager import SUBTITLE_FAILURE_STREAK_CAP
from pudge.manager_models import LibraryEpisode
from subtitle_rebuild_support import assert_not_prepared_again, assert_rebuilding_with_subtitles_kept
from t71g08_support import subtitle_library

ENGINE = "embedded-reference+timeline"
BRIDGED = {
    "engine": ENGINE,
    "timeline_algorithm": "timeline-v6.21-agreed-crossover-fine-offset",
    "timeline_segments": [
        {"kind": "stable", "offset_seconds": -1.0, "support": 23},
        {"kind": "transition_bridge", "offset_seconds": -5.0, "support": 52},
        {"kind": "stable", "offset_seconds": -4.0, "support": 1},
    ],
}
LATE_CUT = {
    "selection_reason": "early_edit_japanese_speech_verification",
    "embedded_timeline_attempt": {
        "timeline_boundaries": [
            {"source_time": 1022.5, "left_offset_seconds": -5.0, "right_offset_seconds": 1.5}
        ]
    },
}


class _Run:
    """A prepare worker: exits at once with ``result``; without one it runs until stopped."""

    instances: list["_Run"] = []
    result: tuple[int, str, str] | None = None
    start_error: OSError | None = None
    on_poll = None

    def __init__(self, command, **_kwargs):
        if _Run.start_error is not None:
            raise _Run.start_error
        self.command = [str(part) for part in command]
        self.returncode = None if _Run.result is None else _Run.result[0]
        self.polls = 0
        self.terminated = False
        _Run.instances.append(self)

    def poll(self):
        self.polls += 1
        if _Run.on_poll is not None:
            _Run.on_poll(self)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return self.returncode

    def communicate(self, *_args, **_kwargs):
        return ("", "") if _Run.result is None else (_Run.result[1], _Run.result[2])


@pytest.fixture
def library(tmp_path: Path, monkeypatch):
    manager, video, _commands = subtitle_library(tmp_path, monkeypatch)
    _Run.instances, _Run.result, _Run.start_error, _Run.on_poll = [], None, None, None
    monkeypatch.setattr("pudge.manager.subprocess.Popen", _Run)
    monkeypatch.setattr("pudge.manager.time.sleep", lambda *_a: None)
    return manager, video


def _select(manager, video: Path, alignment: dict[str, object]) -> None:
    manager.db.record_subtitle_history(
        video_path=video,
        media_id=5,
        episode=1,
        source="jimaku",
        candidate_name="old.srt",
        candidate_path=manager.db.episode_by_path(video).subtitle_path,
        status="selected",
        details={"alignment": alignment},
    )


def _job(manager, video: Path):
    return next(row for row in manager.db.subtitle_jobs() if Path(str(row["video_path"])) == video)


def _due_again(manager, video: Path) -> None:
    with manager.db.connect() as conn:
        conn.execute("UPDATE subtitle_jobs SET next_check=0 WHERE video_path=?", (str(video),))


def _request(manager, video: Path) -> dict:
    return json.loads(manager.db.get_state(manager._subtitle_upgrade_state_key(video), "") or "{}")


def _new_srt(tmp_path: Path) -> Path:
    return (tmp_path / "new.srt").resolve()


def _ready_output(tmp_path: Path, prefix: str = "") -> str:
    return (
        f"{prefix}PREPARED_SUBTITLE={_new_srt(tmp_path)}\nPREPARE_STATUS=ready\n"
        'PREPARED_SUBTITLE_META={"source":"jimaku","name":"new.srt"}\n'
    )


PASSING_FAILURES = {
    "jimaku_timed_out": (1, "Anime: 'Show' episode=1\n", "Error: Jimaku API error: timed out"),
    "offline": (1, "", "httpx.ConnectError: [Errno 8] nodename nor servname provided, or not known"),
    "rate_limited": (0, "Jimaku rate limited (429); retry in 5 min\nPREPARE_STATUS=waiting_subtitles\n", ""),
    "not_found_after_a_jimaku_error": (
        0,
        "Jimaku API error: HTTP 503\nPREPARE_STATUS=waiting_subtitles\n",
        "",
    ),
    "lookup_cut_short_but_ready": None,  # built with the new.srt path
    "worker_did_not_start": "start",
    "twenty_minute_timeout": "timeout",
}


def _fail(monkeypatch, tmp_path: Path, kind: str) -> None:
    failure = PASSING_FAILURES[kind]
    _Run.result, _Run.start_error, _Run.on_poll = None, None, None
    if kind == "lookup_cut_short_but_ready":
        _Run.result = (0, _ready_output(tmp_path, "Jimaku API error: timed out\n"), "")
    elif failure == "start":
        _Run.start_error = OSError(24, "Too many open files")
    elif failure == "timeout":
        real_monotonic = time.monotonic
        clock = {"offset": 0.0}
        monkeypatch.setattr("pudge.manager.time.monotonic", lambda: real_monotonic() + clock["offset"])
        _Run.on_poll = lambda _process: clock.__setitem__("offset", clock["offset"] + 11 * 60.0)
    else:
        _Run.result = failure


@pytest.mark.parametrize("kind", sorted(PASSING_FAILURES))
def test_a_rule_rebuild_that_fails_for_a_passing_reason_keeps_its_request_and_runs_later(
    library, tmp_path: Path, monkeypatch, kind: str
) -> None:
    manager, video = library
    _select(manager, video, LATE_CUT)
    assert manager._requeue_speech_maps_without_late_cut() == 1
    _fail(monkeypatch, tmp_path, kind)

    started = time.time()
    manager.process_subtitle_jobs(limit=1)
    episode = manager.db.episode_by_path(video)
    assert episode.state == "ready" and episode.subtitle_path.read_text(encoding="utf-8").endswith("古い\n")
    assert manager.db.latest_selected_subtitle(video)["candidate_name"] == "old.srt"  # nothing committed
    request = _request(manager, video)
    assert request.get("fresh_rebuild") is True and request.get("rebuild_rule") is True
    assert manager.db.get_state(manager._subtitle_force_rebuild_state_key(video), "") == "1"
    job = _job(manager, video)
    poll = manager.config.agent.subtitle_poll_minutes * 60.0
    assert job["state"] == "pending" and float(job["next_check"]) >= started + poll - 5
    assert int(job["priority"]) == 120 and str(job["last_error"]) == err("jobs.requeueSpeechLateCut")


def test_the_same_passing_failure_waits_longer_each_time_and_ends_at_the_cap(
    library, tmp_path: Path, monkeypatch
) -> None:
    manager, video = library
    _select(manager, video, BRIDGED)
    assert manager._requeue_bridged_singleton_tail_timeline_upgrade() == 1
    _fail(monkeypatch, tmp_path, "jimaku_timed_out")
    poll = manager.config.agent.subtitle_poll_minutes * 60.0
    delays = []
    for _ in range(SUBTITLE_FAILURE_STREAK_CAP - 1):
        _due_again(manager, video)
        started = time.time()
        manager.process_subtitle_jobs(limit=1)
        delays.append(float(_job(manager, video)["next_check"]) - started)
    assert [round(delay / poll) for delay in delays] == [2**n for n in range(SUBTITLE_FAILURE_STREAK_CAP - 1)]

    _due_again(manager, video)
    manager.process_subtitle_jobs(limit=1)  # the same failure once more: the rebuild ends
    episode = manager.db.episode_by_path(video)
    assert episode.state == "ready" and episode.subtitle_path.name == "old.srt"
    assert manager.db.subtitle_jobs() == [] and _request(manager, video) == {}


def test_a_definitive_failure_still_ends_the_rule_rebuild(library, tmp_path: Path) -> None:
    manager, video = library
    _select(manager, video, BRIDGED)
    assert manager._requeue_bridged_singleton_tail_timeline_upgrade() == 1
    _Run.result = (1, "Error: all subtitle candidates failed synchronization/validation\n", "")
    manager.process_subtitle_jobs(limit=1)
    episode = manager.db.episode_by_path(video)
    assert episode.state == "ready" and episode.subtitle_path.name == "old.srt"
    assert manager.db.subtitle_jobs() == [] and _request(manager, video) == {}
    assert manager.db.get_state(manager._subtitle_force_rebuild_state_key(video), "") == ""


def test_a_rule_rebuild_takes_a_complete_result(library, tmp_path: Path) -> None:
    manager, video = library
    _select(manager, video, BRIDGED)
    assert manager._requeue_bridged_singleton_tail_timeline_upgrade() == 1
    _Run.result = (0, _ready_output(tmp_path), "")
    assert manager.process_subtitle_jobs(limit=1) == 1
    assert manager.db.episode_by_path(video).subtitle_path == _new_srt(tmp_path)
    assert manager.db.subtitle_jobs() == [] and _request(manager, video) == {}


def test_a_fresh_rebuild_the_person_asked_for_never_takes_a_lookup_cut_short(library, tmp_path: Path) -> None:
    manager, video = library
    manager.force_fresh_subtitle_selection(video)
    _Run.result = (0, _ready_output(tmp_path, "Jimaku API error: timed out\n"), "")
    manager.process_subtitle_jobs(limit=1)
    episode = manager.db.episode_by_path(video)
    assert episode.state == "ready" and episode.subtitle_path.name == "old.srt"
    # As before for the person's own rebuild: it ends, the old subtitles stay.
    assert manager.db.subtitle_jobs() == [] and _request(manager, video) == {}


# --- shared opening: v6.19 as before, older versions only with evidence and unwatched -----

OPENING_GAP = {
    "applied": False,
    "reason": "later_clock_not_clearly_better",
    "gap_seconds": 100.93,
    "current_offset_seconds": -31.5,
    "candidate_offset_seconds": -41.75,
    "candidate_support": 34,
}
# Re:Zero E19 measured by the aligner: the later clock fits the minute after the opening better.
REZERO_GAINS = {"activity_gain": 0.0073, "mean_error_gain_seconds": 0.2834}


def _episode(manager, tmp_path: Path, name: str, algorithm: str, gap: dict, *, state: str = "ready") -> Path:
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
    if state == "watched":
        manager.db.schedule_cleanup(video, 24 * 365)
    _select(
        manager,
        video,
        {"engine": ENGINE, "timeline_algorithm": algorithm, "timeline_opening_gap_reacquire": gap},
    )
    return video


def test_older_shared_opening_fits_are_rebuilt_only_unwatched_and_with_a_better_later_clock(
    library, tmp_path: Path
) -> None:
    manager, _video = library
    v614 = "timeline-v6.14-opening-preclock-holdout"
    rezero_like = _episode(manager, tmp_path, "rezero_like", v614, {**OPENING_GAP, **REZERO_GAINS})
    watched = _episode(
        manager, tmp_path, "watched_old", v614, {**OPENING_GAP, **REZERO_GAINS}, state="watched"
    )
    # Right fits of the v6.14 fuzz: the later clock starts minutes after the opening.
    worse = _episode(
        manager,
        tmp_path,
        "worse",
        v614,
        {**OPENING_GAP, "activity_gain": -0.1312, "mean_error_gain_seconds": -0.3156},
    )
    mixed = _episode(
        manager,
        tmp_path,
        "mixed",
        v614,
        {**OPENING_GAP, "activity_gain": 0.37, "mean_error_gain_seconds": -0.1136},
    )
    no_gains = _episode(manager, tmp_path, "no_gains", v614, OPENING_GAP)
    v619_watched = _episode(
        manager,
        tmp_path,
        "v619_watched",
        "timeline-v6.19-edge-zones-audio-verify",
        OPENING_GAP,
        state="watched",
    )
    fixed = _episode(manager, tmp_path, "v620", "timeline-v6.20-shared-opening-anchors", OPENING_GAP)

    assert manager._requeue_shared_opening_timeline_upgrade() == 2
    assert_rebuilding_with_subtitles_kept(manager, rezero_like)
    assert_rebuilding_with_subtitles_kept(manager, v619_watched, state="watched")  # v6.19 as before
    for video in (watched, worse, mixed, no_gains, fixed):
        assert_not_prepared_again(manager, video)
    assert manager._requeue_shared_opening_timeline_upgrade() == 0


# --- queue order: rebuilds behind new downloads and the person's checks --------------------


def test_the_restore_pass_keeps_a_waiting_job_and_requeues_a_lost_one_with_its_own_priority(library) -> None:
    manager, video = library
    _select(manager, video, BRIDGED)
    assert manager._requeue_bridged_singleton_tail_timeline_upgrade() == 1
    manager.db.defer_subtitle_job(video, err("jobs.requeueSyncUpdate"), 600)
    before = dict(_job(manager, video))

    manager._restore_interrupted_subtitle_upgrades()
    after = dict(_job(manager, video))
    assert {key: after[key] for key in ("priority", "last_error", "next_check", "state")} == {
        key: before[key] for key in ("priority", "last_error", "next_check", "state")
    }

    manager.db.delete_subtitle_job(video)  # lost (an older repair pass)
    manager._restore_interrupted_subtitle_upgrades()
    job = _job(manager, video)
    assert int(job["priority"]) == 0 and str(job["last_error"]) == err("jobs.requeueBridgedTail")


def test_a_due_upgrade_check_keeps_its_priority_and_reason(library) -> None:
    manager, video = library
    manager.config.jimaku.api_key = "key"
    assert manager.schedule_subtitle_upgrades(force=True) == 1
    request = _request(manager, video)
    assert request.get("requeue_priority") == 180
    manager.db.delete_subtitle_job(video)
    manager._restore_interrupted_subtitle_upgrades()
    job = _job(manager, video)
    assert int(job["priority"]) == 180 and str(job["last_error"]) == err("jobs.checkingUpgrade")


def test_a_paused_rule_rebuild_comes_back_below_new_downloads_and_the_persons_checks(
    library, monkeypatch
) -> None:
    manager, video = library
    _select(manager, video, BRIDGED)
    assert manager._requeue_bridged_singleton_tail_timeline_upgrade() == 1
    monkeypatch.setattr(
        manager.work_scheduler,
        "background_wait_reason",
        lambda *_a, **_k: "foreground" if _Run.instances else None,
    )
    manager.process_subtitle_jobs(limit=1)
    assert _Run.instances and _Run.instances[0].terminated  # playback started
    job = _job(manager, video)
    assert int(job["priority"]) == 0 and str(job["last_error"]) == err("jobs.requeueBridgedTail")
    assert manager.db.priority_subtitle_job_count(min_priority=200) == 0
    assert_rebuilding_with_subtitles_kept(manager, video)


def test_a_paused_fresh_rebuild_the_person_asked_for_keeps_its_high_priority(library, monkeypatch) -> None:
    manager, video = library
    manager.force_fresh_subtitle_selection(video)
    monkeypatch.setattr(
        manager.work_scheduler,
        "background_wait_reason",
        lambda *_a, **_k: "foreground" if _Run.instances else None,
    )
    manager.process_subtitle_jobs(limit=1)
    assert _Run.instances and _Run.instances[0].terminated  # playback started
    assert int(_job(manager, video)["priority"]) == 260
