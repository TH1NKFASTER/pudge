"""FX Senshi Kurumi-chan E03: no subtitles after a fresh rebuild, and lines 6 s early after 17:02.

User: "what is with the subtitles for FX Senshi?" A fresh rebuild dropped the working
subtitles first; Jimaku then timed out twice (and its cached answers had been deleted too),
so the episode played without any. And the subtitles it had were the early-edit speech map
(0 s before the OP, -4.67 s after it) used for the whole episode, while the timeline against
the embedded subtitles also had a cut at 17:02 (source 1022.5 s, -5.0 s -> +1.5 s): every line
after it came 6 s early.

Fixture: cue timings only (start, end) of the two subtitle files.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import httpx

from pudge.providers.jimaku import JimakuClient
from pudge.subtitle_formats import parse_srt
from pudge.subtitles.timeline_alignment import align_subtitle_timelines
from pudge.syncing import _keep_late_timeline_cut_after_speech
from t71g08_support import subtitle_library
from test_fix_timeline_late_crossover_and_tail_offset import FIXTURE, _srt


def _failing_preparation(monkeypatch, commands: list[list[str]]) -> None:
    import subprocess

    import pudge.manager as manager_module

    class Preparation:
        def __init__(self, command, **_kwargs):
            commands.append([str(part) for part in command])
            self.returncode = 1

        def poll(self):
            return self.returncode

        def communicate(self, *_args, **_kwargs):
            return "Anime: 'Show' episode=1\n", "Error: Jimaku API error: timed out"

    monkeypatch.setattr(
        manager_module, "subprocess", SimpleNamespace(**{**vars(subprocess), "Popen": Preparation})
    )


def test_a_failed_fresh_rebuild_keeps_the_subtitles_it_had(tmp_path: Path, monkeypatch) -> None:
    manager, video, _commands = subtitle_library(tmp_path, monkeypatch)
    commands: list[list[str]] = []
    _failing_preparation(monkeypatch, commands)
    force_key = manager._subtitle_force_rebuild_state_key(video)
    upgrade_key = manager._subtitle_upgrade_state_key(video)

    manager.force_fresh_subtitle_selection(video)
    # Playable while it is rebuilt.
    episode = manager.db.episode_by_path(video)
    assert episode.state == "ready" and episode.subtitle_path.name == "old.srt"
    assert json.loads(manager.db.get_state(upgrade_key, ""))["fresh_rebuild"] is True

    manager.process_subtitle_jobs(limit=1)
    [command] = commands
    assert "--force-search" in command and "--resync" in command
    episode = manager.db.episode_by_path(video)
    assert episode.state == "ready" and episode.subtitle_path.name == "old.srt"
    assert episode.subtitle_path.read_text(encoding="utf-8").endswith("古い\n")
    # The rebuild is over: no job, and later checks are not forced rebuilds.
    assert manager.db.subtitle_jobs() == []
    assert manager.db.get_state(force_key, "") == "" and manager.db.get_state(upgrade_key, "") == ""


def test_a_fresh_rebuild_takes_the_new_subtitles_once_ready(tmp_path: Path, monkeypatch) -> None:
    manager, video, commands = subtitle_library(tmp_path, monkeypatch)
    manager.force_fresh_subtitle_selection(video)
    assert manager.process_subtitle_jobs(limit=1) == 1
    episode = manager.db.episode_by_path(video)
    # No score in the result: a rebuild the user asked for is not an upgrade comparison.
    assert episode.state == "ready" and episode.subtitle_path.read_text(encoding="utf-8").endswith("新しい\n")
    assert manager.db.latest_selected_subtitle(video)["candidate_name"] == "new.srt"
    assert manager.db.subtitle_jobs() == []


def test_jimaku_answers_are_expired_not_deleted(tmp_path: Path, monkeypatch) -> None:
    manager, video, _commands = subtitle_library(tmp_path, monkeypatch)
    client = JimakuClient(
        "https://jimaku.cc", "key", cache_dir=manager.config.paths.cache_dir, cache_ttl_seconds=600
    )
    payload = [{"id": 12634, "name": "FX Senshi Kurumi-chan"}]
    calls = {"n": 0}

    def answer(*_args, **_kwargs):
        calls["n"] += 1
        return httpx.Response(200, json=payload, request=httpx.Request("GET", "https://jimaku.cc/x"))

    monkeypatch.setattr(client.client, "get", answer)
    monkeypatch.setattr(client, "_acquire_request_slot", lambda _path: None)
    assert client.search_entries(anilist_id=206401)
    rate_limit = manager.config.paths.cache_dir / "jimaku-api" / "rate-limit.json"
    rate_limit.write_text(json.dumps({"until": time.time() + 30}), encoding="utf-8")
    before = rate_limit.stat().st_mtime

    manager.force_fresh_subtitle_selection(video)
    responses = [
        path for path in (manager.config.paths.cache_dir / "jimaku-api").glob("*.json") if path != rate_limit
    ]
    assert responses and all(path.stat().st_mtime < 1 for path in responses)
    assert rate_limit.stat().st_mtime == before

    def timeout(*_args, **_kwargs):
        calls["n"] += 1
        raise httpx.ReadTimeout("timed out", request=httpx.Request("GET", "https://jimaku.cc/x"))

    monkeypatch.setattr(client.client, "get", timeout)
    monkeypatch.setattr(client, "_rate_limit_remaining", lambda: 0.0)
    # Asked again (the answer is not fresh any more); Jimaku times out; the old answer is used.
    entries = client.search_entries(anilist_id=206401)
    assert [entry.id for entry in entries] == [12634] and calls["n"] == 2


def _kurumi(tmp_path: Path, after_cut_speech_offset: float) -> tuple[Path, Path, dict, Path]:
    data = json.loads(FIXTURE.read_text())
    source = _srt(tmp_path / "source.srt", data["source"], "ja")
    reference = _srt(tmp_path / "reference.srt", data["reference"], "en")
    timeline, result = align_subtitle_timelines(source, reference, tmp_path / "cache", force=True)
    # What the speech map did: nothing before the OP, -4.67 s after it.
    speech_cues = []
    for start, end, text in parse_srt(source):
        offset = 0.0 if start < 120.0 else (-4.67 if start < 1022.5 else after_cut_speech_offset)
        speech_cues.append([start + offset, end + offset])
    speech = _srt(tmp_path / "speech.srt", speech_cues, "ja")
    return speech, timeline, result, reference


def test_the_late_cut_of_the_timeline_is_kept_after_the_speech_map(tmp_path: Path) -> None:
    speech, timeline, result, reference = _kurumi(tmp_path, -4.67)
    out, meta = _keep_late_timeline_cut_after_speech(speech, timeline, result, reference, tmp_path / "cache")
    assert meta["applied"] is True, meta
    assert 1016.0 <= meta["boundary_source_time"] <= 1024.0
    assert meta["spliced_activity_full"] > meta["speech_activity_full"]
    spliced, said, timed = parse_srt(out), parse_srt(speech), parse_srt(timeline)
    assert len(spliced) == len(said) == len(timed)
    for index, (start, _end, _text) in enumerate(spliced):
        if timed[index][0] >= 1022.5 + 1.0:
            assert start == timed[index][0]  # the timeline's clock after the cut
        elif said[index][0] < 1000.0:
            assert start == said[index][0]  # speech before it, the cold open included
    # The cold open is speech's; the timeline now has the same clock there (it had -4/-5 s
    # before the leading-window clock, test_fix_cold_open_own_clock.py).
    assert spliced[0][0] == said[0][0] == timed[0][0]
    # Done once: the spliced file is not spliced again.
    assert (
        _keep_late_timeline_cut_after_speech(out, timeline, result, reference, tmp_path / "cache")[1][
            "reason"
        ]
        == "already_applied"
    )


def test_a_speech_map_that_has_the_cut_too_is_left_alone(tmp_path: Path) -> None:
    speech, timeline, result, reference = _kurumi(tmp_path, 1.5)
    out, meta = _keep_late_timeline_cut_after_speech(speech, timeline, result, reference, tmp_path / "cache")
    assert out == speech and meta["reason"] == "speech_has_the_cut"


def test_a_speech_map_on_another_clock_before_the_cut_is_left_alone(tmp_path: Path) -> None:
    speech, timeline, result, reference = _kurumi(tmp_path, -4.67)
    shifted = _srt(tmp_path / "shifted.srt", [[s + 3.0, e + 3.0] for s, e, _t in parse_srt(speech)], "ja")
    out, meta = _keep_late_timeline_cut_after_speech(shifted, timeline, result, reference, tmp_path / "cache")
    assert out == shifted and meta["reason"] == "speech_disagrees_before_cut"


def _speech_fit(manager, video: Path, *, late: bool, spliced: bool = False) -> None:
    alignment = {
        "engine": "japanese-stt+alass+opening-plateau",
        "selection_reason": "early_edit_japanese_speech_verification",
        "embedded_timeline_attempt": {
            "timeline_boundaries": [
                {
                    "source_time": 1022.5 if late else 130.0,
                    "left_offset_seconds": -5.0,
                    "right_offset_seconds": 1.5,
                }
            ]
        },
    }
    if spliced:
        alignment["timeline_late_cut"] = {"applied": True}
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


def test_old_speech_fits_without_the_late_cut_are_prepared_again_keeping_their_subtitles(
    tmp_path: Path, monkeypatch
) -> None:
    manager, video, _commands = subtitle_library(tmp_path, monkeypatch)
    upgrade_key = manager._subtitle_upgrade_state_key(video)
    for late, spliced in ((False, False), (True, True)):
        _speech_fit(manager, video, late=late, spliced=spliced)
        assert manager._requeue_speech_maps_without_late_cut() == 0
    _speech_fit(manager, video, late=True)
    assert manager._requeue_speech_maps_without_late_cut() == 1
    assert manager._requeue_speech_maps_without_late_cut() == 0  # once
    episode = manager.db.episode_by_path(video)
    assert episode.state == "ready" and episode.subtitle_path.name == "old.srt"
    assert json.loads(manager.db.get_state(upgrade_key, ""))["fresh_rebuild"] is True
    assert [row["video_path"] for row in manager.db.subtitle_jobs()] == [str(video)]
    # Maintenance keeps the playable selection and the job of a running check.
    manager.db.repair_spurious_ready_subtitle_jobs(preserve_paths=manager._active_subtitle_upgrade_paths())
    manager.db.repair_stale_subtitle_selections(preserve_paths=manager._active_subtitle_upgrade_paths())
    assert manager.db.episode_by_path(video).subtitle_path.name == "old.srt" and manager.db.subtitle_jobs()
    assert os.path.exists(episode.subtitle_path)
