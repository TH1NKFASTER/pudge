"""SELECT2: the second Jimaku file is synced too and wins only when its cues meet more speech onsets.

``subtitles.candidate_choice`` is the pure rule; ``optimize_candidates`` runs the selection a
second time without the accepted file (one quality attempt), passes that file through the same
output guard and quality gate, and keeps the first file on any failure or missing signal.
Invented timings only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pudge import syncing
from pudge.config import SyncConfig
from pudge.models import SubtitleCandidate
from pudge.subtitles import candidate_choice

# ------------------------------------------------------------------------- pure rule ---


def _signal(fraction, matched=100):
    return {"available": fraction is not None, "matched_fraction": fraction, "matched_cues": matched}


def test_the_second_file_wins_only_with_a_strictly_higher_fraction() -> None:
    assert candidate_choice.choose(_signal(0.60), _signal(0.72)) == (1, "second_matches_more_onsets")
    assert candidate_choice.choose(_signal(0.72), _signal(0.60)) == (0, "first_not_beaten")
    assert candidate_choice.choose(_signal(0.70), _signal(0.70)) == (0, "first_not_beaten")


def test_a_second_file_with_fewer_matched_cues_never_wins() -> None:
    # Fewer cues inflate the fraction: 299/364 (0.82) against 346/445 (0.78) keeps the first file.
    assert candidate_choice.choose(_signal(0.78, 346), _signal(0.82, 299)) == (0, "second_fewer_matched_cues")
    assert candidate_choice.choose(_signal(0.78, 346), _signal(0.82, 346)) == (
        1,
        "second_matches_more_onsets",
    )
    assert candidate_choice.choose(_signal(0.78, 346), _signal(0.82, 360)) == (
        1,
        "second_matches_more_onsets",
    )


def test_missing_signals_keep_the_first_file() -> None:
    assert candidate_choice.choose(_signal(0.6), None) == (0, "second_unavailable")
    assert candidate_choice.choose(_signal(None), _signal(0.9)) == (0, "signal_missing")
    assert candidate_choice.choose(_signal(0.5), _signal(None)) == (0, "signal_missing")
    assert candidate_choice.choose(None, _signal(0.9)) == (0, "signal_missing")


def test_the_second_sync_is_skipped_when_it_cannot_change_the_choice() -> None:
    assert candidate_choice.needs_second(_signal(None)) == (False, "first_signal_missing")
    assert candidate_choice.needs_second(_signal(0.95)) == (False, "first_onsets_matched")
    assert candidate_choice.needs_second(_signal(candidate_choice.SKIP_SECOND_MATCHED_FRACTION)) == (
        False,
        "first_onsets_matched",
    )
    assert candidate_choice.needs_second(_signal(0.89)) == (True, "check_second")


def test_onset_signal_is_the_onset_lead_measurement() -> None:
    onsets = [10.0 + 3.0 * k for k in range(60)]
    starts = [value - 0.15 for value in onsets[:30]] + [value + 1.3 for value in onsets[30:]]
    signal = candidate_choice.onset_signal(starts, onsets)
    assert signal["matched_cues"] == 30 and signal["cues"] == 60
    assert signal["matched_fraction"] == 0.5
    few = candidate_choice.onset_signal(starts[:10], onsets)
    assert few["matched_fraction"] is None and few["reason"] == "too_few_cues"


# ---------------------------------------------------------------------- integration ---

ONSETS = [10.0 + 3.0 * k for k in range(60)]


def _activity() -> list[float]:
    values = [0.0] * int(200 * 100)
    for onset in ONSETS:
        for frame in range(int(onset * 100), int((onset + 0.8) * 100)):
            values[frame] = 1.0
    return values


def _srt(path: Path, on_onset: int, count: int = 60) -> Path:
    """``count`` cues; the first ``on_onset`` start 0.15 s before a speech onset, the rest 1.3 s after."""

    def stamp(value: float) -> str:
        millis = int(round(value * 1000))
        minutes, millis = divmod(millis, 60_000)
        seconds, millis = divmod(millis, 1000)
        return f"00:{minutes:02d}:{seconds:02d},{millis:03d}"

    blocks = []
    for index, onset in enumerate(ONSETS[:count]):
        start = onset - 0.15 if index < on_onset else onset + 1.3
        blocks.append(f"{index + 1}\n{stamp(start)} --> {stamp(start + 1.0)}\nせりふ{index}\n")
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    state: dict[str, object] = {"passes": [], "outputs": {}, "rejected": set(), "raise": False, "engine": {}}

    def candidate(name: str) -> SubtitleCandidate:
        path = tmp_path / f"{name}.srt"
        path.write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n", encoding="utf-8")
        return SubtitleCandidate(path=path, source="jimaku", score=100.0, name=name)

    def unguarded(video_path, candidates, cache_dir, config, **kwargs):
        candidates = list(candidates)
        state["passes"].append(([c.name for c in candidates], dict(kwargs)))
        if state["raise"] and len(state["passes"]) > 1:
            raise RuntimeError("second pass broke")
        chosen = candidates[0]
        result = {
            "reason": "applied",
            "sync_was_successful": True,
            "engine": state["engine"].get(chosen.name, "embedded-reference+timeline"),
            "selection_reason": f"picked-{chosen.name}",
        }
        if chosen.name in state["rejected"]:
            result["reject"] = True
        return chosen, state["outputs"][chosen.name], result

    def accepted(result):
        return (False, "rejected") if result.get("reject") else (True, "ok")

    onset_lead_paths: list[Path] = []

    def apply_onset_lead(video_path, path, result, cache_dir, config, **kwargs):
        onset_lead_paths.append(path)
        return path

    monkeypatch.setattr(syncing, "_optimize_candidates_unguarded", unguarded)
    monkeypatch.setattr(syncing, "_guard_alignment_result", lambda *a, **k: None)
    monkeypatch.setattr(
        syncing, "_recover_rejected_clock", lambda v, s, path, result, *a, **k: (path, result)
    )
    monkeypatch.setattr(syncing, "subtitle_quality_accepted", accepted)
    monkeypatch.setattr(syncing, "_audio_speech_activity", lambda *a, **k: _activity())
    monkeypatch.setattr(syncing, "_apply_onset_lead", apply_onset_lead)
    monkeypatch.setattr(syncing, "_media_timeline", lambda *a, **k: None)

    def run(*names_with_onsets: tuple):
        candidates = []
        for name, on_onset, *count in names_with_onsets:
            candidates.append(candidate(name))
            state["outputs"][name] = _srt(tmp_path / f"{name}-out.srt", on_onset, *count)
        config = SyncConfig(japanese_stt_fallback=False)
        return syncing.optimize_candidates(
            video, candidates, tmp_path / "cache", config, ffmpeg_path="ffmpeg", force=True
        )

    return run, state, onset_lead_paths


def test_the_second_file_wins_when_more_of_its_cues_meet_the_voice(world) -> None:
    run, state, onset_lead_paths = world
    selected, path, result = run(("first", 30), ("second", 54), ("third", 60))
    assert selected.name == "second"
    assert path == state["outputs"]["second"] and onset_lead_paths == [path]
    record = result["candidate_onset_selection"]
    assert record["version"] == candidate_choice.VERSION
    assert record["decision"] == "second" and record["reason"] == "second_matches_more_onsets"
    assert (
        record["first"]["matched_fraction"] == 0.5 and record["first"]["selection_reason"] == "picked-first"
    )
    assert record["second"]["matched_fraction"] == 0.9 and record["second"]["name"] == "second"
    # Second pass: without the accepted file, one quality attempt, shared analyses not redone.
    names, kwargs = state["passes"][1]
    assert names == ["second", "third"]
    assert kwargs["quality_attempt_limit"] == 1 and kwargs["force"] is False


def test_the_first_file_stays_when_it_meets_as_many_onsets(world) -> None:
    run, state, _ = world
    selected, _path, result = run(("first", 40), ("second", 40))
    assert selected.name == "first"
    assert result["candidate_onset_selection"]["reason"] == "first_not_beaten"
    assert len(state["passes"]) == 2


def test_a_shorter_second_file_does_not_win_on_an_inflated_fraction(world) -> None:
    run, state, _ = world
    # first 45/60 (0.75), second 41/50 (0.82): fewer matched cues keeps the first file.
    selected, _path, result = run(("first", 45), ("second", 41, 50))
    assert selected.name == "first"
    record = result["candidate_onset_selection"]
    assert record["version"] == candidate_choice.VERSION
    assert record["reason"] == "second_fewer_matched_cues"
    assert record["second"]["matched_fraction"] == 0.82 and record["second"]["matched_cues"] == 41


def test_no_second_sync_when_the_first_file_already_meets_the_voice(world) -> None:
    run, state, _ = world
    selected, _path, result = run(("first", 58), ("second", 60))
    assert selected.name == "first" and len(state["passes"]) == 1
    assert result["candidate_onset_selection"]["reason"] == "first_onsets_matched"


def test_a_single_candidate_is_never_synced_twice(world) -> None:
    run, state, _ = world
    selected, _path, result = run(("only", 10))
    assert selected.name == "only" and len(state["passes"]) == 1
    assert "candidate_onset_selection" not in result


def test_a_rejected_second_file_never_wins(world) -> None:
    run, state, _ = world
    state["rejected"].add("second")
    selected, _path, result = run(("first", 30), ("second", 60))
    assert selected.name == "first"
    record = result["candidate_onset_selection"]
    assert record["reason"] == "second_unavailable" and record["second"]["quality_reason"] == "rejected"


def test_a_failing_second_pass_keeps_the_first_file(world) -> None:
    run, state, _ = world
    state["raise"] = True
    selected, _path, result = run(("first", 30), ("second", 60))
    assert selected.name == "first"
    assert result["candidate_onset_selection"]["reason"] == "second_unavailable"


def test_a_file_timed_for_this_exact_release_is_kept_without_a_second_sync(world) -> None:
    run, state, _ = world
    state["engine"]["first"] = "exact-release-crc"
    selected, _path, result = run(("first", 0), ("second", 60))
    assert selected.name == "first" and len(state["passes"]) == 1
    assert result["candidate_onset_selection"]["reason"] == "first_signal_missing"


def test_the_second_pass_reuses_this_process_timeline_alignments(tmp_path: Path, monkeypatch) -> None:
    from pudge.subtitles import timeline_alignment as ta

    source, reference = _srt(tmp_path / "ja.srt", 60), _srt(tmp_path / "en.srt", 60)
    output = tmp_path / "out.srt"
    output.write_text("x", encoding="utf-8")
    calls = []

    def fake(src, ref, cache_dir, *, max_offset_seconds=120.0, force=False):
        calls.append(src)
        return output, {"accepted": True, "segments": [{"offset": 1.0}]}

    monkeypatch.setattr(ta, "_align_subtitle_timelines", fake)
    monkeypatch.setattr(ta, "_ALIGNMENT_MEMO", {})
    first = ta.align_subtitle_timelines(source, reference, tmp_path)
    first[1]["segments"].append("changed by a caller")
    second = ta.align_subtitle_timelines(source, reference, tmp_path, force=True)
    assert len(calls) == 1 and second == (output, {"accepted": True, "segments": [{"offset": 1.0}]})
    ta.align_subtitle_timelines(source, reference, tmp_path, max_offset_seconds=60.0)
    assert len(calls) == 2
    source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    ta.align_subtitle_timelines(source, reference, tmp_path)
    assert len(calls) == 3
