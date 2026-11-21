"""One Jimaku candidate aligned alone (cli._sync_one -> optimize_subtitle) gets the same
onset-lead correction as a candidate that wins among several (optimize_candidates), and
the correction runs once, never twice.  Invented data only."""

from __future__ import annotations

import random
from pathlib import Path

import pudge.subtitles.pipeline  # noqa: F401 - bound before any syncing name is patched
import pudge.syncing as syncing
from pudge.config import SyncConfig
from pudge.models import SubtitleCandidate
from pudge.subtitle_formats import parse_srt, write_srt

RATE = 100.0


def _onsets(count: int = 120, seed: int = 4) -> list[float]:
    rng = random.Random(seed)
    times, t = [], 5.0
    for _ in range(count):
        times.append(round(t, 2))
        t += rng.uniform(2.5, 6.0)
    return times


def _speech_file(tmp_path: Path, onsets: list[float]) -> Path:
    import numpy as np

    frames = np.zeros(int(700 * RATE))
    for onset in onsets:
        frames[int(onset * RATE) : int((onset + 1.6) * RATE)] = 1.0
    path = tmp_path / "speech.npz"
    np.savez_compressed(path, speech=frames)
    return path


def _setup(tmp_path: Path, monkeypatch) -> tuple[Path, Path, list[int]]:
    onsets = _onsets()
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    aligned = tmp_path / "aligned.srt"
    # Aligned right at the voice: Japanese subtitles should lead it slightly.
    write_srt([(t, t + 1.8, f"台詞{i}") for i, t in enumerate(onsets)], aligned)
    speech = _speech_file(tmp_path, onsets)
    calls: list[int] = []
    real = syncing._correct_onset_lead

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(syncing, "_correct_onset_lead", counted)
    monkeypatch.setattr(
        syncing,
        "_optimize_subtitle_unguarded",
        lambda *a, **k: (aligned, {"sync_was_successful": True, "reason": "applied", "engine": "alass"}),
    )
    monkeypatch.setattr(syncing, "_guard_alignment_result", lambda *a, **k: None)
    monkeypatch.setattr(
        syncing, "_recover_rejected_clock", lambda _v, _s, path, result, *a, **k: (path, result)
    )
    monkeypatch.setattr(syncing, "subtitle_quality_accepted", lambda result: (True, "ok"))
    monkeypatch.setattr(syncing, "_media_timeline", lambda *a, **k: None)
    monkeypatch.setattr(syncing, "prepare_speech_reference", lambda *a, **k: (speech, {}))
    monkeypatch.setattr(syncing, "_has_embedded_text_subtitle", lambda *a, **k: False)
    return video, aligned, calls


def _candidate(tmp_path: Path, name: str) -> SubtitleCandidate:
    path = tmp_path / name
    path.write_text("1\n00:00:01,000 --> 00:00:02,000\nテスト\n", encoding="utf-8")
    return SubtitleCandidate(
        path=path, source="jimaku", score=100.0, name=name, details={"episode_match": "exact"}
    )


def test_single_candidate_gets_the_same_output_as_a_winner_among_several(tmp_path: Path, monkeypatch):
    from pudge.cli import optimize_candidates, optimize_subtitle

    video, aligned, calls = _setup(tmp_path, monkeypatch)
    only = _candidate(tmp_path, "ep.ja.srt")
    others = [only, _candidate(tmp_path, "ep2.ja.srt"), _candidate(tmp_path, "ep3.ja.srt")]

    def unguarded_candidates(video_, candidates, cache_dir, config, **kwargs):
        # The real chooser aligns the winner through syncing.optimize_subtitle.
        path, result = syncing.optimize_subtitle(video_, only.path, cache_dir, config, **kwargs)
        return only, path, result

    monkeypatch.setattr(syncing, "_optimize_candidates_unguarded", unguarded_candidates)

    single_path, single_result = optimize_subtitle(video, only.path, tmp_path / "cache-a", SyncConfig())
    assert len(calls) == 1
    chosen, many_path, many_result = optimize_candidates(video, others, tmp_path / "cache-b", SyncConfig())
    assert chosen is only
    assert len(calls) == 2  # once per prepared file, not again inside optimize_candidates

    assert single_result["onset_lead_correction"]["applied"] is True
    assert single_path != aligned and single_result["output"] == str(single_path)
    assert parse_srt(single_path) == parse_srt(many_path)
    assert (
        single_result["onset_lead_correction"]["shift_seconds"]
        == many_result["onset_lead_correction"]["shift_seconds"]
    )
    assert parse_srt(single_path)[0][0] < parse_srt(aligned)[0][0] - 0.1


def test_rejected_single_alignment_is_not_shifted(tmp_path: Path, monkeypatch):
    from pudge.cli import optimize_subtitle

    video, aligned, calls = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(syncing, "subtitle_quality_accepted", lambda result: (False, "bad"))
    path, result = optimize_subtitle(
        video, _candidate(tmp_path, "ep.ja.srt").path, tmp_path / "c", SyncConfig()
    )
    assert path == aligned and not calls and "onset_lead_correction" not in result
