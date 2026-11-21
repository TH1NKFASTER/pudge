"""One Jimaku file aligned alone gets the same last-resort Japanese speech clock as a file
next to other candidates (``optimize_candidates``).  Invented data only."""

from __future__ import annotations

import argparse
from pathlib import Path

import pudge.cli as cli
import pudge.syncing as syncing
from pudge.config import SyncConfig
from pudge.models import SubtitleCandidate
from pudge.subtitle_formats import write_srt

_REJECTED = {
    "sync_was_successful": True,
    "reason": "applied",
    "engine": "alass+ffsubsync",
    "alignment_output_rejected": True,
    "alignment_output_guard": {"status": "rejected", "reason": "too_many_large_jumps"},
}


def _files(tmp_path: Path) -> tuple[Path, SubtitleCandidate, Path]:
    video = tmp_path / "capture.ts"
    video.write_bytes(b"video")
    subtitle = tmp_path / "ep.ja.srt"
    write_srt([(10.0 + 5 * i, 13.0 + 5 * i, f"せりふ{i}") for i in range(60)], subtitle)
    rescued = tmp_path / "speech.srt"
    write_srt([(140.0 + 5 * i, 143.0 + 5 * i, f"せりふ{i}") for i in range(60)], rescued)
    candidate = SubtitleCandidate(
        path=subtitle,
        source="jimaku",
        score=100.0,
        name=subtitle.name,
        details={"language_purity": "japanese_only", "episode_match": "exact"},
    )
    return video, candidate, rescued


def _fake_speech(rescued: Path, tried: list[Path]):
    def fake(_video, subtitle, *_a, **_k):
        tried.append(subtitle)
        return rescued, {
            "sync_was_successful": True,
            "reason": "applied",
            "engine": "japanese-stt+text-clock-plateaus",
            "reference_alignment_reliable": True,
        }

    return fake


def test_rejected_single_file_gets_the_last_resort_speech_clock(tmp_path: Path, monkeypatch):
    video, candidate, rescued = _files(tmp_path)
    tried: list[Path] = []
    lead_steps: list[Path] = []
    monkeypatch.setattr(syncing, "_try_japanese_stt_fallback", _fake_speech(rescued, tried))
    monkeypatch.setattr(syncing, "_guard_alignment_result", lambda *a, **k: None)
    monkeypatch.setattr(
        syncing, "subtitle_quality_accepted", lambda r: (not r.get("alignment_output_rejected"), "x")
    )

    def fake_lead(_video, path, result, *_a, **_k):
        lead_steps.append(path)
        result["onset_lead_correction"] = {"applied": False}
        return path

    monkeypatch.setattr(syncing, "_apply_onset_lead", fake_lead)

    outcome = syncing.rescue_single_candidate(
        video, candidate, dict(_REJECTED), tmp_path / "cache", SyncConfig()
    )
    assert outcome is not None
    path, result = outcome
    assert path == rescued and tried == [candidate.path] and lead_steps == [rescued]
    assert result["selection_reason"] == "last_resort_stt_after_all_candidates_failed"
    assert result["single_candidate_rejected_result"]["engine"] == "alass+ffsubsync"

    # Not when the file was accepted, the speech fallback is off, or the file is a bitmap.
    tried.clear()
    accepted = {"sync_was_successful": True, "reason": "applied"}
    assert (
        syncing.rescue_single_candidate(video, candidate, accepted, tmp_path / "cache", SyncConfig()) is None
    )
    off = SyncConfig(japanese_stt_fallback=False)
    assert syncing.rescue_single_candidate(video, candidate, dict(_REJECTED), tmp_path / "cache", off) is None
    bitmap = SubtitleCandidate(path=tmp_path / "ep.sup", source="jimaku", score=1.0, name="ep.sup")
    assert (
        syncing.rescue_single_candidate(video, bitmap, dict(_REJECTED), tmp_path / "cache", SyncConfig())
        is None
    )
    assert tried == []


def test_rescue_refuses_an_explicit_other_title(tmp_path: Path, monkeypatch):
    video, candidate, rescued = _files(tmp_path)
    candidate.details.update({"entry_anilist_id": 1, "requested_anilist_id": 2})
    tried: list[Path] = []
    monkeypatch.setattr(syncing, "_try_japanese_stt_fallback", _fake_speech(rescued, tried))
    assert (
        syncing.rescue_single_candidate(video, candidate, dict(_REJECTED), tmp_path / "cache", SyncConfig())
        is None
    )
    assert tried == []


def test_cli_single_path_uses_the_rescue_with_the_chosen_candidate(tmp_path: Path, monkeypatch):
    video, candidate, rescued = _files(tmp_path)
    seen: list[tuple[SubtitleCandidate, dict]] = []

    def fake_rescue(_video, cand, result, cache_dir, config, **kwargs):
        seen.append((cand, kwargs))
        return rescued, {"reason": "applied", "sync_was_successful": True}

    monkeypatch.setattr(cli, "rescue_single_candidate", fake_rescue)
    config = cli.AppConfig()
    args = argparse.Namespace(verbose=False)
    outcome = cli._rescue_rejected_single_candidate(
        video, candidate.path, candidate, dict(_REJECTED), args, config
    )
    assert outcome is not None and outcome[0] == rescued
    assert seen[0][0] is candidate
    assert {"ffmpeg_path", "ffprobe_path", "alass_path"} <= set(seen[0][1])

    # A file given by hand (no candidate) is wrapped as one.
    outcome = cli._rescue_rejected_single_candidate(
        video, candidate.path, None, dict(_REJECTED), args, config
    )
    assert outcome is not None and seen[1][0].path == candidate.path
