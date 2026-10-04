"""Mandatory structural guard of aligned subtitles (timing review 2026-09-28).

Timing maps from the review archive, with dialogue replaced by stable tokens:
Jujutsu Kaisen E11 — six Jimaku files that
ALASS "aligned" to a wrong-episode reference (dozens of lines clamped to 0 s,
220 s scattered map, yet activity 0.87 and mutual consensus 0.93); Konosuba
and SPY×FAMILY — good results; Re:HAMATORA — a rejected discontinuity map.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from pudge import syncing
from pudge.alignment_guard import (GUARD_VERSION, stabilize_music_marker_pairs, validate_alignment_output, validate_final_standalone)
from pudge.models import SubtitleCandidate
from pudge.subtitle_formats import parse_srt

FIXTURES = Path(__file__).parent / "fixtures" / "alignment_guard"


def _fx(tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    if not target.exists():
        target.write_bytes(gzip.decompress((FIXTURES / f"{name}.gz").read_bytes()))
    return target


def _write(path: Path, cues: list[tuple[float, float, str]]) -> Path:
    def ts(value: float) -> str:
        ms = int(round(value * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    path.write_text("\n".join(f"{i}\n{ts(a)} --> {ts(b)}\n{t}\n" for i, (a, b, t) in enumerate(cues, 1)), encoding="utf-8")
    return path


def _dialogue(count: int = 200, start: float = 60.0) -> list[tuple[float, float, str]]:
    return [(start + i * 5.0, start + i * 5.0 + 2.5, f"台詞{i}番") for i in range(count)]


# ------------------------------------------------------ timing-map fixtures ---

@pytest.mark.parametrize("variant", ["01", "02", "03", "04", "05", "06"])
def test_jjk_destroyed_maps_are_rejected(tmp_path: Path, variant: str) -> None:
    verdict = validate_alignment_output(_fx(tmp_path, f"jjk-v{variant}-raw.srt"), _fx(tmp_path, f"jjk-v{variant}-pudge.srt"))
    assert verdict["status"] == "rejected"
    assert "clamped_starts" in verdict["reason"]
    assert verdict["diagnostics"]["new_clamped_starts"] >= 30
    assert verdict["version"] == GUARD_VERSION


@pytest.mark.parametrize("name", ["konosuba-v02", "spy-v01", "spy-v03"])
def test_good_real_alignments_pass(tmp_path: Path, name: str) -> None:
    verdict = validate_alignment_output(_fx(tmp_path, f"{name}-raw.srt"), _fx(tmp_path, f"{name}-pudge.srt"))
    assert verdict["status"] == "accepted", verdict


def test_spy_music_markers_are_not_speech_evidence(tmp_path: Path) -> None:
    verdict = validate_alignment_output(_fx(tmp_path, "spy-v01-raw.srt"), _fx(tmp_path, "spy-v01-pudge.srt"))
    # ♪〜 moved +8.9 s while dialogue moved +0.88 s: not a scattered map.
    assert verdict["diagnostics"]["shift_span_p05_p95"] < 0.5
    assert verdict["diagnostics"]["speech_matched"] < verdict["diagnostics"]["matched"]


def test_rehamatora_discontinuity_map_is_rejected(tmp_path: Path) -> None:
    verdict = validate_alignment_output(_fx(tmp_path, "rehamatora-v01-raw.srt"), _fx(tmp_path, "rehamatora-v01-pudge.srt"))
    assert verdict["status"] == "rejected"


def test_standalone_check_catches_legacy_destroyed_file(tmp_path: Path) -> None:
    assert validate_final_standalone(_fx(tmp_path, "jjk-v01-pudge.srt"))["status"] == "rejected"
    assert validate_final_standalone(_fx(tmp_path, "konosuba-v02-pudge.srt"))["status"] == "accepted"
    assert validate_final_standalone(_fx(tmp_path, "spy-v03-pudge.srt"))["status"] == "accepted"


# ----------------------------------------------------------- synthetic ---

def test_large_constant_offset_is_not_rejected_for_magnitude(tmp_path: Path) -> None:
    raw = _dialogue()
    final = [(a - 50.0, b - 50.0, t) for a, b, t in raw]
    verdict = validate_alignment_output(_write(tmp_path / "r.srt", raw), _write(tmp_path / "f.srt", final))
    assert verdict["status"] == "accepted" and verdict["diagnostics"]["median_shift"] == -50.0


def test_cold_open_piecewise_map_is_accepted(tmp_path: Path) -> None:
    raw = _dialogue()
    final = [(a + (0.0 if i < 20 else 95.0), b + (0.0 if i < 20 else 95.0), t) for i, (a, b, t) in enumerate(raw)]
    verdict = validate_alignment_output(_write(tmp_path / "r.srt", raw), _write(tmp_path / "f.srt", final))
    assert verdict["status"] == "accepted" and verdict["reason"] == "large_piecewise_map"


def test_scattered_map_is_rejected_even_without_clamping(tmp_path: Path) -> None:
    raw = _dialogue()
    final = [(a + (0.0 if i % 2 else 40.0), b + (0.0 if i % 2 else 40.0), t) for i, (a, b, t) in enumerate(raw)]
    verdict = validate_alignment_output(_write(tmp_path / "r.srt", raw), _write(tmp_path / "f.srt", final))
    assert verdict["status"] == "rejected" and "scattered_time_map" in verdict["reason"]


def test_existing_raw_overlaps_are_not_new_defects(tmp_path: Path) -> None:
    raw = []
    for i in range(150):  # ASS-style parallel lines: every pair overlaps in the raw file
        raw.append((10.0 + i * 6, 14.0 + i * 6, f"A{i}"))
        raw.append((11.0 + i * 6, 15.0 + i * 6, f"B{i}"))
    final = [(a + 1.2, b + 1.2, t) for a, b, t in raw]
    verdict = validate_alignment_output(_write(tmp_path / "r.srt", raw), _write(tmp_path / "f.srt", final))
    assert verdict["status"] == "accepted" and verdict["diagnostics"]["new_overlaps"] == 0


def test_clamped_lines_with_preserved_texts_are_rejected(tmp_path: Path) -> None:
    raw = _dialogue()
    final = [((0.0, 1.0, t) if i < 30 else (a, b, t)) for i, (a, b, t) in enumerate(raw)]
    verdict = validate_alignment_output(_write(tmp_path / "r.srt", raw), _write(tmp_path / "f.srt", final))
    assert verdict["status"] == "rejected" and "clamped_starts" in verdict["reason"]


# ------------------------------------------------- production integration ---

def _jjk_candidate(tmp_path: Path, variant: str) -> tuple[SubtitleCandidate, Path]:
    raw = _fx(tmp_path, f"jjk-v{variant}-raw.srt")
    candidate = SubtitleCandidate(
        source="jimaku", name=raw.name, path=raw, score=100.0,
        details={"entry_anilist_id": 113415, "requested_anilist_id": 113415, "entry_anilist_match": True,
                 "entry_exact_title_match": True, "episode_match": "exact", "title_similarity": 100.0,
                 "media_format": "TV"},
    )
    return candidate, _fx(tmp_path, f"jjk-v{variant}-pudge.srt")


def test_mutual_clock_consensus_no_longer_accepts_jjk(tmp_path: Path) -> None:
    items = []
    for variant in ("01", "02", "03", "04", "05", "06"):
        candidate, aligned = _jjk_candidate(tmp_path, variant)
        items.append(((1.0, 0.87, 1.0, 100.0), candidate, aligned, {"sync_was_successful": True},
                      {"available": True, "weighted": 0.87, "start": 0.9, "middle": 0.83, "end": 0.88},
                      {"reason": "ok", "retained_ratio": 1.0, "source_cues": 409, "aligned_cues": 409}))
    selected, payload = syncing._exact_jimaku_timing_consensus(items)
    assert selected is None and payload["accepted"] is False
    assert len(payload["guard_rejected"]) == 6


def test_quality_hard_rejects_come_before_identity_exceptions() -> None:
    exact_special = {
        "sync_was_successful": True,
        "reference_discontinuity_rejected": True,
        "timing_reference_validation": {
            "total_samples": 4, "accepted": False, "alignment_mode": "alass-timestamp", "structure_reason": "ok",
            "reference_output_structure": {"retained_ratio": 1.0}, "reference_activity": {"weighted": 0.95},
        },
        "candidate_context": {"source": "jimaku", "entry_anilist_match": True, "entry_exact_title_match": True,
                              "single_special_exact_entry": True, "subtitle_suffix": ".srt"},
    }
    accepted, _reason = syncing.subtitle_quality_accepted(exact_special)
    assert accepted is False
    guarded = {"sync_was_successful": True, "reference_alignment_reliable": True,
               "alignment_output_guard": {"status": "rejected", "reason": "clamped_starts"}}
    assert syncing.subtitle_quality_accepted(guarded)[0] is False


def test_optimize_subtitle_wrapper_rejects_destroyed_output(tmp_path: Path, monkeypatch) -> None:
    candidate, aligned = _jjk_candidate(tmp_path, "01")
    monkeypatch.setattr(syncing, "_optimize_subtitle_unguarded",
                        lambda *a, **k: (aligned, {"sync_was_successful": True, "reference_alignment_reliable": True}))
    path, result = syncing.optimize_subtitle(tmp_path / "v.mkv", candidate.path, tmp_path / "cache", None)
    assert result["sync_was_successful"] is False and result["alignment_output_rejected"] is True
    assert result["alignment_output_guard"]["status"] == "rejected"
    assert syncing.subtitle_quality_accepted(result)[0] is False


@pytest.mark.parametrize("branch", ["timeline", "speech", "trusted_clock", "semantic", "audio_consensus", "mutual_clock"])
def test_every_early_return_passes_the_guard(tmp_path: Path, monkeypatch, branch: str) -> None:
    """Whatever branch picks the destroyed file, the guarded wrapper moves on."""
    bad, bad_aligned = _jjk_candidate(tmp_path, "01")
    good_raw = _fx(tmp_path, "konosuba-v02-raw.srt")
    good_aligned = _fx(tmp_path, "konosuba-v02-pudge.srt")
    good = SubtitleCandidate(source="jimaku", name="good", path=good_raw, score=90.0, details={})
    calls: list[list[str]] = []

    def impl(video, candidates, cache_dir, config, **kwargs):
        names = [c.name for c in candidates]
        calls.append(names)
        first = candidates[0]
        aligned = bad_aligned if first is bad else good_aligned
        return first, aligned, {"sync_was_successful": True, "reference_alignment_reliable": True,
                                "selection_reason": branch}

    monkeypatch.setattr(syncing, "_optimize_candidates_unguarded", impl)
    selected, path, result = syncing.optimize_candidates(tmp_path / "v.mkv", [bad, good], tmp_path / "c", None)
    assert selected is good and path == good_aligned
    assert calls == [[bad.name, "good"], ["good"]]
    assert result["guard_rejected_candidates"][0]["name"] == bad.name


def test_all_destroyed_candidates_give_no_file(tmp_path: Path, monkeypatch) -> None:
    pairs = [_jjk_candidate(tmp_path, v) for v in ("01", "02")]
    by_name = {c.name: a for c, a in pairs}
    monkeypatch.setattr(syncing, "_optimize_candidates_unguarded",
                        lambda video, candidates, *a, **k: (candidates[0], by_name[candidates[0].name],
                                                            {"sync_was_successful": True, "reference_alignment_reliable": True}))
    selected, path, result = syncing.optimize_candidates(tmp_path / "v.mkv", [c for c, _ in pairs], tmp_path / "c", None)
    assert selected is None and path is None and len(result["guard_rejected_candidates"]) == 2


def test_quality_rejected_early_return_is_not_handed_out(tmp_path: Path, monkeypatch) -> None:
    raw = _fx(tmp_path, "konosuba-v02-raw.srt")
    aligned = _fx(tmp_path, "konosuba-v02-pudge.srt")
    only = SubtitleCandidate(source="jimaku", name="only", path=raw, score=90.0, details={})
    monkeypatch.setattr(syncing, "_optimize_candidates_unguarded",
                        lambda *a, **k: (only, aligned, {"sync_was_successful": True, "reference_discontinuity_rejected": True}))
    assert syncing.optimize_candidates(tmp_path / "v.mkv", [only], tmp_path / "c", None)[:2] == (None, None)


def test_legacy_pipeline_cache_is_rechecked(tmp_path: Path) -> None:
    from pudge.config import AppConfig
    from pudge.pipeline_cache import _manifest_path, load_final_pipeline_result, save_final_pipeline_result

    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    video = tmp_path / "Jujutsu Kaisen - 11.mkv"
    video.write_bytes(b"v")
    bad = _fx(tmp_path, "jjk-v01-pudge.srt")
    manifest = save_final_pipeline_result(video, cfg, subtitle=bad, subtitle_id=None, dependency=bad, source="external")
    assert load_final_pipeline_result(video, cfg) is not None  # written by the guarded pipeline: trusted
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload.pop("alignment_guard_version")
    payload.pop("raw_source")
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    assert load_final_pipeline_result(video, cfg) is None  # legacy + destroyed → not handed out
    assert not _manifest_path(video, cfg).exists()

    good = _fx(tmp_path, "konosuba-v02-pudge.srt")
    manifest = save_final_pipeline_result(video, cfg, subtitle=good, subtitle_id=None, dependency=good, source="external",
                                          raw_source=_fx(tmp_path, "konosuba-v02-raw.srt"))
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload.pop("alignment_guard_version")
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    assert load_final_pipeline_result(video, cfg) is not None  # legacy but re-checked against raw


def test_extreme_map_gate_marks_output_guard_required() -> None:
    _ok, _reason, details = syncing._gate_embedded_reference_alass_discontinuity(
        {"alass_shift_spread_seconds": 220.0, "alass_blocks": 3, "alass_distinct_shifts": 3},
        {"start": 0.9, "middle": 0.83, "end": 0.88, "weighted": 0.87},
        reference_ok=True, reference_reason="ok",
    )
    assert details["accepted"] is False
    assert details["reason"] == "extreme_map_requires_speech_verification"
    assert details["output_guard_required"] is True


def test_raw_fallback_disqualification_only_for_hard_negative_results() -> None:
    assert syncing.raw_fallback_disqualified({
        "alignment_output_guard": {"status": "rejected", "reason": "clamped_starts"}
    }) is True
    assert syncing.raw_fallback_disqualified({"reference_discontinuity_rejected": True}) is True
    assert syncing.raw_fallback_disqualified({"reason": "semantic mismatch"}) is True
    assert syncing.raw_fallback_disqualified({"reason": "semantic_mismatch"}) is True
    assert syncing.raw_fallback_disqualified({
        "timing_reference_validation": {"accepted": False, "reason": "semantic mismatch"}
    }) is True
    assert syncing.raw_fallback_disqualified({
        "sync_was_successful": False, "reason": "no usable timing reference"
    }) is False


def test_automatic_raw_fallback_skips_hard_rejected_candidate(tmp_path: Path) -> None:
    from pudge.cli import _confident_raw_unsynced_candidate

    bad_path = _write(tmp_path / "bad.srt", _dialogue(10))
    good_path = _write(tmp_path / "good.srt", _dialogue(10, start=10.0))
    bad = SubtitleCandidate(
        path=bad_path, source="jimaku", score=100.0, name="bad", episode=11,
        verified_japanese=True, details={"episode_match": "exact"},
    )
    good = SubtitleCandidate(
        path=good_path, source="jimaku", score=90.0, name="good", episode=11,
        verified_japanese=True, details={"episode_match": "exact"},
    )
    alignment_result = {
        "quality_fallback_attempts": [
            {
                "name": "bad", "path": str(bad_path), "accepted": False,
                "raw_fallback_disqualified": True,
            },
            {
                "name": "good", "path": str(good_path), "accepted": False,
                "raw_fallback_disqualified": False,
            },
        ]
    }

    selected = _confident_raw_unsynced_candidate(
        [bad, good], expected_episode=11, manually_selected=False,
        minimum_score=80.0, alignment_result=alignment_result,
    )
    assert selected is good


def test_manual_raw_fallback_can_still_use_explicitly_selected_file(tmp_path: Path) -> None:
    from pudge.cli import _confident_raw_unsynced_candidate

    path = _write(tmp_path / "manual.srt", _dialogue(10))
    candidate = SubtitleCandidate(
        path=path, source="manual", score=0.0, name="manual", episode=None,
        verified_japanese=False, details={},
    )
    selected = _confident_raw_unsynced_candidate(
        [candidate], expected_episode=11, manually_selected=True,
        minimum_score=80.0,
        alignment_result={
            "guard_rejected_candidates": [{
                "path": str(path), "raw_fallback_disqualified": True,
            }]
        },
    )
    assert selected is candidate


def test_quality_attempt_records_raw_fallback_disqualification(tmp_path: Path, monkeypatch) -> None:
    from pudge.config import SyncConfig

    raw = _write(tmp_path / "candidate.srt", _dialogue(20))
    candidate = SubtitleCandidate(
        path=raw, source="jimaku", score=100.0, name="candidate", episode=1,
        verified_japanese=True,
        details={"episode_match": "exact", "entry_anilist_match": True},
    )
    monkeypatch.setattr(
        syncing, "prepare_speech_reference",
        lambda *_args, **_kwargs: (None, {"reason": "test"}),
    )
    monkeypatch.setattr(
        syncing, "synchronize_subtitle",
        lambda _video, subtitle, *_args, **_kwargs: (
            subtitle,
            {"sync_was_successful": True, "alignment_score": 100.0},
        ),
    )
    monkeypatch.setattr(
        syncing, "optimize_subtitle",
        lambda _video, subtitle, *_args, **_kwargs: (
            subtitle,
            {
                "sync_was_successful": False,
                "reason": "alignment_output_guard: clamped_starts",
                "alignment_output_guard": {"status": "rejected", "reason": "clamped_starts"},
            },
        ),
    )

    selected, path, result = syncing._optimize_candidates_unguarded(
        tmp_path / "episode.mkv", [candidate], tmp_path / "cache", SyncConfig(),
    )
    assert selected is None and path is None
    attempt = result["quality_fallback_attempts"][0]
    assert attempt["path"] == str(raw)
    assert attempt["raw_fallback_disqualified"] is True


def test_music_marker_pair_uses_one_nearby_dialogue_clock(tmp_path: Path) -> None:
    raw = tmp_path / "raw.srt"
    final = tmp_path / "final.srt"
    raw.write_text(
        "1\n00:00:01,418 --> 00:00:03,420\n♪～\n\n"
        "2\n00:01:27,587 --> 00:01:29,589\n～♪\n\n"
        "3\n00:01:31,591 --> 00:01:33,468\n決戦の時は来た\n\n"
        "4\n00:01:34,000 --> 00:01:35,000\n次の台詞\n",
        encoding="utf-8",
    )
    final.write_text(
        "1\n00:00:11,230 --> 00:00:13,232\n♪～\n\n"
        "2\n00:01:27,485 --> 00:01:29,487\n～♪\n\n"
        "3\n00:01:31,489 --> 00:01:33,366\n決戦の時は来た\n\n"
        "4\n00:01:33,898 --> 00:01:34,898\n次の台詞\n",
        encoding="utf-8",
    )

    result = stabilize_music_marker_pairs(raw, final, cache_dir=tmp_path)
    cues = parse_srt(final)

    assert result["applied"] is True
    assert result["repairs"][0]["before_shifts"] == [9.812, -0.102]
    assert result["repairs"][0]["supported_shift"] == -0.102
    assert cues[0][:2] == pytest.approx((1.316, 3.318), abs=0.001)
    assert cues[1][:2] == pytest.approx((87.485, 89.487), abs=0.001)
    assert cues[2][0] == pytest.approx(91.489, abs=0.001)


def test_music_marker_pair_with_consistent_shift_is_unchanged(tmp_path: Path) -> None:
    raw = tmp_path / "raw.srt"
    final = tmp_path / "final.srt"
    raw.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\n♪～\n\n"
        "2\n00:01:21,000 --> 00:01:23,000\n～♪\n\n"
        "3\n00:01:25,000 --> 00:01:27,000\n台詞\n",
        encoding="utf-8",
    )
    final.write_text(
        "1\n00:00:02,000 --> 00:00:04,000\n♪～\n\n"
        "2\n00:01:22,000 --> 00:01:24,000\n～♪\n\n"
        "3\n00:01:26,000 --> 00:01:28,000\n台詞\n",
        encoding="utf-8",
    )
    before = final.read_bytes()
    result = stabilize_music_marker_pairs(raw, final, cache_dir=tmp_path)
    assert result["applied"] is False
    assert final.read_bytes() == before
