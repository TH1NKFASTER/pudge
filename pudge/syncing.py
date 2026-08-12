from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import os
import re
import shutil
import statistics
import subprocess
import tempfile
import time
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path

from .branding import APP_SLUG
from typing import Iterable

from .config import SyncConfig
from .language import TEXT_SUBTITLE_EXTENSIONS
from .llm import OllamaClient
from .logging_utils import configure_logging, timed_step
from .models import SubtitleCandidate
from .pgs import build_time_mapper, onset_match_score, onset_times, parse_pgs_cues, retime_sup
from .subtitle_formats import convert_to_plain_srt, parse_srt, write_srt
from .subtitles.video_segments import choose_edit_boundary, probe_container_edit_points
from .subtitles.stt import prepare_japanese_stt_reference
from .subtitles.timeline_alignment import align_subtitle_timelines

_SCORE_RE = re.compile(r"\bscore:\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_ALASS_SHIFT_RE = re.compile(r"shifted block .*? by\s+([+-]?\d+:\d{2}:\d{2}(?:\.\d+)?)", re.IGNORECASE)
_TEXT_REFERENCE_CODECS = {"ass", "ssa", "subrip", "srt", "webvtt", "mov_text", "text"}


def _candidate_explicit_anilist_mismatch(candidate: SubtitleCandidate) -> bool:
    details = candidate.details if isinstance(candidate.details, dict) else {}
    # Narrow stale-parent exception: a separately linked special/movie may use
    # another AniList ID when its entry title exactly matches the opened video.
    if bool(details.get("entry_identity_exact_title_match")):
        return False
    entry_id = details.get("entry_anilist_id")
    requested_id = details.get("requested_anilist_id")
    if entry_id in {None, ""} or requested_id in {None, ""}:
        return False
    try:
        return int(entry_id) != int(requested_id)
    except (TypeError, ValueError):
        return False


def _fingerprint(video: Path, subtitle: Path, config: SyncConfig, *, tag: str = "ffsubsync") -> str:
    video_stat = video.stat()
    subtitle_stat = subtitle.stat()
    raw = (
        f"syncing-v0.3.37-timeline-monotonic-boundaries-sparse-cold-open:{tag}:"
        f"{video.resolve()}:{video_stat.st_size}:{video_stat.st_mtime_ns}:"
        f"{subtitle.resolve()}:{subtitle_stat.st_size}:{subtitle_stat.st_mtime_ns}:"
        f"{config.max_offset_seconds}:{config.quality_max_offset_seconds}:"
        f"{config.skip_on_low_quality}:{config.vad}:{config.fix_framerate}:{config.gss}:"
        f"{config.engine}:{config.alass_split_penalty}:{config.compare_engines}:"
        f"{config.segment_validation}:{config.segment_count}:{config.segment_window_seconds}:"
        f"{config.segment_max_offset_seconds}:{config.piecewise_repair}:"
        f"{config.piecewise_min_offset_seconds}:{config.piecewise_jump_threshold_seconds}:"
        f"{config.piecewise_max_correction_seconds}"
    )
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def _result(reason: str, **values: object) -> dict[str, object]:
    return {"reason": reason, **values}


def _record_timeline_debug_attempt(
    cache_dir: Path,
    video: Path,
    source: Path,
    reference: Path,
    result: dict[str, object],
    *,
    stage: str,
) -> None:
    try:
        root = Path(cache_dir) / "subtitle-timeline-debug"
        root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(str(Path(video).resolve()).encode("utf-8")).hexdigest()
        target = root / f"{digest}.jsonl"
        row = {
            "timestamp": time.time(),
            "stage": stage,
            "video": str(Path(video)),
            "source": str(Path(source)),
            "reference": str(Path(reference)),
            "result": result,
        }
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def _best_reference_discontinuity_rejection(
    successful: list[tuple[Path, dict[str, object]]],
) -> tuple[Path, dict[str, object]] | None:
    # Once the exact embedded text reference rejects a catastrophic ALASS
    # block discontinuity, audio/ffsubsync is weaker and very expensive for
    # the same candidate. Preserve the rejected result for the quality gate.
    rejected = [
        item
        for item in successful
        if bool(item[1].get("reference_discontinuity_rejected"))
    ]
    if not rejected:
        return None
    return max(
        rejected,
        key=lambda item: float(item[1].get("alignment_score") or float("-inf")),
    )


def _subtitle_shift_summary(source: Path, aligned: Path) -> dict[str, object]:
    """Summarize the time map when ALASS preserves cue order/count.

    A near-zero spread means the whole subtitle was moved by one constant
    offset. The median remains useful in logs even for mildly non-linear maps.
    """
    try:
        source_cues = parse_srt(source)
        aligned_cues = parse_srt(aligned)
    except OSError:
        return {}
    if not source_cues or len(source_cues) != len(aligned_cues):
        return {}

    shifts = [
        aligned_start - source_start
        for (source_start, _, _), (aligned_start, _, _) in zip(source_cues, aligned_cues)
    ]
    if not shifts:
        return {}
    median_shift = float(statistics.median(shifts))
    sorted_shifts = sorted(shifts)
    low = sorted_shifts[max(0, int(len(sorted_shifts) * 0.05) - 1)]
    high = sorted_shifts[min(len(sorted_shifts) - 1, int(len(sorted_shifts) * 0.95))]
    spread = float(high - low)
    return {
        "offset_seconds": round(median_shift, 3),
        "framerate_scale_factor": 1.0,
        "alass_shift_spread_seconds": round(spread, 3),
        "alass_constant_shift": spread <= 0.35,
    }


def _gate_embedded_reference_alass_discontinuity(
    result: dict[str, object],
    activity: dict[str, object],
    *,
    reference_ok: bool,
    reference_reason: str,
) -> tuple[bool, str, dict[str, object]]:
    # ALASS may preserve all cues but attach a block to the wrong dialogue-dense
    # scene. Global activity overlap alone cannot prove a huge block jump is real.
    if not reference_ok:
        return reference_ok, reference_reason, {
            "checked": False,
            "reason": "reference_already_unreliable",
        }

    try:
        spread = abs(float(result.get("alass_shift_spread_seconds") or 0.0))
        blocks = int(result.get("alass_blocks") or 0)
        distinct = int(result.get("alass_distinct_shifts") or 0)
    except (TypeError, ValueError):
        return reference_ok, reference_reason, {
            "checked": False,
            "reason": "shift_metrics_unavailable",
        }

    if spread < 30.0 or max(blocks, distinct) < 3:
        return reference_ok, reference_reason, {
            "checked": True,
            "accepted": True,
            "reason": "shift_map_not_extreme",
            "spread_seconds": round(spread, 3),
            "blocks": blocks,
            "distinct_shifts": distinct,
        }

    repair = result.get("reference_piecewise_repair")
    repair_data = repair if isinstance(repair, dict) else {}
    edit_boundaries = repair_data.get("edit_boundaries")
    confirmed_boundary = bool(
        repair_data.get("applied")
        or (isinstance(edit_boundaries, list) and edit_boundaries)
    )
    if confirmed_boundary:
        return reference_ok, reference_reason, {
            "checked": True,
            "accepted": True,
            "reason": "extreme_map_confirmed_by_piecewise_boundary",
            "spread_seconds": round(spread, 3),
            "blocks": blocks,
            "distinct_shifts": distinct,
        }

    def metric(name: str) -> float:
        try:
            return float(activity.get(name) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    start = metric("start")
    middle = metric("middle")
    end = metric("end")
    weighted = metric("weighted")

    strong_local_confirmation = (
        start >= 0.72
        and middle >= 0.78
        and end >= 0.72
        and weighted >= 0.75
    )
    details = {
        "checked": True,
        "accepted": bool(strong_local_confirmation),
        "reason": (
            "extreme_map_strongly_confirmed"
            if strong_local_confirmation
            else "extreme_unconfirmed_alass_discontinuity"
        ),
        "spread_seconds": round(spread, 3),
        "blocks": blocks,
        "distinct_shifts": distinct,
        "activity_start": round(start, 4),
        "activity_middle": round(middle, 4),
        "activity_end": round(end, 4),
        "activity_weighted": round(weighted, 4),
        "piecewise_reason": str(repair_data.get("reason") or ""),
        "piecewise_applied": bool(repair_data.get("applied")),
    }
    if strong_local_confirmation:
        return reference_ok, reference_reason, details

    return (
        False,
        "embedded_reference_extreme_unconfirmed_alass_discontinuity",
        details,
    )



def _apply_robust_semantic_activity_gate(
    validation: dict[str, object],
    activity: dict[str, object],
    *,
    minimum_activity: float = 0.65,
) -> dict[str, object]:
    """Cross-check semantic evidence against an independent timing signal."""
    if bool(validation.get("strict_semantic_acceptance")):
        return validation

    weighted = float(activity.get("weighted") or 0.0) if activity.get("available") else 0.0
    try:
        matched = int(validation.get("robust_matches") or validation.get("matched_samples") or 0)
        total = int(validation.get("total_samples") or 0)
        similarity = float(
            validation.get("robust_similarity")
            or validation.get("similarity")
            or 0.0
        )
    except (TypeError, ValueError):
        matched, total, similarity = 0, 0, 0.0

    scores = validation.get("sample_scores")
    complete_scores = isinstance(scores, list) and total > 0 and len(scores) == total

    if bool(validation.get("accepted")) and bool(
        validation.get("robust_semantic_acceptance")
    ):
        required_activity = float(minimum_activity)

        if total >= 5 and matched >= total and similarity >= 0.85:
            required_activity = 0.50
        elif total >= 5 and matched >= total - 1 and similarity >= 0.75:
            required_activity = 0.60
        elif total >= 6 and matched >= total - 2 and similarity >= 0.75:
            required_activity = 0.78

        validation["robust_activity_threshold"] = required_activity
        validation["robust_activity_score"] = round(weighted, 4)

        if weighted < required_activity:
            validation["accepted"] = False
            validation["reason"] = (
                f"robust_semantic_activity_too_low:"
                f"{weighted:.4f}<{required_activity:.4f}"
            )
        return validation

    if (
        not bool(validation.get("accepted"))
        and complete_scores
        and total >= 6
        and matched >= total - 2
        and similarity >= 0.75
    ):
        required_activity = 0.78
        validation["robust_activity_threshold"] = required_activity
        validation["robust_activity_score"] = round(weighted, 4)

        if weighted >= required_activity:
            validation["accepted"] = True
            validation["activity_assisted_semantic_acceptance"] = True
            validation["matched_samples"] = max(
                int(validation.get("matched_samples") or 0),
                matched,
            )
            validation["similarity"] = max(
                float(validation.get("similarity") or 0.0),
                similarity,
            )
            validation["reason"] = (
                "accepted_from_sample_scores_with_activity: "
                + str(validation.get("reason") or "semantic majority")
            )

    return validation


def _cue_onsets(
    cues: list[tuple[float, float, str]],
    *,
    merge_within: float = 0.12,
) -> list[float]:
    """Return dialogue onset times, collapsing near-duplicate layered cues."""
    result: list[float] = []
    for start, _end, _text in sorted(cues, key=lambda item: item[0]):
        if result and start - result[-1] <= merge_within:
            continue
        result.append(start)
    return result


def _activity_overlap_for_shift(
    source_cues: list[tuple[float, float, str]],
    reference_cues: list[tuple[float, float, str]],
    shift_seconds: float,
) -> float:
    """Dice overlap of subtitle-active intervals after one constant shift."""
    shifted = [
        (start + shift_seconds, end + shift_seconds, text)
        for start, end, text in source_cues
        if end + shift_seconds > 0.0
    ]
    if not shifted or not reference_cues:
        return 0.0
    source_intervals = _merge_intervals(shifted)
    reference_intervals = _merge_intervals(reference_cues)
    duration = max(
        max(end for _start, end, _text in shifted),
        max(end for _start, end, _text in reference_cues),
    )
    source_length = _interval_length(source_intervals, 0.0, duration)
    reference_length = _interval_length(reference_intervals, 0.0, duration)
    intersection = _interval_intersection_length(
        source_intervals,
        reference_intervals,
        0.0,
        duration,
    )
    denominator = source_length + reference_length
    return 0.0 if denominator <= 0 else 2.0 * intersection / denominator


def _matched_onset_pairs(
    candidate_starts: list[float],
    reference_starts: list[float],
    *,
    tolerance: float,
) -> list[tuple[int, int, float]]:
    """Return monotonic one-to-one onset matches and their absolute errors."""
    candidate = sorted(enumerate(candidate_starts), key=lambda item: item[1])
    reference = sorted(enumerate(reference_starts), key=lambda item: item[1])
    i = j = 0
    pairs: list[tuple[int, int, float]] = []
    while i < len(candidate) and j < len(reference):
        candidate_index, candidate_time = candidate[i]
        reference_index, reference_time = reference[j]
        delta = candidate_time - reference_time
        if abs(delta) <= tolerance:
            if j + 1 < len(reference):
                next_index, next_time = reference[j + 1]
                if abs(candidate_time - next_time) < abs(delta) and abs(candidate_time - next_time) <= tolerance:
                    j += 1
                    reference_index, reference_time = next_index, next_time
                    delta = candidate_time - reference_time
            pairs.append((candidate_index, reference_index, abs(delta)))
            i += 1
            j += 1
        elif delta < -tolerance:
            i += 1
        else:
            j += 1
    return pairs


def _onset_candidate_shifts(
    source_starts: list[float],
    reference_starts: list[float],
    *,
    limit: float,
    bin_seconds: float = 0.20,
    maximum_candidates: int = 48,
) -> list[float]:
    """Find shift hypotheses from dense peaks of pairwise onset differences."""
    bins: dict[int, int] = {}
    for source_time in source_starts:
        for reference_time in reference_starts:
            shift = reference_time - source_time
            if shift < -limit:
                continue
            if shift > limit:
                break
            key = int(round(shift / bin_seconds))
            bins[key] = bins.get(key, 0) + 1
    ranked = sorted(bins.items(), key=lambda item: (item[1], -abs(item[0])), reverse=True)
    shifts = [key * bin_seconds for key, _votes in ranked[:maximum_candidates]]
    if 0.0 not in shifts:
        shifts.append(0.0)
    return shifts



def _activity_correlation_for_shift(
    source_cues: list[tuple[float, float, str]],
    reference_cues: list[tuple[float, float, str]],
    shift_seconds: float,
    *,
    bin_seconds: float = 0.50,
) -> float:
    """Centered correlation of subtitle-active masks after a constant shift.

    Raw overlap rewards dense dialogue and can create false peaks one opening or
    recap away. Centering the binary masks also penalizes activity where the
    other track is silent, which is much more discriminative across languages.
    """
    shifted = [
        (start + shift_seconds, end + shift_seconds, text)
        for start, end, text in source_cues
        if end + shift_seconds > 0.0
    ]
    if not shifted or not reference_cues:
        return 0.0
    duration = max(
        max(end for _start, end, _text in shifted),
        max(end for _start, end, _text in reference_cues),
    )
    if duration <= 0.0:
        return 0.0
    step = max(0.20, min(1.0, float(bin_seconds)))
    count = max(1, int(duration / step) + 1)

    def mask(cues: list[tuple[float, float, str]]) -> list[bool]:
        diff = [0] * (count + 1)
        for start, end, _text in cues:
            left = max(0, min(count - 1, int(start / step)))
            right = max(left + 1, min(count, int(end / step) + 1))
            diff[left] += 1
            diff[right] -= 1
        active = 0
        result: list[bool] = []
        for index in range(count):
            active += diff[index]
            result.append(active > 0)
        return result

    source_mask = mask(shifted)
    reference_mask = mask(reference_cues)
    n11 = n10 = n01 = n00 = 0
    for source_active, reference_active in zip(source_mask, reference_mask):
        if source_active and reference_active:
            n11 += 1
        elif source_active:
            n10 += 1
        elif reference_active:
            n01 += 1
        else:
            n00 += 1
    denominator = (
        (n11 + n10)
        * (n11 + n01)
        * (n00 + n10)
        * (n00 + n01)
    ) ** 0.5
    if denominator <= 0.0:
        return 0.0
    return (n11 * n00 - n10 * n01) / denominator


def _landmark_shift_votes(
    source_starts: list[float],
    reference_starts: list[float],
    *,
    limit: float,
    shift_bin_seconds: float = 0.20,
    gap_bin_seconds: float = 0.25,
) -> dict[int, float]:
    """Vote for shifts using local inter-onset fingerprints.

    Matching two consecutive gaps is far less likely by chance than matching a
    single onset in dense dialogue. Single long-gap landmarks are included with
    lower weight so the method still works when one translation splits cues.
    """
    votes: dict[int, float] = {}

    def add_vote(shift: float, weight: float) -> None:
        if -limit <= shift <= limit:
            key = int(round(shift / shift_bin_seconds))
            votes[key] = votes.get(key, 0.0) + weight

    reference_triples: dict[tuple[int, int], list[int]] = {}
    for index in range(len(reference_starts) - 2):
        first = reference_starts[index + 1] - reference_starts[index]
        second = reference_starts[index + 2] - reference_starts[index + 1]
        if not (0.45 <= first <= 25.0 and 0.45 <= second <= 25.0):
            continue
        signature = (int(round(first / gap_bin_seconds)), int(round(second / gap_bin_seconds)))
        reference_triples.setdefault(signature, []).append(index)

    for index in range(len(source_starts) - 2):
        first = source_starts[index + 1] - source_starts[index]
        second = source_starts[index + 2] - source_starts[index + 1]
        if not (0.45 <= first <= 25.0 and 0.45 <= second <= 25.0):
            continue
        signature = (int(round(first / gap_bin_seconds)), int(round(second / gap_bin_seconds)))
        for reference_index in reference_triples.get(signature, []):
            add_vote(reference_starts[reference_index] - source_starts[index], 4.0)

    reference_long_gaps: dict[int, list[int]] = {}
    for index in range(len(reference_starts) - 1):
        gap = reference_starts[index + 1] - reference_starts[index]
        if gap < 4.0 or gap > 40.0:
            continue
        reference_long_gaps.setdefault(int(round(gap / gap_bin_seconds)), []).append(index)

    for index in range(len(source_starts) - 1):
        gap = source_starts[index + 1] - source_starts[index]
        if gap < 4.0 or gap > 40.0:
            continue
        signature = int(round(gap / gap_bin_seconds))
        for nearby in (signature - 1, signature, signature + 1):
            for reference_index in reference_long_gaps.get(nearby, []):
                weight = min(3.0, 0.5 + gap / 12.0)
                add_vote(reference_starts[reference_index] - source_starts[index], weight)
    return votes


def estimate_constant_subtitle_offsets(
    source: Path,
    reference: Path,
    *,
    max_offset_seconds: float = 120.0,
    onset_tolerance_seconds: float = 0.45,
    maximum_results: int = 6,
) -> list[dict[str, object]]:
    """Return several plausible global subtitle offsets before audio analysis.

    A single raw maximum is unsafe: openings, recaps and dense dialogue can
    produce a stronger accidental peak than the real translation alignment.
    We therefore retain diverse candidates and let semantic verification choose
    among them before falling back to audio/FFT.
    """
    try:
        source_cues = parse_srt(source)
        reference_cues = parse_srt(reference)
    except OSError as exc:
        return [{"available": False, "reason": "read_error", "error": str(exc)}]
    if len(source_cues) < 4 or len(reference_cues) < 4:
        return [{"available": False, "reason": "not_enough_cues"}]

    source_starts = _cue_onsets(source_cues)
    reference_starts = _cue_onsets(reference_cues)
    if len(source_starts) < 4 or len(reference_starts) < 4:
        return [{"available": False, "reason": "not_enough_onsets"}]

    limit = max(5.0, abs(float(max_offset_seconds)))
    strict_tolerance = max(0.18, min(0.65, float(onset_tolerance_seconds)))
    wide_tolerance = max(strict_tolerance + 0.25, min(1.0, strict_tolerance * 2.0))
    duration = max(reference_starts[-1], source_starts[-1], 1.0)
    region_count_total = 8
    landmark_votes = _landmark_shift_votes(source_starts, reference_starts, limit=limit)

    def evaluate(shift: float) -> tuple[tuple[float, ...], dict[str, object]]:
        shifted = [value + shift for value in source_starts]
        strict_pairs = _matched_onset_pairs(shifted, reference_starts, tolerance=strict_tolerance)
        wide_pairs = _matched_onset_pairs(shifted, reference_starts, tolerance=wide_tolerance)
        matched = len(strict_pairs)
        errors = [error for _source_index, _reference_index, error in strict_pairs]
        matched_shifts = [
            reference_starts[reference_index] - source_starts[source_index]
            for source_index, reference_index, _error in strict_pairs
        ]
        refined_shift = float(statistics.median(matched_shifts)) if matched_shifts else shift
        mean_error = sum(errors) / matched if matched else strict_tolerance * 4.0
        matched_reference_times = [reference_starts[reference_index] for _, reference_index, _ in strict_pairs]
        regions = {
            min(region_count_total - 1, int((time / duration) * region_count_total))
            for time in matched_reference_times
        }
        region_count = len(regions)
        edge_regions = int(0 in regions) + int((region_count_total - 1) in regions)
        span_ratio = (
            (max(matched_reference_times) - min(matched_reference_times)) / duration
            if len(matched_reference_times) >= 2
            else 0.0
        )

        consistent_transitions = 0
        comparable_transitions = 0
        longest_consistent_run = 0
        current_run = 0
        for left, right in zip(strict_pairs, strict_pairs[1:]):
            source_left, reference_left, _ = left
            source_right, reference_right, _ = right
            if source_right - source_left > 3 or reference_right - reference_left > 3:
                current_run = 0
                continue
            comparable_transitions += 1
            source_gap = source_starts[source_right] - source_starts[source_left]
            reference_gap = reference_starts[reference_right] - reference_starts[reference_left]
            if abs(source_gap - reference_gap) <= 0.75:
                consistent_transitions += 1
                current_run += 1
                longest_consistent_run = max(longest_consistent_run, current_run + 1)
            else:
                current_run = 0
        sequence_consistency = (
            consistent_transitions / comparable_transitions if comparable_transitions else 0.0
        )

        overlap_start = max(reference_starts[0], source_starts[0] + shift)
        overlap_end = min(reference_starts[-1], source_starts[-1] + shift)
        overlap_duration = max(0.001, overlap_end - overlap_start)
        source_in_overlap = sum(overlap_start <= value + shift <= overlap_end for value in source_starts)
        reference_in_overlap = sum(overlap_start <= value <= overlap_end for value in reference_starts)
        expected_random = (
            source_in_overlap
            * reference_in_overlap
            * min(1.0, (2.0 * strict_tolerance) / overlap_duration)
        )
        onset_excess = matched - expected_random
        onset_z = onset_excess / ((expected_random + 1.0) ** 0.5)

        activity = _activity_overlap_for_shift(source_cues, reference_cues, shift)
        activity_correlation = _activity_correlation_for_shift(
            source_cues,
            reference_cues,
            shift,
        )
        denominator = max(1, min(len(source_starts), len(reference_starts)))
        coverage = matched / denominator
        minimum_matches = max(6, int(round(denominator * 0.03)))
        distributed = region_count >= 4 and span_ratio >= 0.55
        landmark_key = int(round(shift / 0.20))
        landmark_support = sum(
            landmark_votes.get(landmark_key + delta, 0.0) for delta in (-1, 0, 1)
        )
        evidence = (
            onset_z >= 2.25
            or activity_correlation >= 0.08
            or landmark_support >= 8.0
            or longest_consistent_run >= 5
        )
        qualified = matched >= minimum_matches and distributed and evidence
        semantic_priority = (
            2.8 * max(-0.25, activity_correlation)
            + 0.11 * max(-2.0, onset_z)
            + 0.018 * min(30.0, landmark_support)
            + 0.35 * coverage
            + 0.025 * min(12, longest_consistent_run)
        )
        rank = (
            1.0 if qualified else 0.0,
            round(semantic_priority, 6),
            round(activity_correlation, 6),
            round(onset_z, 6),
            round(landmark_support, 6),
            float(matched),
            float(region_count),
            round(span_ratio, 6),
            -round(mean_error, 6),
            -abs(shift),
        )
        payload: dict[str, object] = {
            "available": True,
            "reason": "ok" if qualified else "weak_onset_structure",
            "offset_seconds": round(refined_shift, 3),
            "matched_onsets": matched,
            "wide_matched_onsets": len(wide_pairs),
            "source_onsets": len(source_starts),
            "reference_onsets": len(reference_starts),
            "onset_coverage": round(coverage, 4),
            "expected_random_matches": round(expected_random, 3),
            "onset_excess_matches": round(onset_excess, 3),
            "onset_z_score": round(onset_z, 4),
            "mean_onset_error_seconds": round(mean_error, 4) if matched else -1.0,
            "activity_overlap": round(activity, 4),
            "activity_correlation": round(activity_correlation, 4),
            "matched_regions": region_count,
            "edge_regions": edge_regions,
            "matched_span_ratio": round(span_ratio, 4),
            "sequence_consistency": round(sequence_consistency, 4),
            "longest_consistent_run": longest_consistent_run,
            "landmark_support": round(landmark_support, 3),
            "minimum_matches": minimum_matches,
            "strict_tolerance_seconds": strict_tolerance,
            "wide_tolerance_seconds": wide_tolerance,
            "semantic_priority": round(semantic_priority, 4),
            "usable_for_semantic_sampling": qualified,
        }
        return rank, payload

    pair_hypotheses = _onset_candidate_shifts(
        source_starts,
        reference_starts,
        limit=limit,
        maximum_candidates=32,
    )
    landmark_hypotheses = [
        key * 0.20
        for key, _votes in sorted(
            landmark_votes.items(),
            key=lambda item: (item[1], -abs(item[0])),
            reverse=True,
        )[:16]
    ]
    hypotheses = list(dict.fromkeys([*pair_hypotheses, *landmark_hypotheses, 0.0]))
    evaluated: list[tuple[tuple[float, ...], dict[str, object]]] = []
    seen: set[int] = set()

    def evaluate_once(shift: float) -> None:
        shift = max(-limit, min(limit, shift))
        key = int(round(shift * 1000))
        if key in seen:
            return
        seen.add(key)
        evaluated.append(evaluate(shift))

    # The old implementation refined every coarse hypothesis with 13 expensive
    # activity evaluations. With ~100 hypotheses that meant >1000 full-episode
    # scans. Score coarse hypotheses once, then refine only the strongest seeds.
    for hypothesis in hypotheses:
        evaluate_once(hypothesis)

    coarse_ranked = sorted(evaluated, key=lambda item: item[0], reverse=True)
    refinement_seeds: list[float] = []

    def add_refinement_seeds(
        items: list[tuple[tuple[float, ...], dict[str, object]]],
        target_count: int,
    ) -> None:
        for _rank, payload in items:
            if len(refinement_seeds) >= target_count:
                return
            shift = float(payload.get("offset_seconds") or 0.0)
            if any(abs(shift - existing) < 0.35 for existing in refinement_seeds):
                continue
            refinement_seeds.append(shift)

    add_refinement_seeds(coarse_ranked, 4)
    add_refinement_seeds(
        sorted(
            evaluated,
            key=lambda item: float(item[1]["activity_correlation"]),
            reverse=True,
        ),
        6,
    )
    add_refinement_seeds(
        sorted(evaluated, key=lambda item: float(item[1]["onset_z_score"]), reverse=True),
        7,
    )
    add_refinement_seeds(
        sorted(evaluated, key=lambda item: float(item[1]["landmark_support"]), reverse=True),
        8,
    )

    for seed in refinement_seeds:
        for step in range(-4, 5):
            evaluate_once(seed + step * 0.05)

    ranked = sorted(evaluated, key=lambda item: item[0], reverse=True)
    selected: list[dict[str, object]] = []

    def add_diverse(
        items: list[tuple[tuple[float, ...], dict[str, object]]],
        target_count: int,
    ) -> None:
        for _rank, payload in items:
            if len(selected) >= target_count:
                return
            shift = float(payload.get("offset_seconds") or 0.0)
            if any(abs(shift - float(item.get("offset_seconds") or 0.0)) < 1.5 for item in selected):
                continue
            selected.append(dict(payload))

    # Reserve slots for metrics that fail differently. This lets semantic
    # verification recover when a recap/opening wins the combined onset score.
    maximum = max(1, int(maximum_results))
    add_diverse(ranked, min(maximum, 3))
    add_diverse(
        sorted(
            evaluated,
            key=lambda item: float(item[1]["activity_correlation"]),
            reverse=True,
        ),
        min(maximum, 4),
    )
    add_diverse(
        sorted(evaluated, key=lambda item: float(item[1]["onset_z_score"]), reverse=True),
        min(maximum, 5),
    )
    add_diverse(
        sorted(evaluated, key=lambda item: float(item[1]["landmark_support"]), reverse=True),
        maximum,
    )

    for index, payload in enumerate(selected, start=1):
        payload["candidate_rank"] = index
        payload["candidate_hypotheses"] = len(hypotheses)
        payload["candidate_evaluations"] = len(evaluated)
    return selected or [{"available": False, "reason": "no_candidates"}]


def estimate_constant_subtitle_offset(
    source: Path,
    reference: Path,
    *,
    max_offset_seconds: float = 120.0,
    onset_tolerance_seconds: float = 0.45,
) -> dict[str, object]:
    """Backward-compatible best-candidate wrapper."""
    return estimate_constant_subtitle_offsets(
        source,
        reference,
        max_offset_seconds=max_offset_seconds,
        onset_tolerance_seconds=onset_tolerance_seconds,
        maximum_results=1,
    )[0]


def _shift_subtitle_for_semantic_validation(
    source: Path,
    cache_dir: Path,
    offset_seconds: float,
    *,
    force: bool = False,
) -> Path:
    """Write a temporary constant-shifted SRT used only for LLM sampling."""
    stat = source.stat()
    digest = hashlib.sha1(
        (
            f"semantic-offset-v1:{source.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:"
            f"{offset_seconds:.3f}"
        ).encode()
    ).hexdigest()[:20]
    output_dir = cache_dir / "semantic-offset"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{digest}.srt"
    if force:
        output.unlink(missing_ok=True)
    if output.exists() and output.stat().st_size > 0:
        return output

    shifted: list[tuple[float, float, str]] = []
    for start, end, text in parse_srt(source):
        new_start = start + offset_seconds
        new_end = end + offset_seconds
        if new_end <= 0.0:
            continue
        shifted.append((max(0.0, new_start), max(0.05, new_end), text))
    write_srt(shifted, output)
    return output


def _metadata_path(output: Path) -> Path:
    return output.with_name(output.name + ".json")


def _write_metadata(output: Path, result: dict[str, object]) -> None:
    serializable = {
        key: value
        for key, value in result.items()
        if isinstance(value, (str, int, float, bool)) or value is None
    }
    _metadata_path(output).write_text(
        json.dumps(serializable, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _read_metadata(output: Path) -> dict[str, object]:
    path = _metadata_path(output)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return dict(payload) if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


class _AlignmentScoreHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.score: float | None = None

    def emit(self, record: logging.LogRecord) -> None:
        match = _SCORE_RE.search(record.getMessage())
        if match:
            try:
                self.score = float(match.group(1))
            except ValueError:
                pass


class _SuppressHarmlessSpeechProbe(logging.Filter):
    """Hide ffsubsync's informational probe for embedded subtitle streams.

    Audio/WAV references normally have no subtitle stream. ffsubsync reports this
    at INFO level before falling back to voice activity detection, which is the
    expected path for pudge rather than an error.
    """

    _IGNORED = (
        "Checking video for subtitles stream",
        "Video file appears to lack subtitle stream",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(fragment in message for fragment in self._IGNORED)


_SPEECH_PROBE_FILTER = _SuppressHarmlessSpeechProbe()


def prepare_speech_reference(
    video: Path,
    cache_dir: Path,
    config: SyncConfig,
    *,
    ffmpeg_path: str | None = None,
    force: bool = False,
    verbose: bool = False,
) -> tuple[Path | None, dict[str, object]]:
    """Extract VAD speech once and reuse it for every subtitle candidate."""
    video_stat = video.stat()
    raw = (
        f"{video.resolve()}:{video_stat.st_size}:{video_stat.st_mtime_ns}:"
        f"{config.vad}:{ffmpeg_path or ''}"
    )
    digest = hashlib.sha1(raw.encode()).hexdigest()[:20]
    output_dir = cache_dir / "speech"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{digest}.npz"
    if force:
        output.unlink(missing_ok=True)
    if output.exists() and output.stat().st_size > 0:
        return output, _result("cached", cached=True, output=str(output))

    try:
        import numpy as np
        from ffsubsync import ffsubsync as ffsubsync_impl
    except ImportError:
        return None, _result("ffsubsync_missing")

    arguments = [str(video)]
    if config.vad and config.vad != "default":
        arguments.extend(["--vad", config.vad])
    if ffmpeg_path:
        resolved = shutil.which(ffmpeg_path) if "/" not in ffmpeg_path else ffmpeg_path
        if resolved:
            arguments.extend(["--ffmpeg-path", resolved])
    speech_logger = logging.getLogger("ffsubsync.speech_transformers")
    speech_logger.addFilter(_SPEECH_PROBE_FILTER)
    try:
        args = ffsubsync_impl.make_parser().parse_args(arguments)
        reference_pipe = ffsubsync_impl.make_reference_pipe(args)
        reference_pipe.fit(str(video))
        np.savez_compressed(output, speech=reference_pipe.transform(str(video)))
    except (Exception, SystemExit) as exc:
        output.unlink(missing_ok=True)
        return None, _result(
            "speech_cache_error",
            error=str(exc),
            arguments=arguments if verbose else None,
        )
    finally:
        speech_logger.removeFilter(_SPEECH_PROBE_FILTER)
    return output, _result("applied", output=str(output))


def synchronize_subtitle(
    video: Path,
    subtitle: Path,
    cache_dir: Path,
    config: SyncConfig,
    *,
    ffmpeg_path: str | None = None,
    force: bool = False,
    verbose: bool = False,
    quiet: bool = False,
    tag: str = "ffsubsync",
    reference: Path | None = None,
) -> tuple[Path, dict[str, object]]:
    """Apply global ffsubsync alignment and expose its raw alignment score."""
    if not config.enabled:
        return subtitle, _result("disabled", sync_was_successful=False)
    if subtitle.suffix.casefold() not in TEXT_SUBTITLE_EXTENSIONS:
        return subtitle, _result("unsupported_format", sync_was_successful=False)

    output_dir = cache_dir / "synced"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{_fingerprint(video, subtitle, config, tag=tag)}{subtitle.suffix.casefold()}"
    if force:
        output.unlink(missing_ok=True)
        _metadata_path(output).unlink(missing_ok=True)
    if output.exists() and output.stat().st_size > 0:
        cached = _read_metadata(output)
        return output, _result(
            "cached",
            cached=True,
            sync_was_successful=True,
            output=str(output),
            **{key: value for key, value in cached.items() if key not in {"reason", "cached", "sync_was_successful", "output"}},
        )

    try:
        from ffsubsync import ffsubsync as ffsubsync_impl
    except ImportError:
        return subtitle, _result("ffsubsync_missing", sync_was_successful=False)

    arguments = [
        str(reference or video),
        "-i",
        str(subtitle),
        "-o",
        str(output),
        "--max-offset-seconds",
        str(config.max_offset_seconds),
    ]
    if reference is None and config.vad and config.vad != "default":
        arguments.extend(["--vad", config.vad])
    if not config.fix_framerate:
        arguments.append("--no-fix-framerate")
    if config.gss:
        arguments.append("--gss")
    if ffmpeg_path:
        resolved = shutil.which(ffmpeg_path) if "/" not in ffmpeg_path else ffmpeg_path
        if resolved:
            arguments.extend(["--ffmpeg-path", resolved])

    score_handler = _AlignmentScoreHandler()
    logger = getattr(ffsubsync_impl, "logger", logging.getLogger("ffsubsync.ffsubsync"))
    speech_logger = logging.getLogger("ffsubsync.speech_transformers")
    previous_level = logger.level
    previous_propagate = logger.propagate
    noisy_loggers = [
        logging.getLogger("ffsubsync.subtitle_parser"),
        logging.getLogger("ffsubsync.speech_transformers"),
        # The third-party `srt` package logs every cue discarded below t=0.
        # Internal probes may intentionally explore such offsets; one summary is
        # enough and hundreds of per-cue lines only hide the actual decision.
        logging.getLogger("srt"),
    ]
    noisy_levels = [item.level for item in noisy_loggers]
    handler_levels = [(handler, handler.level) for handler in logger.handlers]
    logger.addHandler(score_handler)
    speech_logger.addFilter(_SPEECH_PROBE_FILTER)
    logger.propagate = False
    if logger.getEffectiveLevel() > logging.INFO:
        logger.setLevel(logging.INFO)
    if quiet:
        for noisy_logger in noisy_loggers:
            noisy_logger.setLevel(logging.WARNING)
        for handler, _ in handler_levels:
            handler.setLevel(logging.WARNING)
    sink = io.StringIO()
    output_context = (
        redirect_stdout(sink) if quiet else nullcontext()
    )
    error_context = (
        redirect_stderr(sink) if quiet else nullcontext()
    )
    try:
        with output_context, error_context:
            parsed = ffsubsync_impl.make_parser().parse_args(arguments)
            result = dict(ffsubsync_impl.run(parsed))
    except (Exception, SystemExit) as exc:  # argparse reports failures through SystemExit
        output.unlink(missing_ok=True)
        return subtitle, _result(
            "ffsubsync_error",
            sync_was_successful=False,
            error=str(exc),
            arguments=arguments if verbose else None,
        )
    finally:
        logger.removeHandler(score_handler)
        speech_logger.removeFilter(_SPEECH_PROBE_FILTER)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
        for noisy_logger, level in zip(noisy_loggers, noisy_levels):
            noisy_logger.setLevel(level)
        for handler, level in handler_levels:
            handler.setLevel(level)

    if score_handler.score is not None:
        result["alignment_score"] = score_handler.score

    successful = bool(result.get("sync_was_successful"))
    offset_raw = result.get("offset_seconds")
    try:
        offset = float(offset_raw) if offset_raw is not None else None
    except (TypeError, ValueError):
        offset = None

    if not successful:
        output.unlink(missing_ok=True)
        result["reason"] = "alignment_failed"
        return subtitle, result

    if not output.exists() or output.stat().st_size == 0:
        result["reason"] = "output_missing"
        result["sync_was_successful"] = False
        return subtitle, result

    if (
        config.skip_on_low_quality
        and offset is not None
        and abs(offset) > config.quality_max_offset_seconds
    ):
        output.unlink(missing_ok=True)
        result["reason"] = "quality_offset_exceeded"
        result["sync_was_successful"] = False
        result["quality_max_offset_seconds"] = config.quality_max_offset_seconds
        return subtitle, result

    result["reason"] = "applied"
    result["output"] = str(output)
    _write_metadata(output, result)
    return output, result


def _resolve_alass(command: str) -> str | None:
    candidates = [command]
    if command in {"alass", "alass-cli"}:
        candidates.extend(["alass", "alass-cli"])
    for candidate in dict.fromkeys(candidates):
        resolved = shutil.which(candidate) if "/" not in candidate else candidate
        if resolved and Path(resolved).is_file():
            return resolved
    return None


def synchronize_with_alass(
    video: Path,
    subtitle: Path,
    cache_dir: Path,
    config: SyncConfig,
    *,
    alass_path: str = "alass",
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
    force: bool = False,
    verbose: bool = False,
) -> tuple[Path, dict[str, object]]:
    """Use ALASS for non-linear timing, including internal cuts/splits.

    ``video`` may also be a subtitle file. For subtitle-to-subtitle alignment we
    normalize both sides to clean UTF-8 SRT first. This avoids silent ALASS
    failures caused by BOMs, ASS styling, malformed indices, or odd filenames.
    """
    source_suffix = subtitle.suffix.casefold()
    if source_suffix not in {".srt", ".ass", ".ssa"}:
        return subtitle, _result("alass_unsupported_format", sync_was_successful=False)
    binary = _resolve_alass(alass_path)
    if binary is None:
        return subtitle, _result("alass_missing", sync_was_successful=False)

    reference_is_subtitle = video.suffix.casefold() in {".srt", ".ass", ".ssa"}
    reference_input = video
    source_input = subtitle
    normalization: dict[str, object] = {}

    if reference_is_subtitle and video.suffix.casefold() in {".ass", ".ssa"}:
        reference_input, reference_conversion = convert_to_plain_srt(
            video,
            cache_dir,
            ffmpeg_path=ffmpeg_path,
            force=force,
            verbose=verbose,
        )
        normalization["reference_conversion"] = reference_conversion.get("reason")
    if reference_is_subtitle and subtitle.suffix.casefold() in {".ass", ".ssa"}:
        source_input, source_conversion = convert_to_plain_srt(
            subtitle,
            cache_dir,
            ffmpeg_path=ffmpeg_path,
            force=force,
            verbose=verbose,
        )
        normalization["source_conversion"] = source_conversion.get("reason")

    output_suffix = source_input.suffix.casefold()
    if output_suffix not in {".srt", ".ass", ".ssa"}:
        output_suffix = source_suffix

    output_dir = cache_dir / "alass"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{_fingerprint(video, subtitle, config, tag='alass-direct-v2')}{output_suffix}"
    if force:
        output.unlink(missing_ok=True)
        _metadata_path(output).unlink(missing_ok=True)
    if output.exists() and output.stat().st_size > 0:
        cached = _read_metadata(output)
        return output, _result(
            "cached",
            cached=True,
            sync_was_successful=True,
            engine="alass",
            output=str(output),
            reference_kind="subtitle" if reference_is_subtitle else "media",
            **{
                key: value
                for key, value in cached.items()
                if key
                not in {
                    "reason",
                    "cached",
                    "sync_was_successful",
                    "engine",
                    "output",
                    "reference_kind",
                }
            },
        )

    with tempfile.TemporaryDirectory(prefix=f"{APP_SLUG}-alass-") as tmp_raw:
        tmp = Path(tmp_raw)
        safe_reference = tmp / (
            f"reference{reference_input.suffix.casefold()}"
            if reference_is_subtitle
            else f"video{reference_input.suffix.casefold()}"
        )
        safe_source = tmp / f"input{source_input.suffix.casefold()}"
        safe_output = tmp / f"output{output_suffix}"

        try:
            if reference_is_subtitle and reference_input.suffix.casefold() == ".srt":
                reference_cues = parse_srt(reference_input)
                if not reference_cues:
                    return subtitle, _result(
                        "alass_reference_empty",
                        sync_was_successful=False,
                        reference_kind="subtitle",
                    )
                write_srt(reference_cues, safe_reference)
            elif reference_is_subtitle:
                shutil.copy2(reference_input, safe_reference)
            else:
                try:
                    os.symlink(reference_input, safe_reference)
                except OSError:
                    shutil.copy2(reference_input, safe_reference)

            if source_input.suffix.casefold() == ".srt":
                source_cues = parse_srt(source_input)
                if not source_cues:
                    return subtitle, _result(
                        "alass_source_empty",
                        sync_was_successful=False,
                        reference_kind="subtitle" if reference_is_subtitle else "media",
                    )
                write_srt(source_cues, safe_source)
            else:
                shutil.copy2(source_input, safe_source)
        except OSError as exc:
            return subtitle, _result(
                "alass_prepare_error",
                sync_was_successful=False,
                error=str(exc),
                reference_kind="subtitle" if reference_is_subtitle else "media",
            )

        command = [
            binary,
            str(safe_reference),
            str(safe_source),
            str(safe_output),
            "--split-penalty",
            str(config.alass_split_penalty),
        ]
        env = os.environ.copy()
        env["ALASS_FFMPEG_PATH"] = ffmpeg_path
        env["ALASS_FFPROBE_PATH"] = ffprobe_path
        try:
            with timed_step(
                configure_logging(),
                "alass.run",
                reference_kind="subtitle" if reference_is_subtitle else "media",
                source=subtitle.name,
            ):
                completed = subprocess.run(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=config.alass_timeout_seconds,
                    check=False,
                    env=env,
                )
        except subprocess.TimeoutExpired as exc:
            return subtitle, _result(
                "alass_timeout",
                sync_was_successful=False,
                error=f"ALASS превысил timeout {config.alass_timeout_seconds}s",
                reference_kind="subtitle" if reference_is_subtitle else "media",
                command_output=(exc.stdout or "")[-2000:] if isinstance(exc.stdout, str) else "",
            )
        except OSError as exc:
            return subtitle, _result(
                "alass_error",
                sync_was_successful=False,
                error=str(exc),
                reference_kind="subtitle" if reference_is_subtitle else "media",
            )

        command_output = completed.stdout or ""
        if completed.returncode != 0 or not safe_output.exists() or safe_output.stat().st_size == 0:
            detail = command_output[-2000:].strip() or "ALASS не создал выходной файл"
            return subtitle, _result(
                "alass_error",
                sync_was_successful=False,
                exit_code=completed.returncode,
                error=detail,
                command_output=detail,
                reference_kind="subtitle" if reference_is_subtitle else "media",
            )
        shutil.copy2(safe_output, output)

    shifts = _ALASS_SHIFT_RE.findall(command_output)
    shift_summary = _subtitle_shift_summary(source_input, output) if reference_is_subtitle else {}
    result = _result(
        "applied",
        sync_was_successful=True,
        engine="alass",
        output=str(output),
        reference_kind="subtitle" if reference_is_subtitle else "media",
        alass_blocks=len(shifts),
        alass_distinct_shifts=len(set(shifts)),
        **shift_summary,
        **normalization,
    )
    _write_metadata(output, result)
    return output, result



def _resolve_command(command: str) -> str | None:
    resolved = shutil.which(command) if "/" not in command else command
    return resolved if resolved and Path(resolved).is_file() else None


def _probe_duration(video: Path, ffprobe_path: str) -> float | None:
    binary = _resolve_command(ffprobe_path)
    if binary is None:
        return None
    command = [
        binary,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video),
    ]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=20,
            check=False,
        )
        duration = float(completed.stdout.strip())
        return duration if duration > 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _select_timing_reference_stream(streams: list[dict[str, object]]) -> dict[str, object] | None:
    """Choose a full text subtitle track already synchronized to the video.

    English/default streaming-service tracks are preferred. Forced/sign/song-only
    tracks are intentionally rejected because their sparse timing is a poor
    structural reference for Japanese dialogue subtitles.
    """

    ranked: list[tuple[float, dict[str, object]]] = []
    for stream in streams:
        if str(stream.get("codec_type", "")).casefold() != "subtitle":
            continue
        codec = str(stream.get("codec_name", "")).casefold()
        if codec not in _TEXT_REFERENCE_CODECS:
            continue
        tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
        disposition = (
            stream.get("disposition") if isinstance(stream.get("disposition"), dict) else {}
        )
        language = str(tags.get("language", "")).casefold().replace("_", "-")
        title = str(tags.get("title", ""))
        lowered = title.casefold()
        score = 0.0
        if language in {"eng", "en", "en-us", "en-gb"}:
            score += 120.0
        elif language in {"rus", "ru", "spa", "es", "deu", "ger", "de", "fra", "fre", "fr"}:
            score += 60.0
        elif language in {"jpn", "ja", "jp"}:
            score -= 150.0
        if int(disposition.get("default", 0) or 0):
            score += 25.0
        if any(marker in lowered for marker in ("cr", "dialog", "full")):
            score += 15.0
        if any(marker in lowered for marker in ("forced", "sign", "song", "karaoke")):
            score -= 120.0
        if codec in {"subrip", "srt"}:
            score += 5.0
        try:
            index = int(stream["index"])
        except (KeyError, TypeError, ValueError):
            continue
        candidate = dict(stream)
        candidate["index"] = index
        candidate["_timing_score"] = score
        ranked.append((score, candidate))
    if not ranked:
        return None
    score, stream = max(ranked, key=lambda item: item[0])
    return stream if score >= 20.0 else None


def extract_embedded_timing_reference(
    video: Path,
    cache_dir: Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
    force: bool = False,
    verbose: bool = False,
) -> tuple[Path | None, dict[str, object]]:
    """Extract the best embedded non-Japanese text subtitle as plain SRT."""

    ffprobe = _resolve_command(ffprobe_path)
    ffmpeg = _resolve_command(ffmpeg_path)
    if ffprobe is None or ffmpeg is None:
        return None, _result("timing_reference_tools_missing")
    try:
        completed = subprocess.run(
            [ffprobe, "-v", "error", "-show_streams", "-of", "json", str(video)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
        payload = json.loads(completed.stdout) if completed.returncode == 0 else {}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return None, _result("timing_reference_probe_failed", error=str(exc))
    streams = payload.get("streams", []) if isinstance(payload, dict) else []
    stream = _select_timing_reference_stream(streams if isinstance(streams, list) else [])
    if stream is None:
        return None, _result("timing_reference_not_found")

    index = int(stream["index"])
    tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
    language = str(tags.get("language", ""))
    title = str(tags.get("title", ""))
    stat = video.stat()
    digest = hashlib.sha1(
        (
            f"{video.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:"
            f"stream={index}:timing-reference-v1"
        ).encode()
    ).hexdigest()[:20]
    output_dir = cache_dir / "timing-reference"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{digest}.srt"
    if force:
        output.unlink(missing_ok=True)
    if output.exists() and output.stat().st_size > 0:
        return output, _result(
            "cached",
            output=str(output),
            stream_index=index,
            language=language,
            title=title,
        )
    command = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-map",
        f"0:{index}",
        "-c:s",
        "srt",
        str(output),
    ]
    try:
        extracted = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        output.unlink(missing_ok=True)
        return None, _result("timing_reference_extract_failed", error=str(exc))
    if extracted.returncode != 0 or not output.exists() or output.stat().st_size == 0:
        output.unlink(missing_ok=True)
        return None, _result(
            "timing_reference_extract_failed",
            error=extracted.stdout[-1000:] if verbose else "ffmpeg не извлёк дорожку",
        )
    return output, _result(
        "applied",
        output=str(output),
        stream_index=index,
        language=language,
        title=title,
    )


def _segment_windows(duration: float, count: int, window_seconds: float) -> list[tuple[str, float, float]]:
    count = max(3, count)
    # Short windows are useful around openings/endings. Older versions silently
    # forced every value below 30 seconds back to 30, making the UI misleading.
    window = min(max(5.0, window_seconds), duration)
    if duration <= window + 1:
        return [("full", 0.0, duration)]
    span = duration - window
    starts = [span * index / (count - 1) for index in range(count)]
    labels = ["start", *[f"part-{index}" for index in range(2, count)], "end"]
    windows: list[tuple[str, float, float]] = []
    seen: set[int] = set()
    for label, start in zip(labels, starts):
        key = round(start * 10)
        if key in seen:
            continue
        seen.add(key)
        windows.append((label, start, window))
    return windows


def _extract_audio_segment(
    video: Path,
    start: float,
    duration: float,
    cache_dir: Path,
    *,
    ffmpeg_path: str,
    force: bool,
    padding_seconds: float = 0.0,
) -> tuple[Path | None, str | None]:
    """Extract one local audio window, optionally surrounded by silence.

    Symmetric silence lets subtitle cues from before/after the nominal window be
    represented with non-negative timestamps. This is essential for measuring a
    local correction larger than the short validation window itself.
    """
    binary = _resolve_command(ffmpeg_path)
    if binary is None:
        return None, "ffmpeg_missing"
    padding = max(0.0, padding_seconds)
    stat = video.stat()
    digest = hashlib.sha1(
        (
            f"{video.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:"
            f"{start:.3f}:{duration:.3f}:{padding:.3f}:mono-16k-v2"
        ).encode()
    ).hexdigest()[:20]
    output_dir = cache_dir / "segment-audio"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{digest}.wav"
    if force:
        output.unlink(missing_ok=True)
    if output.exists() and output.stat().st_size > 0:
        return output, None
    command = [
        binary,
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(video),
        "-map",
        "0:a:0?",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
    ]
    if padding > 0:
        delay_ms = int(round(padding * 1000))
        command.extend(
            [
                "-af",
                f"adelay={delay_ms}:all=1,apad=pad_dur={padding:.3f}",
                "-t",
                f"{duration + 2 * padding:.3f}",
            ]
        )
    command.extend(["-c:a", "pcm_s16le", str(output)])
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=max(60.0, duration / 2),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        output.unlink(missing_ok=True)
        return None, str(exc)
    if completed.returncode != 0 or not output.exists() or output.stat().st_size == 0:
        output.unlink(missing_ok=True)
        return None, completed.stdout[-1000:]
    return output, None


def _clip_cues(
    cues: list[tuple[float, float, str]],
    start: float,
    duration: float,
) -> list[tuple[float, float, str]]:
    end = start + duration
    clipped: list[tuple[float, float, str]] = []
    for cue_start, cue_end, text in cues:
        if cue_end <= start or cue_start >= end:
            continue
        relative_start = max(cue_start, start) - start
        relative_end = min(cue_end, end) - start
        if relative_end > relative_start:
            clipped.append((relative_start, relative_end, text))
    return clipped


def _clip_cues_for_local_alignment(
    cues: list[tuple[float, float, str]],
    start: float,
    duration: float,
    padding_seconds: float,
) -> list[tuple[float, float, str]]:
    """Keep cues near a video window without destroying large local offsets.

    A cue can belong to the current video window even when its source timestamp
    differs by tens of seconds. We therefore include a padded subtitle range and
    shift both it and the audio onto a non-negative padded timeline.
    """
    padding = max(0.0, padding_seconds)
    source_start = max(0.0, start - padding)
    source_end = start + duration + padding
    timeline_shift = padding - start
    clipped: list[tuple[float, float, str]] = []
    for cue_start, cue_end, text in cues:
        if cue_end <= source_start or cue_start >= source_end:
            continue
        relative_start = max(cue_start, source_start) + timeline_shift
        relative_end = min(cue_end, source_end) + timeline_shift
        if relative_end > relative_start:
            clipped.append((max(0.0, relative_start), max(0.05, relative_end), text))
    return clipped


def _largest_offset_cluster_fraction(values: list[float], tolerance: float) -> float:
    if not values:
        return 0.0
    tolerance = max(0.1, tolerance)
    largest = 1
    for center in values:
        largest = max(largest, sum(abs(value - center) <= tolerance for value in values))
    return largest / len(values)


def _segment_reliability(
    offsets: list[float],
    *,
    max_offset: float,
    jump_threshold: float,
) -> dict[str, object]:
    """Reject FFT maxima that look like search-boundary noise.

    Real editing discontinuities form a few stable plateaus or a smooth drift.
    Random alternation across nearly the entire ±max_offset range is a hallmark of
    ambiguous speech correlation and must never drive engine selection or repair.
    """

    if not offsets:
        return {
            "reliable": False,
            "quality_reason": "no_offsets",
            "edge_hit_count": 0,
            "edge_hit_ratio": 0.0,
            "large_jump_ratio": 0.0,
            "dominant_cluster_fraction": 0.0,
            "roughness_seconds": None,
        }
    limit = max(0.1, abs(max_offset))
    edge_margin = max(0.25, limit * 0.02)
    edge_hits = [abs(abs(value) - limit) <= edge_margin for value in offsets]
    edge_hit_count = sum(edge_hits)
    edge_hit_ratio = edge_hit_count / len(offsets)
    usable = [value for value, edge in zip(offsets, edge_hits) if not edge]
    ordered = usable if len(usable) >= 2 else offsets
    differences = [abs(right - left) for left, right in zip(ordered, ordered[1:])]
    roughness = statistics.median(differences) if differences else 0.0
    large_jump_cutoff = max(5.0, jump_threshold * 2.0, limit * 0.12)
    large_jump_ratio = (
        sum(delta >= large_jump_cutoff for delta in differences) / len(differences)
        if differences
        else 0.0
    )
    spread = max(offsets) - min(offsets) if len(offsets) >= 2 else 0.0
    dominant_cluster = _largest_offset_cluster_fraction(
        usable or offsets,
        tolerance=max(1.5, jump_threshold),
    )
    spans_full_search = spread >= limit * 1.75

    reasons: list[str] = []
    if edge_hit_ratio > 0.20:
        reasons.append("too_many_boundary_hits")
    if spans_full_search and large_jump_ratio > 0.35:
        reasons.append("full_range_oscillation")
    if large_jump_ratio > 0.55:
        reasons.append("too_many_large_jumps")
    if len(offsets) >= 5 and dominant_cluster < 0.30 and roughness > max(4.0, limit * 0.15):
        reasons.append("no_stable_offset_cluster")
    reliable = not reasons
    return {
        "reliable": reliable,
        "quality_reason": "ok" if reliable else ",".join(reasons),
        "edge_hit_count": edge_hit_count,
        "edge_hit_ratio": round(edge_hit_ratio, 4),
        "large_jump_ratio": round(large_jump_ratio, 4),
        "dominant_cluster_fraction": round(dominant_cluster, 4),
        "roughness_seconds": round(roughness, 4),
        "spans_full_search": spans_full_search,
    }


def evaluate_segment_alignment(
    video: Path,
    subtitle: Path,
    cache_dir: Path,
    config: SyncConfig,
    *,
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
    force: bool = False,
    verbose: bool = False,
) -> dict[str, object]:
    """Measure residual offset in several independent regions of the episode."""
    if not config.segment_validation:
        return _result("disabled", available=False)

    plain, conversion = convert_to_plain_srt(
        subtitle,
        cache_dir,
        ffmpeg_path=ffmpeg_path,
        force=force,
        verbose=verbose,
    )
    if plain.suffix.casefold() != ".srt":
        return _result(
            "segment_subtitle_conversion_failed",
            available=False,
            conversion_reason=conversion.get("reason"),
        )
    try:
        cues = parse_srt(plain)
    except OSError as exc:
        return _result("segment_subtitle_read_error", available=False, error=str(exc))
    if len(cues) < 6:
        return _result("too_few_cues", available=False, cue_count=len(cues))

    duration = _probe_duration(video, ffprobe_path)
    if duration is None:
        return _result("duration_probe_failed", available=False)
    windows = _segment_windows(duration, config.segment_count, config.segment_window_seconds)

    stat = plain.stat()
    base_digest = hashlib.sha1(
        (
            f"{plain.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:"
            f"{duration:.3f}:{config.segment_count}:{config.segment_window_seconds}:"
            f"{config.segment_max_offset_seconds}:local-v3"
        ).encode()
    ).hexdigest()[:20]
    segment_dir = cache_dir / "segment-subs" / base_digest
    segment_dir.mkdir(parents=True, exist_ok=True)

    local_padding = max(0.0, config.segment_max_offset_seconds)
    evaluation_config = replace(
        config,
        engine="ffsubsync",
        compare_engines=False,
        fix_framerate=False,
        gss=False,
        skip_on_low_quality=False,
        max_offset_seconds=max(5.0, local_padding),
        quality_max_offset_seconds=max(5.0, local_padding),
        segment_validation=False,
        piecewise_repair=False,
    )
    segments: list[dict[str, object]] = []
    for label, start, length in windows:
        clipped = _clip_cues_for_local_alignment(
            cues,
            start,
            length,
            local_padding,
        )
        item: dict[str, object] = {
            "label": label,
            "start_seconds": round(start, 3),
            "center_seconds": round(start + length / 2, 3),
            "duration_seconds": round(length, 3),
            "cue_count": len(clipped),
        }
        minimum_cues = 1 if label in {"start", "end"} else 2
        if len(clipped) < minimum_cues:
            item.update({"successful": False, "reason": "too_few_cues"})
            segments.append(item)
            continue
        segment_srt = segment_dir / f"{label}.srt"
        if force or not segment_srt.exists():
            write_srt(clipped, segment_srt)
        audio, audio_error = _extract_audio_segment(
            video,
            start,
            length,
            cache_dir,
            ffmpeg_path=ffmpeg_path,
            force=force,
            padding_seconds=local_padding,
        )
        if audio is None:
            item.update({"successful": False, "reason": "audio_extract_failed", "error": audio_error})
            segments.append(item)
            continue
        _, result = synchronize_subtitle(
            audio,
            segment_srt,
            cache_dir,
            evaluation_config,
            ffmpeg_path=ffmpeg_path,
            force=force,
            verbose=verbose,
            quiet=True,
            tag=f"segment-eval-{label}",
        )
        successful = bool(result.get("sync_was_successful"))
        offset_value: float | None = None
        try:
            raw_offset = result.get("offset_seconds")
            offset_value = float(raw_offset) if raw_offset is not None else None
        except (TypeError, ValueError):
            offset_value = None
        boundary_hit = bool(
            successful
            and offset_value is not None
            and abs(abs(offset_value) - max(0.1, local_padding))
            <= max(0.25, max(0.1, local_padding) * 0.02)
        )
        item.update(
            {
                "successful": successful,
                "reason": result.get("reason"),
                "offset_seconds": offset_value,
                "alignment_score": result.get("alignment_score"),
                "boundary_hit": boundary_hit,
            }
        )
        segments.append(item)

    offsets: list[float] = []
    for item in segments:
        if not item.get("successful"):
            continue
        try:
            offsets.append(float(item["offset_seconds"]))
        except (KeyError, TypeError, ValueError):
            continue
    successful_count = len(offsets)
    boundary_labels = {"start", "end", "full"}
    boundary_successful = sum(
        1
        for item in segments
        if item.get("label") in boundary_labels and item.get("successful")
    )
    # Dense short-window scans contain many silent/low-dialogue windows. Requiring
    # 60% success made large segment counts unusable; a handful of trustworthy
    # anchors is sufficient for piecewise interpolation.
    minimum_successful = max(2, min(len(windows), min(8, (len(windows) + 4) // 5)))
    available = successful_count >= minimum_successful
    max_abs = max((abs(value) for value in offsets), default=float("inf"))
    mean_abs = sum(abs(value) for value in offsets) / successful_count if offsets else float("inf")
    spread = max(offsets) - min(offsets) if len(offsets) >= 2 else 0.0
    reliability = _segment_reliability(
        offsets,
        max_offset=local_padding,
        jump_threshold=config.piecewise_jump_threshold_seconds,
    )
    return _result(
        (
            "evaluated"
            if available and reliability.get("reliable")
            else "unreliable_segments"
            if available
            else "insufficient_segments"
        ),
        available=available,
        subtitle=str(plain),
        attempted_segments=len(windows),
        successful_segments=successful_count,
        boundary_successful_segments=boundary_successful,
        successful_ratio=round(successful_count / len(windows), 4) if windows else 0.0,
        max_abs_offset_seconds=round(max_abs, 4) if offsets else None,
        mean_abs_offset_seconds=round(mean_abs, 4) if offsets else None,
        offset_spread_seconds=round(spread, 4) if offsets else None,
        **{
            **reliability,
            "reliable": bool(available and reliability.get("reliable")),
        },
        segments=segments,
    )


def _diagnostic_rank(
    diagnostics: dict[str, object] | None,
) -> tuple[int, int, float, float, float, float, int]:
    if not diagnostics or not diagnostics.get("available"):
        return (0, 0, float("-inf"), float("-inf"), float("-inf"), 0.0, 0)
    # Backward compatibility for tests/old cached metadata: if reliability is
    # absent, treat an otherwise available diagnostic as trusted. New results
    # always write this field explicitly.
    if diagnostics.get("reliable", True) is False:
        return (0, 0, float("-inf"), float("-inf"), float("-inf"), 0.0, 0)
    try:
        successful = int(diagnostics.get("successful_segments", 0))
        boundary_successful = int(diagnostics.get("boundary_successful_segments", 0))
        success_ratio = float(diagnostics.get("successful_ratio", 0.0))
        max_abs = float(diagnostics.get("max_abs_offset_seconds"))
        mean_abs = float(diagnostics.get("mean_abs_offset_seconds"))
        spread = float(diagnostics.get("offset_spread_seconds"))
    except (TypeError, ValueError):
        return (0, 0, float("-inf"), float("-inf"), float("-inf"), 0.0, 0)
    # Correctly covering the opening and ending is more important than a tiny
    # score improvement in the already-correct middle of the episode.
    return (1, boundary_successful, -max_abs, -mean_abs, -spread, success_ratio, successful)


def _piecewise_offset(anchors: list[tuple[float, float]], timestamp: float, jump_threshold: float) -> float:
    if timestamp <= anchors[0][0]:
        return anchors[0][1]
    if timestamp >= anchors[-1][0]:
        return anchors[-1][1]
    for (left_time, left_offset), (right_time, right_offset) in zip(anchors, anchors[1:]):
        if not left_time <= timestamp <= right_time:
            continue
        if abs(right_offset - left_offset) >= jump_threshold:
            return left_offset if timestamp < (left_time + right_time) / 2 else right_offset
        ratio = (timestamp - left_time) / max(0.001, right_time - left_time)
        return left_offset + ratio * (right_offset - left_offset)
    return anchors[-1][1]


def _smooth_anchors(anchors: list[tuple[float, float]], radius: int = 2) -> list[tuple[float, float]]:
    """Median-smooth dense local estimates while retaining genuine timing jumps."""
    if len(anchors) < 3 or radius <= 0:
        return anchors
    smoothed: list[tuple[float, float]] = []
    for index, (timestamp, _) in enumerate(anchors):
        left = max(0, index - radius)
        right = min(len(anchors), index + radius + 1)
        values = sorted(offset for _, offset in anchors[left:right])
        middle = len(values) // 2
        median = values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2
        smoothed.append((timestamp, median))
    return smoothed


def _piecewise_correction_for_source_time(
    anchors: list[tuple[float, float]],
    source_timestamp: float,
    jump_threshold: float,
) -> float:
    """Solve target_time = source_time + correction(target_time) approximately."""
    target_guess = source_timestamp
    correction = 0.0
    for _ in range(4):
        correction = _piecewise_offset(anchors, target_guess, jump_threshold)
        target_guess = source_timestamp + correction
    return correction


def _retime_cues_without_reordering(
    cues: list[tuple[float, float, str]],
    corrections: list[float],
    *,
    minimum_start_seconds: float = 0.100,
    minimum_duration_seconds: float = 0.300,
) -> tuple[list[tuple[float, float, str]] | None, dict[str, object]]:
    """Apply per-cue corrections only when dialogue order stays intact.

    Piecewise interpolation can jump across an edit and move an earlier line
    behind a later one. Sorting the resulting timestamps hides the problem by
    silently shuffling dialogue. Reject that entire local repair instead.
    """
    if len(cues) != len(corrections):
        return None, {"reason": "correction_count_mismatch"}

    repaired: list[tuple[float, float, str]] = []
    previous_start = -1.0
    previous_midpoint = -1.0
    max_adjacent_correction_jump = 0.0
    previous_correction: float | None = None

    for index, ((start, end, text), correction) in enumerate(zip(cues, corrections)):
        duration = max(float(end) - float(start), minimum_duration_seconds)
        corrected_start = float(start) + float(correction)
        corrected_end = float(end) + float(correction)
        if corrected_start < minimum_start_seconds:
            corrected_start = minimum_start_seconds
            corrected_end = corrected_start + duration
        else:
            corrected_end = max(corrected_end, corrected_start + minimum_duration_seconds)

        midpoint = (corrected_start + corrected_end) / 2.0
        if index and (corrected_start < previous_start - 1e-6 or midpoint < previous_midpoint - 1e-6):
            return None, {
                "reason": "cue_order_inversion",
                "cue_index": index + 1,
                "previous_start": round(previous_start, 3),
                "corrected_start": round(corrected_start, 3),
                "previous_midpoint": round(previous_midpoint, 3),
                "corrected_midpoint": round(midpoint, 3),
            }

        if corrected_end - corrected_start < minimum_duration_seconds - 1e-6:
            return None, {
                "reason": "cue_too_short",
                "cue_index": index + 1,
                "duration_seconds": round(corrected_end - corrected_start, 4),
            }

        if previous_correction is not None:
            max_adjacent_correction_jump = max(
                max_adjacent_correction_jump,
                abs(float(correction) - previous_correction),
            )
        previous_correction = float(correction)
        previous_start = corrected_start
        previous_midpoint = midpoint
        repaired.append((corrected_start, corrected_end, text))

    return repaired, {
        "reason": "safe",
        "cue_count": len(repaired),
        "max_adjacent_correction_jump_seconds": round(max_adjacent_correction_jump, 3),
    }


def apply_piecewise_repair(
    subtitle: Path,
    diagnostics: dict[str, object],
    cache_dir: Path,
    config: SyncConfig,
    *,
    ffmpeg_path: str = "ffmpeg",
    force: bool = False,
    verbose: bool = False,
) -> tuple[Path, dict[str, object]]:
    """Apply different residual offsets across the episode and emit plain SRT."""
    if not config.piecewise_repair or not diagnostics.get("available"):
        return subtitle, _result("piecewise_disabled", applied=False)
    if diagnostics.get("reliable", True) is False:
        return subtitle, _result(
            "piecewise_unreliable_diagnostics",
            applied=False,
            quality_reason=diagnostics.get("quality_reason"),
        )
    raw_segments = diagnostics.get("segments")
    if not isinstance(raw_segments, list):
        return subtitle, _result("piecewise_no_segments", applied=False)
    anchors: list[tuple[float, float]] = []
    for item in raw_segments:
        if not isinstance(item, dict) or not item.get("successful"):
            continue
        try:
            center = float(item["center_seconds"])
            offset = float(item["offset_seconds"])
        except (KeyError, TypeError, ValueError):
            continue
        boundary_hit = bool(item.get("boundary_hit"))
        if not boundary_hit and abs(offset) <= config.piecewise_max_correction_seconds:
            anchors.append((center, offset))
    anchors.sort()
    anchors = _smooth_anchors(anchors)
    if len(anchors) < 2:
        return subtitle, _result("piecewise_too_few_anchors", applied=False)
    if max(abs(offset) for _, offset in anchors) < config.piecewise_min_offset_seconds:
        return subtitle, _result("piecewise_not_needed", applied=False, anchors=anchors)

    plain, conversion = convert_to_plain_srt(
        subtitle,
        cache_dir,
        ffmpeg_path=ffmpeg_path,
        force=force,
        verbose=verbose,
    )
    if plain.suffix.casefold() != ".srt":
        return subtitle, _result(
            "piecewise_conversion_failed",
            applied=False,
            conversion_reason=conversion.get("reason"),
        )
    cues = parse_srt(plain)
    if not cues:
        return subtitle, _result("piecewise_no_cues", applied=False)

    stat = plain.stat()
    digest = hashlib.sha1(
        (
            f"{plain.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{anchors}:"
            f"{config.piecewise_jump_threshold_seconds}:piecewise-v6"
        ).encode()
    ).hexdigest()[:20]
    output_dir = cache_dir / "piecewise"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{digest}.srt"
    if force:
        output.unlink(missing_ok=True)
    if output.exists() and output.stat().st_size > 0:
        return output, _result("cached", applied=True, anchors=anchors, output=str(output))

    corrections = [
        _piecewise_correction_for_source_time(
            anchors,
            (start + end) / 2.0,
            config.piecewise_jump_threshold_seconds,
        )
        for start, end, _text in cues
    ]
    repaired, sequence_safety = _retime_cues_without_reordering(cues, corrections)
    if repaired is None:
        return subtitle, _result(
            "piecewise_unsafe_sequence",
            applied=False,
            anchors=anchors,
            sequence_safety=sequence_safety,
        )
    write_srt(repaired, output, preserve_order=True)
    return output, _result(
        "applied",
        applied=True,
        anchors=anchors,
        output=str(output),
        sequence_safety=sequence_safety,
    )


def _maybe_repair_piecewise(
    video: Path,
    path: Path,
    result: dict[str, object],
    cache_dir: Path,
    config: SyncConfig,
    *,
    ffmpeg_path: str,
    ffprobe_path: str,
    force: bool,
    verbose: bool,
    source_path: Path | None = None,
) -> tuple[Path, dict[str, object]]:
    """Try local repair from the original subtitle, not a globally shifted copy.

    A large negative global offset can make opening cues negative. ffsubsync then
    omits them from its output, so no later operation can recover them. Local
    diagnostics and piecewise correction must therefore use the untouched source.
    """
    baseline_diagnostics = result.get("segment_diagnostics")
    if not isinstance(baseline_diagnostics, dict):
        baseline_diagnostics = evaluate_segment_alignment(
            video,
            path,
            cache_dir,
            config,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            force=force,
            verbose=verbose,
        )
        result["segment_diagnostics"] = baseline_diagnostics

    repair_source = source_path or path
    if repair_source != path:
        source_diagnostics = evaluate_segment_alignment(
            video,
            repair_source,
            cache_dir,
            config,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            force=force,
            verbose=verbose,
        )
    else:
        source_diagnostics = baseline_diagnostics

    if baseline_diagnostics.get("reliable", True) is False:
        result["piecewise_repair_skipped"] = {
            "reason": "baseline_diagnostics_unreliable",
            "quality_reason": baseline_diagnostics.get("quality_reason"),
        }
        return path, result
    if source_diagnostics.get("reliable", True) is False:
        result["piecewise_repair_skipped"] = {
            "reason": "source_diagnostics_unreliable",
            "quality_reason": source_diagnostics.get("quality_reason"),
        }
        return path, result

    repaired, repair_result = apply_piecewise_repair(
        repair_source,
        source_diagnostics,
        cache_dir,
        config,
        ffmpeg_path=ffmpeg_path,
        force=force,
        verbose=verbose,
    )
    if not repair_result.get("applied") or repaired == repair_source:
        return path, result

    repaired_diagnostics = evaluate_segment_alignment(
        video,
        repaired,
        cache_dir,
        config,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        force=force,
        verbose=verbose,
    )
    if repaired_diagnostics.get("reliable", True) is False:
        result["piecewise_repair_skipped"] = {
            "reason": "repaired_diagnostics_unreliable",
            "quality_reason": repaired_diagnostics.get("quality_reason"),
        }
        return path, result
    if _diagnostic_rank(repaired_diagnostics) > _diagnostic_rank(baseline_diagnostics):
        updated = dict(result)
        updated["reason"] = "applied"
        suffix = "piecewise-source" if repair_source != path else "piecewise"
        updated["engine"] = f"{result.get('engine', 'unknown')}+{suffix}"
        updated["piecewise_repair"] = repair_result
        updated["segment_diagnostics_before"] = baseline_diagnostics
        updated["segment_source_diagnostics"] = source_diagnostics
        updated["segment_diagnostics"] = repaired_diagnostics
        updated["output"] = str(repaired)
        return repaired, updated
    return path, result


def _merge_intervals(
    cues: list[tuple[float, float, str]],
    *,
    padding_seconds: float = 0.45,
) -> list[tuple[float, float]]:
    """Convert subtitle cues to merged activity intervals.

    A small padding makes the score tolerant to different line splitting and
    reading speeds between Japanese and English subtitles.
    """
    intervals = sorted(
        (max(0.0, start - padding_seconds), end + padding_seconds)
        for start, end, _ in cues
        if end > start
    )
    merged: list[tuple[float, float]] = []
    for start, end in intervals:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _interval_length(intervals: list[tuple[float, float]], start: float, end: float) -> float:
    total = 0.0
    for left, right in intervals:
        if right <= start:
            continue
        if left >= end:
            break
        total += max(0.0, min(right, end) - max(left, start))
    return total


def _interval_intersection_length(
    first: list[tuple[float, float]],
    second: list[tuple[float, float]],
    start: float,
    end: float,
) -> float:
    i = j = 0
    total = 0.0
    while i < len(first) and j < len(second):
        a0, a1 = first[i]
        b0, b1 = second[j]
        if a1 <= start:
            i += 1
            continue
        if b1 <= start:
            j += 1
            continue
        if a0 >= end or b0 >= end:
            break
        left = max(a0, b0, start)
        right = min(a1, b1, end)
        if right > left:
            total += right - left
        if a1 <= b1:
            i += 1
        else:
            j += 1
    return total


def compare_timing_activity(
    candidate: Path,
    reference: Path,
    *,
    priority_seconds: float = 180.0,
) -> dict[str, object]:
    """Compare only subtitle timing activity, independent of language/text.

    This is intentionally used only after the LLM has confirmed that both files
    describe the same episode. It measures whether dialogue appears at similar
    moments and gives the cold open its own score so a long correct middle cannot
    hide a broken beginning.
    """
    try:
        candidate_cues = parse_srt(candidate)
        reference_cues = parse_srt(reference)
    except OSError as exc:
        return {"available": False, "reason": "read_error", "error": str(exc)}
    if not candidate_cues or not reference_cues:
        return {"available": False, "reason": "empty_subtitles"}

    candidate_intervals = _merge_intervals(candidate_cues)
    reference_intervals = _merge_intervals(reference_cues)
    duration = max(
        max(end for _, end, _ in candidate_cues),
        max(end for _, end, _ in reference_cues),
    )
    if duration <= 0:
        return {"available": False, "reason": "invalid_duration"}

    edge = min(max(60.0, priority_seconds), max(60.0, duration / 3.0))
    regions = {
        "start": (0.0, min(edge, duration)),
        "middle": (min(edge, duration), max(min(edge, duration), duration - edge)),
        "end": (max(0.0, duration - edge), duration),
        "full": (0.0, duration),
    }

    scores: dict[str, float] = {}
    for name, (region_start, region_end) in regions.items():
        if region_end <= region_start + 0.001:
            scores[name] = 0.0
            continue
        candidate_length = _interval_length(candidate_intervals, region_start, region_end)
        reference_length = _interval_length(reference_intervals, region_start, region_end)
        intersection = _interval_intersection_length(
            candidate_intervals,
            reference_intervals,
            region_start,
            region_end,
        )
        denominator = candidate_length + reference_length
        scores[name] = 1.0 if denominator <= 0 else (2.0 * intersection / denominator)

    # Cold open matters more than the ending here; middle remains a guardrail.
    weighted = (
        0.45 * scores["start"]
        + 0.40 * scores["middle"]
        + 0.10 * scores["end"]
        + 0.05 * scores["full"]
    )
    return {
        "available": True,
        "reason": "ok",
        "duration_seconds": round(duration, 3),
        "priority_seconds": round(edge, 3),
        "start": round(scores["start"], 4),
        "middle": round(scores["middle"], 4),
        "end": round(scores["end"], 4),
        "full": round(scores["full"], 4),
        "weighted": round(weighted, 4),
    }


def _windowed_reference_shift(
    candidate_cues: list[tuple[float, float, str]],
    reference_cues: list[tuple[float, float, str]],
    *,
    region_start: float,
    region_end: float,
    max_shift_seconds: float,
) -> dict[str, object]:
    """Estimate a small residual offset inside one subtitle-reference window.

    The two tracks may use different languages and cue splitting, so text is not
    compared here. We combine dialogue-onset matches with subtitle activity
    correlation. The caller only uses this after semantic verification has
    confirmed that both tracks belong to the same episode.
    """
    limit = max(1.0, abs(float(max_shift_seconds)))
    local_reference = [
        (start - region_start, end - region_start, text)
        for start, end, text in reference_cues
        if end >= region_start and start <= region_end
    ]
    source_pool = [
        (start, end, text)
        for start, end, text in candidate_cues
        if end >= region_start - limit and start <= region_end + limit
    ]
    if len(local_reference) < 4 or len(source_pool) < 4:
        return {"available": False, "reason": "not_enough_cues"}

    source_starts = _cue_onsets(source_pool)
    reference_starts = _cue_onsets(
        [(start + region_start, end + region_start, text) for start, end, text in local_reference]
    )
    if len(source_starts) < 4 or len(reference_starts) < 4:
        return {"available": False, "reason": "not_enough_onsets"}

    hypotheses = _onset_candidate_shifts(
        source_starts,
        reference_starts,
        limit=limit,
        bin_seconds=0.20,
        maximum_candidates=36,
    )
    hypotheses.extend([0.0, -limit, limit])

    def evaluate(shift: float) -> tuple[float, dict[str, object]]:
        shifted_local: list[tuple[float, float, str]] = []
        for start, end, text in source_pool:
            shifted_start = start + shift
            shifted_end = end + shift
            if shifted_end < region_start or shifted_start > region_end:
                continue
            shifted_local.append(
                (
                    max(region_start, shifted_start) - region_start,
                    min(region_end, shifted_end) - region_start,
                    text,
                )
            )
        if len(shifted_local) < 4:
            return float("-inf"), {"available": False, "reason": "not_enough_shifted_cues"}

        shifted_starts = _cue_onsets(shifted_local)
        local_reference_starts = _cue_onsets(local_reference)
        pairs = _matched_onset_pairs(
            shifted_starts,
            local_reference_starts,
            tolerance=0.65,
        )
        denominator = max(1, min(len(shifted_starts), len(local_reference_starts)))
        coverage = len(pairs) / denominator
        overlap = _activity_overlap_for_shift(shifted_local, local_reference, 0.0)
        correlation = _activity_correlation_for_shift(shifted_local, local_reference, 0.0)
        # Dense, nearly periodic dialogue can align perfectly one cue earlier or
        # later. A small edge penalty prefers the shift that also preserves the
        # first and last activity landmarks of this window.
        first_edge_error = abs(shifted_starts[0] - local_reference_starts[0])
        last_edge_error = abs(shifted_starts[-1] - local_reference_starts[-1])
        edge_penalty = 0.08 * (
            min(6.0, first_edge_error) + min(6.0, last_edge_error)
        )
        score = (
            2.25 * correlation
            + 1.05 * overlap
            + 1.20 * coverage
            - 0.012 * abs(shift)
            - edge_penalty
        )
        return score, {
            "available": True,
            "shift_seconds": round(float(shift), 3),
            "score": round(score, 5),
            "matched_onsets": len(pairs),
            "coverage": round(coverage, 4),
            "activity_overlap": round(overlap, 4),
            "activity_correlation": round(correlation, 4),
            "source_onsets": len(shifted_starts),
            "reference_onsets": len(local_reference_starts),
            "first_edge_error": round(first_edge_error, 3),
            "last_edge_error": round(last_edge_error, 3),
        }

    evaluated: list[tuple[float, dict[str, object]]] = []
    seen: set[int] = set()
    for hypothesis in hypotheses:
        for step in range(-8, 9):
            shift = max(-limit, min(limit, float(hypothesis) + step * 0.05))
            key = int(round(shift * 1000))
            if key in seen:
                continue
            seen.add(key)
            evaluated.append(evaluate(shift))
    if not evaluated:
        return {"available": False, "reason": "no_hypotheses"}

    best_score, best = max(evaluated, key=lambda item: item[0])
    _, baseline = evaluate(0.0)
    if not best.get("available") or not baseline.get("available"):
        return {"available": False, "reason": "metrics_unavailable"}

    matched = int(best.get("matched_onsets") or 0)
    minimum_matches = max(
        4,
        int(round(min(int(best.get("source_onsets") or 0), int(best.get("reference_onsets") or 0)) * 0.07)),
    )
    improvement = float(best_score) - float(baseline.get("score") or 0.0)
    confident = (
        matched >= minimum_matches
        and float(best.get("coverage") or 0.0) >= 0.10
        and (
            float(best.get("activity_correlation") or 0.0) >= 0.015
            or float(best.get("activity_overlap") or 0.0) >= 0.42
        )
        and (abs(float(best.get("shift_seconds") or 0.0)) <= 0.35 or improvement >= 0.055)
    )
    return {
        **best,
        "available": True,
        "confident": confident,
        "minimum_matches": minimum_matches,
        "score_improvement": round(improvement, 5),
        "baseline": baseline,
        "region_start": round(region_start, 3),
        "region_end": round(region_end, 3),
    }


def _stable_offset_cluster(
    values: list[float],
    *,
    tolerance: float = 0.45,
) -> dict[str, object] | None:
    """Return the strongest compact offset cluster, ignoring isolated aliases."""
    if len(values) < 2:
        return None
    tolerance = max(0.10, float(tolerance))
    best: list[float] = []
    for center in values:
        group = [value for value in values if abs(value - center) <= tolerance]
        if len(group) > len(best):
            best = group
        elif len(group) == len(best) and group:
            current_spread = max(group) - min(group)
            best_spread = max(best) - min(best) if best else float("inf")
            if current_spread < best_spread:
                best = group
    if len(best) < 2:
        return None
    median = float(statistics.median(best))
    inliers = [value for value in values if abs(value - median) <= tolerance]
    if len(inliers) < 2:
        return None
    return {
        "offset_seconds": round(float(statistics.median(inliers)), 4),
        "support": len(inliers),
        "total": len(values),
        "support_ratio": round(len(inliers) / len(values), 4),
        "spread_seconds": round(max(inliers) - min(inliers), 4),
        "values": [round(value, 4) for value in inliers],
    }


def _stable_two_plateau_offsets(
    before_values: list[float],
    after_values: list[float],
    *,
    minimum_delta_seconds: float = 0.55,
    maximum_delta_seconds: float = 3.0,
) -> dict[str, object] | None:
    """Recognize two nearby but genuinely distinct timing plateaus.

    This intentionally targets small edit differences that the ordinary
    large-jump detector ignores. Both sides must independently form a compact
    cluster, so two noisy ffsubsync maxima are not enough.
    """
    before = _stable_offset_cluster(before_values)
    after = _stable_offset_cluster(after_values)
    if before is None or after is None:
        return None
    if float(before["support_ratio"]) < 0.60 or float(after["support_ratio"]) < 0.60:
        return None

    left = float(before["offset_seconds"])
    right = float(after["offset_seconds"])
    delta = abs(right - left)
    if not minimum_delta_seconds <= delta <= maximum_delta_seconds:
        return None
    return {
        "before": before,
        "after": after,
        "before_offset_seconds": left,
        "after_offset_seconds": right,
        "delta_seconds": round(delta, 4),
    }


def _repair_stable_opening_plateaus(
    aligned: Path,
    reference: Path,
    candidate_cues: list[tuple[float, float, str]],
    reference_cues: list[tuple[float, float, str]],
    cache_dir: Path,
    config: SyncConfig,
    *,
    force: bool = False,
    verbose: bool = False,
) -> tuple[Path, dict[str, object]]:
    """Repair a small stable clock change across an opening/title-card gap.

    Some releases use one constant offset before the OP and another, only about
    a second away, afterwards. The older piecewise logic looked specifically for
    large jumps, so it interpolated through this edit. Here each side must be
    supported by several independent subtitle-activity probes and the final
    candidate still has to improve the exact embedded-reference activity score.
    """
    if len(candidate_cues) < 12 or len(reference_cues) < 12:
        return aligned, _result("opening_plateau_too_few_cues", applied=False)

    duration = max(
        max(end for _start, end, _text in candidate_cues),
        max(end for _start, end, _text in reference_cues),
    )
    if duration < 300.0:
        return aligned, _result("opening_plateau_too_short", applied=False)

    gap_candidates = [
        (previous_end, next_start, next_start - previous_end)
        for (_previous_start, previous_end, _previous_text),
            (next_start, _next_end, _next_text)
        in zip(candidate_cues, candidate_cues[1:])
        if next_start - previous_end >= 20.0
        and previous_end >= 20.0
        and next_start <= min(duration * 0.45, 10 * 60.0)
    ]
    if not gap_candidates:
        return aligned, _result("opening_plateau_gap_not_found", applied=False)

    max_shift = min(
        12.0,
        max(4.0, float(config.piecewise_max_correction_seconds)),
    )
    evaluated: list[dict[str, object]] = []

    for gap_start, gap_end, gap_seconds in sorted(
        gap_candidates,
        key=lambda item: item[2],
        reverse=True,
    )[:4]:
        pre_regions = [
            (max(0.0, gap_start - 75.0), gap_start),
            (max(0.0, gap_start - 120.0), gap_start),
            (max(0.0, gap_start - 180.0), max(0.0, gap_start - 20.0)),
        ]
        post_regions = [
            (gap_end, min(duration, gap_end + 75.0)),
            (gap_end, min(duration, gap_end + 120.0)),
            (min(duration, gap_end + 20.0), min(duration, gap_end + 180.0)),
        ]

        def probe(regions: list[tuple[float, float]]) -> tuple[list[float], list[dict[str, object]]]:
            values: list[float] = []
            details: list[dict[str, object]] = []
            seen: set[tuple[int, int]] = set()
            for region_start, region_end in regions:
                if region_end - region_start < 25.0:
                    continue
                key = (int(round(region_start * 10)), int(round(region_end * 10)))
                if key in seen:
                    continue
                seen.add(key)
                result = _windowed_reference_shift(
                    candidate_cues,
                    reference_cues,
                    region_start=region_start,
                    region_end=region_end,
                    max_shift_seconds=max_shift,
                )
                details.append(result)
                if result.get("confident"):
                    values.append(float(result.get("shift_seconds") or 0.0))
            return values, details

        before_values, before_probes = probe(pre_regions)
        after_values, after_probes = probe(post_regions)
        plateau = _stable_two_plateau_offsets(before_values, after_values)
        item: dict[str, object] = {
            "gap_start": round(gap_start, 3),
            "gap_end": round(gap_end, 3),
            "gap_seconds": round(gap_seconds, 3),
            "before_values": [round(value, 4) for value in before_values],
            "after_values": [round(value, 4) for value in after_values],
            "before_probes": before_probes,
            "after_probes": after_probes,
            "plateau": plateau,
        }
        evaluated.append(item)
        if plateau is None:
            continue

    valid = [item for item in evaluated if isinstance(item.get("plateau"), dict)]
    if not valid:
        return aligned, _result(
            "opening_plateau_not_stable",
            applied=False,
            candidates=evaluated,
        )

    def plan_rank(item: dict[str, object]) -> tuple[int, float, float]:
        plateau = item["plateau"]
        assert isinstance(plateau, dict)
        before = plateau["before"]
        after = plateau["after"]
        assert isinstance(before, dict) and isinstance(after, dict)
        support = int(before.get("support") or 0) + int(after.get("support") or 0)
        return (
            support,
            float(item.get("gap_seconds") or 0.0),
            -float(plateau.get("delta_seconds") or 0.0),
        )

    best = max(valid, key=plan_rank)
    plateau = best["plateau"]
    assert isinstance(plateau, dict)
    before_offset = float(plateau["before_offset_seconds"])
    after_offset = float(plateau["after_offset_seconds"])
    boundary = float(best["gap_end"])

    corrections = [
        before_offset if (start + end) / 2.0 < boundary else after_offset
        for start, end, _text in candidate_cues
    ]
    repaired, sequence_safety = _retime_cues_without_reordering(
        candidate_cues,
        corrections,
    )
    if repaired is None:
        return aligned, _result(
            "opening_plateau_unsafe_sequence",
            applied=False,
            plan=best,
            sequence_safety=sequence_safety,
            candidates=evaluated,
        )

    aligned_stat = aligned.stat()
    reference_stat = reference.stat()
    digest = hashlib.sha1(
        (
            f"reference-opening-plateau-v1:{aligned.resolve()}:{aligned_stat.st_size}:"
            f"{aligned_stat.st_mtime_ns}:{reference.resolve()}:{reference_stat.st_size}:"
            f"{reference_stat.st_mtime_ns}:{boundary:.3f}:{before_offset:.4f}:"
            f"{after_offset:.4f}"
        ).encode()
    ).hexdigest()[:20]
    output_dir = cache_dir / "reference-opening-plateau"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{digest}.srt"
    if force:
        output.unlink(missing_ok=True)
    if not output.exists() or output.stat().st_size <= 0:
        write_srt(repaired, output, preserve_order=True)

    before_activity = compare_timing_activity(aligned, reference)
    after_activity = compare_timing_activity(output, reference)
    priority_seconds = min(duration, max(120.0, boundary + 60.0))
    before_opening = compare_timing_activity(
        aligned,
        reference,
        priority_seconds=priority_seconds,
    )
    after_opening = compare_timing_activity(
        output,
        reference,
        priority_seconds=priority_seconds,
    )
    if not all(
        payload.get("available")
        for payload in (before_activity, after_activity, before_opening, after_opening)
    ):
        output.unlink(missing_ok=True)
        return aligned, _result(
            "opening_plateau_metrics_unavailable",
            applied=False,
            plan=best,
            candidates=evaluated,
        )

    weighted_gain = float(after_activity.get("weighted") or 0.0) - float(
        before_activity.get("weighted") or 0.0
    )
    start_gain = float(after_opening.get("start") or 0.0) - float(
        before_opening.get("start") or 0.0
    )
    middle_loss = float(before_activity.get("middle") or 0.0) - float(
        after_activity.get("middle") or 0.0
    )
    accepted = (
        middle_loss <= 0.020
        and (
            weighted_gain >= 0.004
            or (start_gain >= 0.015 and weighted_gain >= -0.001)
        )
    )
    if verbose:
        print(
            "  Stable opening plateaus: "
            f"before={before_offset:+.2f}s, after={after_offset:+.2f}s, "
            f"boundary={boundary:.2f}s, delta={float(plateau['delta_seconds']):.2f}s, "
            f"weighted_gain={weighted_gain:+.4f}, start_gain={start_gain:+.4f}, "
            f"middle_loss={middle_loss:+.4f}, accepted={accepted}"
        )
    if not accepted:
        output.unlink(missing_ok=True)
        return aligned, _result(
            "opening_plateau_no_improvement",
            applied=False,
            plan=best,
            candidates=evaluated,
            before=before_activity,
            after=after_activity,
            before_opening=before_opening,
            after_opening=after_opening,
            weighted_gain=round(weighted_gain, 4),
            start_gain=round(start_gain, 4),
            middle_loss=round(middle_loss, 4),
        )

    return output, _result(
        "applied",
        applied=True,
        strategy="stable_opening_plateaus",
        output=str(output),
        boundary_seconds=round(boundary, 3),
        before_offset_seconds=round(before_offset, 4),
        after_offset_seconds=round(after_offset, 4),
        delta_seconds=round(float(plateau["delta_seconds"]), 4),
        plan=best,
        candidates=evaluated,
        before=before_activity,
        after=after_activity,
        before_opening=before_opening,
        after_opening=after_opening,
        weighted_gain=round(weighted_gain, 4),
        start_gain=round(start_gain, 4),
        middle_loss=round(middle_loss, 4),
        sequence_safety=sequence_safety,
    )



def _reference_repair_engine_suffix(result: dict[str, object]) -> str:
    if str(result.get("strategy") or "") == "embedded_reference_dialogue_anchors":
        return "+reference-dialogue-anchors"
    if str(result.get("strategy") or "") == "sparse_cold_open":
        return "+sparse-cold-open"
    return "+reference-piecewise"


def _reference_only_opening_gap(
    candidate_cues: list[tuple[float, float, str]],
    reference_cues: list[tuple[float, float, str]],
    *,
    max_clock_error_seconds: float = 12.0,
) -> dict[str, object] | None:
    """Find an early JP-silent / embedded-reference-active opening region."""
    if len(candidate_cues) < 8 or len(reference_cues) < 8:
        return None
    duration = max(
        max(end for _start, end, _text in candidate_cues),
        max(end for _start, end, _text in reference_cues),
    )
    limit = max(2.0, float(max_clock_error_seconds))
    candidates: list[dict[str, object]] = []
    for (_left_start, left_end, _left_text), (right_start, _right_end, _right_text) in zip(
        candidate_cues,
        candidate_cues[1:],
    ):
        gap_seconds = float(right_start - left_end)
        if (
            gap_seconds < 45.0
            or left_end < 15.0
            or right_start > min(duration * 0.45, 10 * 60.0)
        ):
            continue
        interior_start = left_end + min(8.0, gap_seconds * 0.12)
        interior_end = right_start - min(8.0, gap_seconds * 0.12)
        reference_inside = [
            cue
            for cue in reference_cues
            if (
                (cue[0] + cue[1]) / 2.0 >= interior_start - limit
                and (cue[0] + cue[1]) / 2.0 <= interior_end + limit
            )
        ]
        if len(reference_inside) < 4:
            continue
        active_seconds = sum(max(0.0, end - start) for start, end, _text in reference_inside)
        if active_seconds < 10.0:
            continue
        candidates.append(
            {
                "gap_start": round(float(left_end), 3),
                "gap_end": round(float(right_start), 3),
                "gap_seconds": round(gap_seconds, 3),
                "reference_cues": len(reference_inside),
                "reference_active_seconds": round(active_seconds, 3),
            }
        )
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            int(item["reference_cues"]),
            float(item["reference_active_seconds"]),
            float(item["gap_seconds"]),
            -float(item["gap_start"]),
        ),
    )


def _anchor_region_cluster(
    offsets: list[dict[str, object]],
    *,
    tolerance: float = 0.90,
    minimum_support: int = 2,
) -> dict[str, object] | None:
    if not offsets:
        return None

    minimum_support = max(1, int(minimum_support))

    if minimum_support == 1 and len(offsets) == 1:
        item = offsets[0]
        value = float(item["offset_seconds"])
        return {
            "offset_seconds": round(value, 4),
            "support": 1,
            "total": 1,
            "support_ratio": 1.0,
            "spread_seconds": 0.0,
            "values": [round(value, 4)],
            "matches": [item],
            "mean_confidence": round(float(item.get("confidence") or 0.0), 4),
        }

    values = [float(item["offset_seconds"]) for item in offsets]
    cluster = _stable_offset_cluster(values, tolerance=tolerance)
    if cluster is None:
        return None
    center = float(cluster["offset_seconds"])
    inliers = [
        item
        for item in offsets
        if abs(float(item["offset_seconds"]) - center) <= tolerance
    ]
    if len(inliers) < minimum_support:
        return None
    return {
        **cluster,
        "matches": inliers,
        "mean_confidence": round(
            statistics.fmean(float(item.get("confidence") or 0.0) for item in inliers),
            4,
        ),
    }


def _repair_with_embedded_reference_dialogue_anchors(
    aligned: Path,
    reference: Path,
    candidate_cues: list[tuple[float, float, str]],
    reference_cues: list[tuple[float, float, str]],
    cache_dir: Path,
    config: SyncConfig,
    llm: OllamaClient,
    *,
    edit_points: list[dict[str, object]] | None = None,
    force: bool = False,
    verbose: bool = False,
) -> tuple[Path, dict[str, object]]:
    """Use semantically matched dialogue timestamps as the primary video clock.

    The embedded text track belongs to the exact video, so once the LLM has
    already verified the episode identity its timestamps are ground truth.
    English-only opening lyrics are represented as a reference-only gap and are
    deliberately left unmatched rather than treated as timing evidence.
    """
    max_shift = min(
        15.0,
        max(8.0, float(config.piecewise_max_correction_seconds)),
    )
    opening = _reference_only_opening_gap(
        candidate_cues,
        reference_cues,
        max_clock_error_seconds=max_shift,
    )
    if opening is None:
        return aligned, _result("reference_dialogue_opening_gap_not_found", applied=False)

    gap_start = float(opening["gap_start"])
    gap_end = float(opening["gap_end"])
    duration = max(
        max(end for _start, end, _text in candidate_cues),
        max(end for _start, end, _text in reference_cues),
    )

    candidate_dialogue = [
        cue for cue in candidate_cues if not _is_reference_non_dialogue(cue[2])
    ]
    if len(candidate_dialogue) < 12:
        return aligned, _result("reference_dialogue_too_few_candidate_cues", applied=False)

    def select_range(start: float, end: float, *, limit: int = 10) -> list[tuple[float, float, str]]:
        selected = [
            cue
            for cue in candidate_dialogue
            if cue[1] >= start and cue[0] <= end
        ]
        if len(selected) <= limit:
            return selected
        center = (start + end) / 2.0
        ranked = sorted(
            selected,
            key=lambda cue: abs(((cue[0] + cue[1]) / 2.0) - center),
        )[:limit]
        return sorted(ranked, key=lambda cue: cue[0])

    before = select_range(max(0.0, gap_start - 150.0), gap_start - 0.01, limit=10)
    after = select_range(gap_end + 0.01, min(duration, gap_end + 180.0), limit=10)

    def select_near(center: float, *, limit: int = 8) -> list[tuple[float, float, str]]:
        ranked = sorted(
            candidate_dialogue,
            key=lambda cue: abs(((cue[0] + cue[1]) / 2.0) - center),
        )
        selected = sorted(ranked[:limit], key=lambda cue: cue[0])
        return [
            cue
            for cue in selected
            if cue[0] >= gap_end + 5.0
        ]

    middle = select_near(max(gap_end + 120.0, duration * 0.52), limit=8)
    late = select_near(max(gap_end + 240.0, duration * 0.80), limit=8)

    raw_regions = [
        ("before_opening", before),
        ("after_opening", after),
        ("middle", middle),
        ("late", late),
    ]
    region_payloads: list[dict[str, object]] = []
    region_lookup: dict[str, dict[str, list[dict[str, object]]]] = {}
    for name, japanese_cues in raw_regions:
        minimum_region_cues = 1 if name == "before_opening" else 3
        if len(japanese_cues) < minimum_region_cues:
            continue
        region_start = min(start for start, _end, _text in japanese_cues)
        region_end = max(end for _start, end, _text in japanese_cues)
        english_cues = [
            cue
            for cue in reference_cues
            if cue[1] >= region_start - max_shift - 4.0
            and cue[0] <= region_end + max_shift + 4.0
            and not _is_reference_non_dialogue(cue[2])
        ]
        if len(english_cues) < minimum_region_cues:
            continue
        if len(english_cues) > 22:
            center = (region_start + region_end) / 2.0
            english_cues = sorted(
                sorted(
                    english_cues,
                    key=lambda cue: abs(((cue[0] + cue[1]) / 2.0) - center),
                )[:22],
                key=lambda cue: cue[0],
            )

        japanese_rows = [
            {
                "index": index,
                "start": round(float(start), 3),
                "end": round(float(end), 3),
                "text": str(text)[:260],
            }
            for index, (start, end, text) in enumerate(japanese_cues)
        ]
        english_rows = [
            {
                "index": index,
                "start": round(float(start), 3),
                "end": round(float(end), 3),
                "text": str(text)[:260],
            }
            for index, (start, end, text) in enumerate(english_cues)
        ]
        region_payloads.append(
            {
                "name": name,
                "japanese": japanese_rows,
                "english": english_rows,
            }
        )
        region_lookup[name] = {
            "japanese": japanese_rows,
            "english": english_rows,
        }

    if not any(item["name"] == "before_opening" for item in region_payloads):
        return aligned, _result("reference_dialogue_before_region_missing", applied=False)
    if not any(item["name"] == "after_opening" for item in region_payloads):
        return aligned, _result("reference_dialogue_after_region_missing", applied=False)

    matched_regions = []
    total_anchor_matches = 0

    for region in region_payloads:
        one_result = llm.match_subtitle_anchor_regions(
            [region],
            force=force,
        )
        regions = one_result.get("regions")
        if isinstance(regions, list):
            for matched_region in regions:
                if not isinstance(matched_region, dict):
                    continue
                matched_regions.append(matched_region)
                matches = matched_region.get("matches")
                if isinstance(matches, list):
                    total_anchor_matches += len(matches)

    match_result = {
        "accepted": total_anchor_matches >= 2,
        "reason": "matched" if total_anchor_matches >= 2 else "too_few_matches",
        "regions": matched_regions,
        "total_matches": total_anchor_matches,
        "per_region_requests": True,
    }

    if not bool(match_result.get("accepted")):
        return aligned, _result(
            "reference_dialogue_llm_match_failed",
            applied=False,
            opening=opening,
            llm_result=match_result,
        )

    region_offsets: dict[str, list[dict[str, object]]] = {}
    for region in match_result.get("regions", []):
        if not isinstance(region, dict):
            continue
        name = str(region.get("name") or "")
        lookup = region_lookup.get(name)
        if lookup is None:
            continue
        japanese_rows = lookup["japanese"]
        english_rows = lookup["english"]
        last_japanese = -1
        last_english = -1
        used_japanese: set[int] = set()
        used_english: set[int] = set()
        accepted_matches: list[dict[str, object]] = []
        for match in region.get("matches", []):
            if not isinstance(match, dict):
                continue
            try:
                confidence = float(match.get("confidence") or 0.0)
            except (TypeError, ValueError):
                continue
            if confidence < 0.68:
                continue

            def indices(value: object) -> list[int]:
                raw = value if isinstance(value, list) else [value]
                parsed: list[int] = []
                for item in raw:
                    try:
                        parsed.append(int(item))
                    except (TypeError, ValueError):
                        pass
                return sorted(set(parsed))

            japanese_indexes = indices(match.get("japanese"))
            english_indexes = indices(match.get("english"))
            if (
                not japanese_indexes
                or not english_indexes
                or len(japanese_indexes) > 3
                or len(english_indexes) > 3
                or min(japanese_indexes) <= last_japanese
                or min(english_indexes) <= last_english
                or used_japanese.intersection(japanese_indexes)
                or used_english.intersection(english_indexes)
                or max(japanese_indexes) >= len(japanese_rows)
                or max(english_indexes) >= len(english_rows)
            ):
                continue
            ja_group = [japanese_rows[index] for index in japanese_indexes]
            en_group = [english_rows[index] for index in english_indexes]
            ja_start = min(float(item["start"]) for item in ja_group)
            ja_end = max(float(item["end"]) for item in ja_group)
            en_start = min(float(item["start"]) for item in en_group)
            en_end = max(float(item["end"]) for item in en_group)
            start_offset = en_start - ja_start
            end_offset = en_end - ja_end
            if abs(start_offset) > max_shift:
                continue

            # Translation subtitles frequently keep the same spoken line
            # on screen for different lengths. The beginning of the matched
            # utterance is the reliable clock anchor; cue end is diagnostic only.
            offset = start_offset
            accepted_matches.append(
                {
                    "offset_seconds": round(offset, 4),
                    "start_offset_seconds": round(start_offset, 4),
                    "end_offset_seconds": round(end_offset, 4),
                    "confidence": round(confidence, 4),
                    "japanese": japanese_indexes,
                    "english": english_indexes,
                    "candidate_time": round((ja_start + ja_end) / 2.0, 3),
                    "reference_time": round((en_start + en_end) / 2.0, 3),
                }
            )
            used_japanese.update(japanese_indexes)
            used_english.update(english_indexes)
            last_japanese = max(japanese_indexes)
            last_english = max(english_indexes)
        region_offsets[name] = accepted_matches

    # Build a real semantic clock timeline instead of assuming that every
    # post-opening cue shares one offset. Different releases may contain
    # additional/removed material at several points in the episode.

    before_matches = region_offsets.get("before_opening", [])
    before_lookup = region_lookup.get("before_opening")

    if not before_matches or before_lookup is None:
        return aligned, _result(
            "reference_dialogue_before_not_stable",
            applied=False,
            opening=opening,
            region_offsets=region_offsets,
            llm_result=match_result,
        )

    strongest_before = max(
        before_matches,
        key=lambda item: float(item.get("confidence") or 0.0),
    )
    if float(strongest_before.get("confidence") or 0.0) < 0.88:
        return aligned, _result(
            "reference_dialogue_before_not_confident",
            applied=False,
            opening=opening,
            region_offsets=region_offsets,
            llm_result=match_result,
        )

    def expand_compact_group(
        rows: list[dict[str, object]],
        indexes: list[int],
        *,
        maximum_gap: float = 2.5,
    ) -> list[int]:
        if not indexes:
            return []

        left = min(indexes)
        right = max(indexes)

        while left > 0:
            previous = rows[left - 1]
            current = rows[left]
            gap = float(current["start"]) - float(previous["end"])
            if gap > maximum_gap:
                break
            left -= 1

        while right + 1 < len(rows):
            current = rows[right]
            following = rows[right + 1]
            gap = float(following["start"]) - float(current["end"])
            if gap > maximum_gap:
                break
            right += 1

        return list(range(left, right + 1))

    before_ja_indexes = [
        int(index) for index in strongest_before.get("japanese", [])
    ]
    before_en_indexes = [
        int(index) for index in strongest_before.get("english", [])
    ]

    before_ja_indexes = expand_compact_group(
        before_lookup["japanese"],
        before_ja_indexes,
    )
    before_en_indexes = expand_compact_group(
        before_lookup["english"],
        before_en_indexes,
    )

    before_ja_rows = [
        before_lookup["japanese"][index]
        for index in before_ja_indexes
    ]
    before_en_rows = [
        before_lookup["english"][index]
        for index in before_en_indexes
    ]

    if not before_ja_rows or not before_en_rows:
        return aligned, _result(
            "reference_dialogue_before_block_missing",
            applied=False,
            opening=opening,
            region_offsets=region_offsets,
            llm_result=match_result,
        )

    before_ja_start = min(float(item["start"]) for item in before_ja_rows)
    before_ja_end = max(float(item["end"]) for item in before_ja_rows)
    before_en_start = min(float(item["start"]) for item in before_en_rows)
    before_en_end = max(float(item["end"]) for item in before_en_rows)

    before_offset = before_en_start - before_ja_start
    if abs(before_offset) > max_shift:
        return aligned, _result(
            "reference_dialogue_before_offset_too_large",
            applied=False,
            opening=opening,
            before_offset_seconds=round(before_offset, 4),
        )

    cold_match = {
        "offset_seconds": round(before_offset, 4),
        "start_offset_seconds": round(before_offset, 4),
        "end_offset_seconds": round(before_en_end - before_ja_end, 4),
        "confidence": round(
            float(strongest_before.get("confidence") or 0.0),
            4,
        ),
        "japanese": before_ja_indexes,
        "english": before_en_indexes,
        "candidate_time": round(
            (before_ja_start + before_ja_end) / 2.0,
            3,
        ),
        "reference_time": round(
            (before_en_start + before_en_end) / 2.0,
            3,
        ),
        "region": "before_opening",
    }

    before_cluster = {
        "offset_seconds": round(before_offset, 4),
        "support": 1,
        "total": 1,
        "support_ratio": 1.0,
        "spread_seconds": 0.0,
        "values": [round(before_offset, 4)],
        "matches": [cold_match],
        "mean_confidence": cold_match["confidence"],
    }

    timeline_anchors: list[dict[str, object]] = [
        {
            "region": "before_opening",
            "time": float(cold_match["candidate_time"]),
            "offset_seconds": round(before_offset, 4),
            "support": 1,
            "matches": [cold_match],
        }
    ]

    regional_clusters: dict[str, dict[str, object]] = {}

    for name in ("after_opening", "middle", "late"):
        cluster = _anchor_region_cluster(
            region_offsets.get(name, []),
            tolerance=0.90,
        )
        if cluster is None:
            continue

        matches = [
            item
            for item in cluster.get("matches", [])
            if isinstance(item, dict)
        ]
        if len(matches) < 2:
            continue

        mean_confidence = statistics.fmean(
            float(item.get("confidence") or 0.0)
            for item in matches
        )
        if mean_confidence < 0.88:
            continue

        anchor_time = statistics.median(
            float(item["candidate_time"])
            for item in matches
        )

        tagged_matches = []
        for item in matches:
            tagged = dict(item)
            tagged["region"] = name
            tagged_matches.append(tagged)

        cluster = {
            **cluster,
            "matches": tagged_matches,
            "anchor_time": round(anchor_time, 3),
        }
        regional_clusters[name] = cluster

        timeline_anchors.append(
            {
                "region": name,
                "time": round(anchor_time, 3),
                "offset_seconds": round(
                    float(cluster["offset_seconds"]),
                    4,
                ),
                "support": int(cluster.get("support") or len(matches)),
                "matches": tagged_matches,
            }
        )

    if "after_opening" not in regional_clusters:
        return aligned, _result(
            "reference_dialogue_after_not_stable",
            applied=False,
            opening=opening,
            before_cluster=before_cluster,
            region_offsets=region_offsets,
            llm_result=match_result,
        )

    if len(regional_clusters) < 2:
        return aligned, _result(
            "reference_dialogue_timeline_too_sparse",
            applied=False,
            opening=opening,
            before_cluster=before_cluster,
            regions=sorted(regional_clusters),
            region_offsets=region_offsets,
            llm_result=match_result,
        )

    timeline_anchors.sort(key=lambda item: float(item["time"]))

    # Use nearest semantic clock anchor (piecewise constant timeline).
    # This is deliberately not a linear interpolation: release differences are
    # commonly caused by inserted/removed sections, which create clock jumps.
    boundaries: list[float] = []

    for index in range(len(timeline_anchors) - 1):
        left = timeline_anchors[index]
        right = timeline_anchors[index + 1]

        midpoint = (
            float(left["time"]) + float(right["time"])
        ) / 2.0
        boundary, _boundary_evidence = choose_edit_boundary(
            float(left["time"]),
            float(right["time"]),
            midpoint=midpoint,
            edit_points=edit_points or (),
        )

        # The first clock transition is known to happen inside the JP-silent
        # opening. Clamp it there rather than allowing the midpoint to leak
        # into spoken dialogue.
        if (
            left["region"] == "before_opening"
            and right["region"] == "after_opening"
        ):
            boundary = min(
                gap_end,
                max(gap_start, boundary),
            )

        boundaries.append(boundary)

    def correction_for_time(timestamp: float) -> float:
        for index, boundary in enumerate(boundaries):
            if timestamp < boundary:
                return float(
                    timeline_anchors[index]["offset_seconds"]
                )
        return float(timeline_anchors[-1]["offset_seconds"])

    corrections = [
        correction_for_time((start + end) / 2.0)
        for start, end, _text in candidate_cues
    ]

    if max(abs(value) for value in corrections) < 0.12:
        return aligned, _result(
            "reference_dialogue_already_aligned",
            applied=False,
            opening=opening,
            timeline_anchors=timeline_anchors,
        )

    repaired, sequence_safety = _retime_cues_without_reordering(
        candidate_cues,
        corrections,
    )
    if repaired is None:
        return aligned, _result(
            "reference_dialogue_unsafe_sequence",
            applied=False,
            opening=opening,
            before_cluster=before_cluster,
            timeline_anchors=timeline_anchors,
            boundaries=[round(value, 3) for value in boundaries],
            sequence_safety=sequence_safety,
        )

    aligned_stat = aligned.stat()
    reference_stat = reference.stat()

    anchor_signature = ",".join(
        f"{float(item['time']):.3f}:{float(item['offset_seconds']):.4f}"
        for item in timeline_anchors
    )

    digest = hashlib.sha1(
        (
            f"reference-dialogue-anchor-v2:"
            f"{aligned.resolve()}:{aligned_stat.st_size}:"
            f"{aligned_stat.st_mtime_ns}:"
            f"{reference.resolve()}:{reference_stat.st_size}:"
            f"{reference_stat.st_mtime_ns}:"
            f"{anchor_signature}"
        ).encode()
    ).hexdigest()[:20]

    output_dir = cache_dir / "reference-dialogue-anchor"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{digest}.srt"

    if force:
        output.unlink(missing_ok=True)

    if not output.exists() or output.stat().st_size <= 0:
        write_srt(repaired, output, preserve_order=True)

    all_anchor_offsets: list[float] = []
    residuals_after: list[float] = []

    for anchor in timeline_anchors:
        anchor_offset = float(anchor["offset_seconds"])
        for item in anchor.get("matches", []):
            value = float(item["offset_seconds"])
            all_anchor_offsets.append(value)
            residuals_after.append(abs(value - anchor_offset))

    anchor_mae_before = statistics.fmean(
        abs(value)
        for value in all_anchor_offsets
    )
    anchor_mae_after = statistics.fmean(residuals_after)

    # Hold out each match from sufficiently populated regional clusters. This
    # prevents the same semantic anchor from both defining and validating its
    # own clock plateau. The single high-confidence cold-open match is excluded
    # because it intentionally represents a different edit segment.
    holdout_residuals: list[float] = []
    for anchor in timeline_anchors:
        matches = [item for item in anchor.get("matches", []) if isinstance(item, dict)]
        if len(matches) < 3:
            continue
        values = [float(item["offset_seconds"]) for item in matches]
        for index, actual in enumerate(values):
            training = values[:index] + values[index + 1 :]
            predicted = float(statistics.median(training))
            holdout_residuals.append(abs(actual - predicted))
    holdout_p95 = None
    if holdout_residuals:
        ordered_holdout = sorted(holdout_residuals)
        holdout_p95 = ordered_holdout[
            min(len(ordered_holdout) - 1, max(0, math.ceil(len(ordered_holdout) * 0.95) - 1))
        ]

    before_activity = compare_timing_activity(aligned, reference)
    after_activity = compare_timing_activity(output, reference)

    middle_loss = (
        float(before_activity.get("middle") or 0.0)
        - float(after_activity.get("middle") or 0.0)
        if before_activity.get("available")
        and after_activity.get("available")
        else 0.0
    )

    weighted_loss = (
        float(before_activity.get("weighted") or 0.0)
        - float(after_activity.get("weighted") or 0.0)
        if before_activity.get("available")
        and after_activity.get("available")
        else 0.0
    )

    accepted = (
        anchor_mae_after <= 0.75
        and anchor_mae_before - anchor_mae_after >= 0.50
        and middle_loss <= 0.035
        and weighted_loss <= 0.020
        and (holdout_p95 is None or holdout_p95 <= 1.25)
    )

    after_offset = float(
        regional_clusters["after_opening"]["offset_seconds"]
    )

    post_matches = []
    for name, cluster in regional_clusters.items():
        post_matches.extend(cluster.get("matches", []))

    post_cluster = {
        "offset_seconds": round(after_offset, 4),
        "support": len(post_matches),
        "matches": post_matches,
        "regions": sorted(regional_clusters),
    }

    if verbose:
        compact_anchors = [
            (
                str(item["region"]),
                round(float(item["time"]), 1),
                round(float(item["offset_seconds"]), 2),
                int(item.get("support") or 0),
            )
            for item in timeline_anchors
        ]
        print(
            "  Direct embedded-reference dialogue anchors: "
            f"anchors={compact_anchors}, "
            f"boundaries={[round(value, 1) for value in boundaries]}, "
            f"anchor_mae={anchor_mae_before:.2f}"
            f"->{anchor_mae_after:.2f}s, "
            f"activity={before_activity.get('weighted', '-')}"
            f"->{after_activity.get('weighted', '-')}, "
            f"accepted={accepted}"
        )

    if not accepted:
        output.unlink(missing_ok=True)
        return aligned, _result(
            "reference_dialogue_no_safe_improvement",
            applied=False,
            opening=opening,
            before_cluster=before_cluster,
            post_cluster=post_cluster,
            timeline_anchors=timeline_anchors,
            boundaries=[round(value, 3) for value in boundaries],
            region_offsets=region_offsets,
            llm_result=match_result,
            anchor_mae_before=round(anchor_mae_before, 4),
            anchor_mae_after=round(anchor_mae_after, 4),
            holdout_p95_seconds=(round(holdout_p95, 4) if holdout_p95 is not None else None),
            before_activity=before_activity,
            after_activity=after_activity,
            middle_loss=round(middle_loss, 4),
            weighted_loss=round(weighted_loss, 4),
        )

    return output, _result(
        "applied",
        applied=True,
        strategy="embedded_reference_dialogue_anchors",
        output=str(output),
        opening=opening,
        before_offset_seconds=round(before_offset, 4),
        after_offset_seconds=round(after_offset, 4),
        before_cluster=before_cluster,
        post_cluster=post_cluster,
        post_regions=sorted(regional_clusters),
        timeline_anchors=timeline_anchors,
        boundaries=[round(value, 3) for value in boundaries],
        region_offsets=region_offsets,
        anchor_mae_before=round(anchor_mae_before, 4),
        anchor_mae_after=round(anchor_mae_after, 4),
        holdout_p95_seconds=(round(holdout_p95, 4) if holdout_p95 is not None else None),
        before_activity=before_activity,
        after_activity=after_activity,
        middle_loss=round(middle_loss, 4),
        weighted_loss=round(weighted_loss, 4),
        sequence_safety=sequence_safety,
    )



def _repair_sparse_cold_open(
    aligned: Path,
    reference: Path,
    candidate_cues: list[tuple[float, float, str]],
    reference_cues: list[tuple[float, float, str]],
    cache_dir: Path,
    *,
    force: bool = False,
) -> tuple[Path, dict[str, object]]:
    """Repair a short spoken prologue before a long non-dialogue opening.

    Broadcast captions can keep music/SFX cues throughout an opening while the
    embedded translation stays empty. Broad activity windows then hide a local
    prologue clock. Match a compact onset fingerprint, require the first main
    dialogue to already be aligned, and move the whole pre-main block rigidly.
    """
    candidate_dialogue = [
        (index, cue)
        for index, cue in enumerate(candidate_cues)
        if not _is_reference_non_dialogue(cue[2])
    ]
    reference_dialogue = [
        (index, cue)
        for index, cue in enumerate(reference_cues)
        if not _is_reference_non_dialogue(cue[2])
    ]
    diagnostics: dict[str, object] = {
        "applied": False,
        "reason": "sparse_cold_open_not_applicable",
    }
    if len(candidate_dialogue) < 4 or len(reference_dialogue) < 4:
        diagnostics["reason"] = "sparse_cold_open_too_few_dialogue_cues"
        return aligned, diagnostics

    split: int | None = None
    for position in range(3, min(7, len(candidate_dialogue))):
        _left_index, left = candidate_dialogue[position - 1]
        _right_index, right = candidate_dialogue[position]
        if (
            candidate_dialogue[0][1][0] <= 45.0
            and left[1] <= 90.0
            and right[0] - left[1] >= 45.0
            and right[0] <= 240.0
        ):
            split = position
            break
    if split is None:
        diagnostics["reason"] = "sparse_cold_open_dialogue_gap_not_found"
        return aligned, diagnostics

    cold_dialogue = candidate_dialogue[:split]
    next_dialogue_index, next_dialogue = candidate_dialogue[split]
    post_error = min(
        abs(float(cue[0]) - float(next_dialogue[0]))
        for _index, cue in reference_dialogue
    )
    diagnostics.update(
        {
            "cold_dialogue_cues": len(cold_dialogue),
            "cold_block_cues": next_dialogue_index,
            "next_dialogue_start": round(float(next_dialogue[0]), 3),
            "post_opening_error_seconds": round(post_error, 4),
        }
    )
    if post_error > 0.90:
        diagnostics["reason"] = "sparse_cold_open_post_dialogue_not_stable"
        return aligned, diagnostics

    reference_limit = float(next_dialogue[0]) - 20.0
    eligible_reference = [
        cue for _index, cue in reference_dialogue if float(cue[1]) <= reference_limit
    ]
    count = len(cold_dialogue)
    if len(eligible_reference) < count:
        diagnostics["reason"] = "sparse_cold_open_reference_block_missing"
        return aligned, diagnostics

    source_starts = [float(cue[0]) for _index, cue in cold_dialogue]
    source_ends = [float(cue[1]) for _index, cue in cold_dialogue]
    candidates: list[dict[str, object]] = []
    for start_index in range(len(eligible_reference) - count + 1):
        block = eligible_reference[start_index : start_index + count]
        reference_starts = [float(cue[0]) for cue in block]
        reference_ends = [float(cue[1]) for cue in block]
        offsets = [
            reference_time - source_time
            for source_time, reference_time in zip(source_starts, reference_starts)
        ]
        offsets.extend(
            reference_time - source_time
            for source_time, reference_time in zip(source_ends, reference_ends)
        )
        correction = float(statistics.median(offsets))
        dispersion = max(abs(value - correction) for value in offsets)
        relative_errors = [
            abs(
                (reference_starts[index] - reference_starts[0])
                - (source_starts[index] - source_starts[0])
            )
            for index in range(1, count)
        ]
        fingerprint_error = statistics.fmean(relative_errors)
        candidates.append(
            {
                "block": block,
                "reference_start": reference_starts[0],
                "correction": correction,
                "dispersion": dispersion,
                "fingerprint_error": fingerprint_error,
                "score": dispersion + fingerprint_error,
            }
        )

    candidates.sort(key=lambda item: float(item["score"]))
    best = candidates[0]
    best_block = best["block"]
    assert isinstance(best_block, list)
    best_reference_starts = [float(cue[0]) for cue in best_block]
    correction = float(best["correction"])
    dispersion = float(best["dispersion"])
    fingerprint_error = float(best["fingerprint_error"])
    runner_up_margin = (
        float(candidates[1]["score"]) - float(best["score"])
        if len(candidates) > 1
        else None
    )
    before_error = statistics.fmean(
        abs(reference_time - source_time)
        for source_time, reference_time in zip(source_starts, best_reference_starts)
    )
    after_error = statistics.fmean(
        abs(reference_time - (source_time + correction))
        for source_time, reference_time in zip(source_starts, best_reference_starts)
    )
    diagnostics.update(
        {
            "reference_start": round(float(best["reference_start"]), 3),
            "correction_seconds": round(correction, 4),
            "offset_dispersion_seconds": round(dispersion, 4),
            "gap_fingerprint_error_seconds": round(fingerprint_error, 4),
            "runner_up_margin": (
                round(runner_up_margin, 4) if runner_up_margin is not None else None
            ),
            "onset_mae_before_seconds": round(before_error, 4),
            "onset_mae_after_seconds": round(after_error, 4),
        }
    )
    if not 2.5 <= abs(correction) <= 20.0:
        diagnostics["reason"] = "sparse_cold_open_correction_out_of_range"
        return aligned, diagnostics
    if dispersion > 0.80 or fingerprint_error > 0.80:
        diagnostics["reason"] = "sparse_cold_open_fingerprint_unstable"
        return aligned, diagnostics
    if runner_up_margin is not None and runner_up_margin < 0.75:
        diagnostics["reason"] = "sparse_cold_open_fingerprint_ambiguous"
        return aligned, diagnostics
    if before_error - after_error < 2.0 or after_error > 0.65:
        diagnostics["reason"] = "sparse_cold_open_does_not_improve"
        return aligned, diagnostics

    shifted = [
        (
            start + correction if index < next_dialogue_index else start,
            end + correction if index < next_dialogue_index else end,
            text,
        )
        for index, (start, end, text) in enumerate(candidate_cues)
    ]
    if any(start < 0.0 or end <= start for start, end, _text in shifted):
        diagnostics["reason"] = "sparse_cold_open_invalid_timestamps"
        return aligned, diagnostics
    if any(
        shifted[index][0] + 0.001 < shifted[index - 1][0]
        for index in range(1, len(shifted))
    ):
        diagnostics["reason"] = "sparse_cold_open_reorders_cues"
        return aligned, diagnostics
    if (
        next_dialogue_index > 0
        and shifted[next_dialogue_index - 1][1] > float(next_dialogue[0]) - 0.10
    ):
        diagnostics["reason"] = "sparse_cold_open_overlaps_main_dialogue"
        return aligned, diagnostics

    aligned_stat = aligned.stat()
    reference_stat = reference.stat()
    digest = hashlib.sha1(
        (
            f"sparse-cold-open-v1:{aligned.resolve()}:{aligned_stat.st_size}:"
            f"{aligned_stat.st_mtime_ns}:{reference.resolve()}:{reference_stat.st_size}:"
            f"{reference_stat.st_mtime_ns}:{correction:.6f}:{next_dialogue_index}"
        ).encode()
    ).hexdigest()[:20]
    output_dir = cache_dir / "reference-sparse-cold-open"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{digest}.srt"
    if force:
        output.unlink(missing_ok=True)
    if not output.exists() or output.stat().st_size <= 0:
        write_srt(shifted, output, preserve_order=True)

    diagnostics.update(
        {
            "applied": True,
            "reason": "sparse_cold_open_fingerprint_improved",
            "strategy": "sparse_cold_open",
            "output": str(output),
        }
    )
    return output, diagnostics


def repair_with_embedded_reference_piecewise(
    aligned: Path,
    reference: Path,
    cache_dir: Path,
    config: SyncConfig,
    *,
    llm: OllamaClient | None = None,
    edit_points: list[dict[str, object]] | None = None,
    force: bool = False,
    verbose: bool = False,
) -> tuple[Path, dict[str, object]]:
    """Repair residual cold-open drift after a globally accepted ALASS result.

    Broadcast and streaming masters can differ around title cards, recaps, and
    openings. A single global offset may therefore be wrong by several seconds
    at the beginning while becoming correct later. This function estimates
    conservative local residuals against the exact embedded subtitle clock and
    applies a monotonic piecewise correction only when timing-activity metrics
    measurably improve.
    """
    if not config.piecewise_repair:
        return aligned, _result("reference_piecewise_disabled", applied=False)
    try:
        candidate_cues = parse_srt(aligned)
        reference_cues = parse_srt(reference)
    except OSError as exc:
        return aligned, _result("reference_piecewise_read_error", applied=False, error=str(exc))
    if len(candidate_cues) < 12 or len(reference_cues) < 12:
        return aligned, _result("reference_piecewise_too_few_cues", applied=False)

    duration = max(
        max(end for _start, end, _text in candidate_cues),
        max(end for _start, end, _text in reference_cues),
    )
    if duration < 180.0:
        return aligned, _result("reference_piecewise_too_short", applied=False)

    sparse_path, sparse_result = _repair_sparse_cold_open(
        aligned,
        reference,
        candidate_cues,
        reference_cues,
        cache_dir,
        force=force,
    )
    if sparse_result.get("applied"):
        return sparse_path, sparse_result

    if llm is not None:
        direct_path, direct_result = _repair_with_embedded_reference_dialogue_anchors(
            aligned,
            reference,
            candidate_cues,
            reference_cues,
            cache_dir,
            config,
            llm,
            edit_points=edit_points,
            force=force,
            verbose=verbose,
        )
        if direct_result.get("applied"):
            return direct_path, direct_result
        if verbose:
            print(
                "  Direct embedded-reference dialogue anchors: "
                f"fallback reason={direct_result.get('reason', '-')}"
            )
            region_offsets = direct_result.get("region_offsets")
            if isinstance(region_offsets, dict):
                for region_name, matches in region_offsets.items():
                    if not isinstance(matches, list):
                        continue
                    compact = [
                        {
                            "offset": item.get("offset_seconds"),
                            "start": item.get("start_offset_seconds"),
                            "end": item.get("end_offset_seconds"),
                            "confidence": item.get("confidence"),
                            "ja": item.get("japanese"),
                            "en": item.get("english"),
                        }
                        for item in matches
                        if isinstance(item, dict)
                    ]
                    print(
                        f"    anchor region {region_name}: {compact}"
                    )

    plateau_path, plateau_result = _repair_stable_opening_plateaus(
        aligned,
        reference,
        candidate_cues,
        reference_cues,
        cache_dir,
        config,
        force=force,
        verbose=verbose,
    )
    if plateau_result.get("applied"):
        return plateau_path, plateau_result

    # Large equal windows are good for gradual drift across an episode, but they
    # can completely hide a cold-open-only error: a 5-6 second mismatch in the
    # first minute is averaged together with two already-correct minutes. Probe
    # the opening with short overlapping windows, then add broader windows for
    # the rest of the episode.
    cold_regions = [
        (0.0, min(duration, 35.0)),
        (10.0, min(duration, 50.0)),
    ]
    transition_regions = [
        (25.0, min(duration, 75.0)),
        (45.0, min(duration, 120.0)),
        (90.0, min(duration, 240.0)),
    ]
    early_regions = [*cold_regions, *transition_regions]
    broad_window_seconds = min(150.0, max(75.0, duration / 10.0))
    broad_count = 10
    first_center = broad_window_seconds / 2.0
    last_center = max(first_center, duration - broad_window_seconds / 2.0)
    broad_step = (last_center - first_center) / max(1, broad_count - 1)
    regions: list[tuple[float, float]] = list(early_regions)
    for index in range(broad_count):
        center = first_center + index * broad_step
        regions.append(
            (
                max(0.0, center - broad_window_seconds / 2.0),
                min(duration, center + broad_window_seconds / 2.0),
            )
        )

    # Cold opens can differ by much more than the ordinary residual drift.
    # In particular, streaming masters may insert a 15-20 second title card
    # before dialogue while the broadcast subtitle clock starts immediately.
    # Searching only ±12s creates a periodic false match on a neighbouring cue.
    broad_max_shift = min(12.0, max(4.0, float(config.piecewise_max_correction_seconds)))
    cold_max_shift = min(30.0, max(20.0, float(config.piecewise_max_correction_seconds)))
    windows: list[dict[str, object]] = []
    raw_anchor_records: list[tuple[float, float, str]] = []
    seen_regions: set[tuple[int, int]] = set()
    cold_keys = {
        (int(round(start * 10)), int(round(end * 10)))
        for start, end in cold_regions
        if end > start + 15.0
    }
    transition_keys = {
        (int(round(start * 10)), int(round(end * 10)))
        for start, end in transition_regions
        if end > start + 15.0
    }
    early_keys = cold_keys | transition_keys
    for region_start, region_end in regions:
        if region_end <= region_start + 15.0:
            continue
        key = (int(round(region_start * 10)), int(round(region_end * 10)))
        if key in seen_regions:
            continue
        seen_regions.add(key)
        center = (region_start + region_end) / 2.0
        is_short_cold_probe = key in cold_keys
        result = _windowed_reference_shift(
            candidate_cues,
            reference_cues,
            region_start=region_start,
            region_end=region_end,
            max_shift_seconds=cold_max_shift if is_short_cold_probe else broad_max_shift,
        )
        result["probe_kind"] = "cold" if is_short_cold_probe else (
            "transition" if key in transition_keys else "broad"
        )
        result["center"] = round(center, 3)
        windows.append(result)
        if result.get("confident"):
            kind = str(result["probe_kind"])
            raw_anchor_records.append(
                (center, float(result.get("shift_seconds") or 0.0), kind)
            )

    if len(raw_anchor_records) < 3:
        return aligned, _result(
            "reference_piecewise_too_few_anchors",
            applied=False,
            anchors=[(timepoint, offset) for timepoint, offset, _kind in raw_anchor_records],
            windows=windows,
        )
    raw_anchor_records.sort()

    # Periodic dialogue cadence can create a mathematically perfect alias one
    # cue-spacing away (for example +6.0s versus -1.3s). Use the first broad
    # windows as a continuity reference and replace only opening estimates that
    # disagree in sign or jump implausibly far from that local trend.
    broad_records = [record for record in raw_anchor_records if record[2] == "broad"]
    cold_records = [record for record in raw_anchor_records if record[2] == "cold"]

    # A subtitle track with SDH/music/SFX captions can be much denser than a
    # normal embedded translation. In that case activity correlation may align
    # only a small subset of early cues and invent a large +10..20 second
    # cold-open shift even though the globally accepted ALASS result is already
    # correct. Large opening corrections therefore need substantially stronger
    # onset support than ordinary residual drift.
    weak_large_cold_probes: list[dict[str, object]] = []
    strong_cold_records: list[tuple[float, float, str]] = []
    confident_cold_windows = {
        round(float(item.get("center") or 0.0), 3): item
        for item in windows
        if item.get("probe_kind") == "cold" and item.get("confident")
    }
    for record in [item for item in raw_anchor_records if item[2] == "cold"]:
        window = confident_cold_windows.get(round(float(record[0]), 3))
        if window is None:
            continue
        offset = abs(float(record[1]))
        source_onsets = int(window.get("source_onsets") or 0)
        reference_onsets = int(window.get("reference_onsets") or 0)
        onset_ratio = (
            min(source_onsets, reference_onsets) / max(source_onsets, reference_onsets)
            if source_onsets and reference_onsets
            else 0.0
        )
        coverage = float(window.get("coverage") or 0.0)
        matched = int(window.get("matched_onsets") or 0)
        edge_error = max(
            float(window.get("first_edge_error") or 0.0),
            float(window.get("last_edge_error") or 0.0),
        )
        large_shift_is_strong = (
            offset <= 8.0
            or (
                coverage >= 0.30
                and onset_ratio >= 0.58
                and matched >= 6
                and edge_error <= 5.0
            )
        )
        if large_shift_is_strong:
            strong_cold_records.append(record)
        else:
            weak_large_cold_probes.append(
                {
                    "timepoint": round(float(record[0]), 3),
                    "offset_seconds": round(float(record[1]), 3),
                    "coverage": round(coverage, 4),
                    "onset_ratio": round(onset_ratio, 4),
                    "matched_onsets": matched,
                    "edge_error": round(edge_error, 3),
                }
            )

    cold_cluster_offset: float | None = None
    if len(strong_cold_records) >= 2:
        cold_values = [offset for _timepoint, offset, _kind in strong_cold_records]
        cold_median = float(statistics.median(cold_values))
        clustered = [value for value in cold_values if abs(value - cold_median) <= 1.5]
        if len(clustered) >= 2:
            cold_cluster_offset = float(statistics.median(clustered))
    elif (
        len(strong_cold_records) == 1
        and abs(strong_cold_records[0][1]) > broad_max_shift + 1.0
    ):
        # A shift outside the normal ±12s search range cannot be represented by
        # any broad probe. Keep it only when the onset evidence above is strong.
        cold_cluster_offset = strong_cold_records[0][1]

    stabilized_records: list[tuple[float, float, str]] = []
    continuity_limit = max(2.5, float(config.piecewise_jump_threshold_seconds))
    weak_large_cold_times = {
        round(float(item["timepoint"]), 3) for item in weak_large_cold_probes
    }
    for timepoint, offset, kind in raw_anchor_records:
        if kind == "cold" and round(float(timepoint), 3) in weak_large_cold_times:
            continue
        # Two agreeing short cold-open probes are more trustworthy than a broad
        # 0-150s window that straddles a real edit point. Do not overwrite that
        # cluster with the later, already-correct episode trend.
        if kind == "cold" and cold_cluster_offset is not None:
            offset = cold_cluster_offset
        elif kind == "transition" and broad_records:
            nearest_time, nearest_offset, _ = min(
                broad_records,
                key=lambda record: abs(record[0] - timepoint),
            )
            opposite_sign = offset * nearest_offset < 0 and abs(offset) > 0.5 and abs(nearest_offset) > 0.5
            implausible_jump = abs(offset - nearest_offset) > continuity_limit
            if abs(nearest_time - timepoint) <= 180.0 and (opposite_sign or implausible_jump):
                offset = nearest_offset
        stabilized_records.append((timepoint, offset, kind))

    if cold_cluster_offset is not None:
        stabilized_records = [
            record
            for record in stabilized_records
            if record[2] == "cold"
            or record[0] >= 120.0
            or abs(record[1] - cold_cluster_offset) <= continuity_limit
        ]

    raw_anchors = [(timepoint, offset) for timepoint, offset, _kind in stabilized_records]
    if len(raw_anchors) < 3:
        return aligned, _result(
            "reference_piecewise_weak_large_cold_open",
            applied=False,
            anchors=raw_anchors,
            windows=windows,
            weak_large_cold_probes=weak_large_cold_probes,
        )
    smoothed_anchors = _smooth_anchors(raw_anchors, radius=1)
    # Keep the short-window opening estimates intact. Median smoothing across a
    # 20s/50s/100s anchor and a broad 150s anchor can otherwise erase the exact
    # cold-open correction we added these probes to detect.
    opening_cutoff = min(180.0, max(90.0, duration * 0.15))
    anchors = [
        (timepoint, raw_offset if timepoint <= opening_cutoff else smooth_offset)
        for (timepoint, raw_offset), (_, smooth_offset) in zip(raw_anchors, smoothed_anchors)
    ]
    if weak_large_cold_probes and verbose:
        rejected = [
            (item["offset_seconds"], item["coverage"], item["onset_ratio"])
            for item in weak_large_cold_probes
        ]
        print(
            "  Piecewise: отклонена слабая большая cold-open поправка "
            f"(offset, coverage, onset_ratio)={rejected}"
        )
    if max(abs(offset) for _center, offset in anchors) < config.piecewise_min_offset_seconds:
        return aligned, _result(
            "reference_piecewise_not_needed",
            applied=False,
            anchors=anchors,
            windows=windows,
            weak_large_cold_probes=weak_large_cold_probes,
        )

    aligned_stat = aligned.stat()
    reference_stat = reference.stat()
    digest = hashlib.sha1(
        (
            f"reference-piecewise-v6:{aligned.resolve()}:{aligned_stat.st_size}:"
            f"{aligned_stat.st_mtime_ns}:{reference.resolve()}:{reference_stat.st_size}:"
            f"{reference_stat.st_mtime_ns}:{anchors}"
        ).encode()
    ).hexdigest()[:20]
    output_dir = cache_dir / "reference-piecewise"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{digest}.srt"
    if force:
        output.unlink(missing_ok=True)

    sequence_safety: dict[str, object] = {"reason": "cached"}
    edit_boundaries: list[dict[str, float]] = []
    if not output.exists() or output.stat().st_size <= 0:
        jump_threshold = max(8.0, float(config.piecewise_jump_threshold_seconds))
        discontinuity_threshold = max(2.5, float(config.piecewise_jump_threshold_seconds))

        # A long subtitle-free interval usually marks an opening/title-card edit.
        # Do not interpolate a cold-open residual through that cut: doing so can
        # leave the first dialogue after the opening one or two seconds late even
        # when the next broad anchor already says the clocks match.  Instead,
        # switch from the left anchor to the right anchor at the first cue after
        # the largest gap overlapping that anchor pair.
        cue_gaps = [
            (previous_end, next_start, next_start - previous_end)
            for (_previous_start, previous_end, _previous_text),
                (next_start, _next_end, _next_text)
            in zip(candidate_cues, candidate_cues[1:])
            if next_start - previous_end >= 20.0
        ]
        for (left_time, left_offset), (right_time, right_offset) in zip(anchors, anchors[1:]):
            if abs(right_offset - left_offset) < discontinuity_threshold:
                continue
            overlapping = [
                gap
                for gap in cue_gaps
                if gap[0] <= right_time and gap[1] >= left_time
            ]
            midpoint = (left_time + right_time) / 2.0
            boundary, evidence = choose_edit_boundary(
                left_time,
                right_time,
                midpoint=midpoint,
                edit_points=edit_points or (),
                cue_gaps=overlapping,
            )
            if evidence.get("kind") == "midpoint":
                continue
            edit_boundaries.append(
                {
                    "left_time": float(left_time),
                    "right_time": float(right_time),
                    "boundary": float(boundary),
                    "left_offset": float(left_offset),
                    "right_offset": float(right_offset),
                    "gap_seconds": float(evidence.get("gap_seconds") or 0.0),
                }
            )

        def correction_for_cue(start: float, end: float) -> float:
            midpoint = (start + end) / 2.0
            for boundary in edit_boundaries:
                if boundary["left_time"] <= midpoint <= boundary["right_time"]:
                    return (
                        boundary["left_offset"]
                        if midpoint < boundary["boundary"]
                        else boundary["right_offset"]
                    )
            return _piecewise_offset(anchors, midpoint, jump_threshold)

        corrections = [
            correction_for_cue(start, end)
            for start, end, _text in candidate_cues
        ]
        repaired, sequence_safety = _retime_cues_without_reordering(
            candidate_cues,
            corrections,
        )
        if repaired is None:
            return aligned, _result(
                "reference_piecewise_unsafe_sequence",
                applied=False,
                anchors=anchors,
                windows=windows,
                weak_large_cold_probes=weak_large_cold_probes,
                sequence_safety=sequence_safety,
                edit_boundaries=edit_boundaries,
            )

        # Some Crunchyroll tracks put the same on-screen SFX cue on screen twice
        # around the first spoken line. Activity matching cannot distinguish the
        # repeated sign from dialogue and may anchor a short cold-open several
        # seconds too early (Hyakkano S03E05: two overlapping "Krrrack!!" cues).
        # When a duplicated reference cue overlaps the first unique reference cue,
        # use that unique cue as the cold-open dialogue anchor. Keep the whole
        # pre-opening block rigid so we do not distort Japanese cue durations.
        cold_open_reference_anchor: dict[str, object] | None = None
        long_gap_index = next(
            (
                index
                for index, ((_, previous_end, _), (next_start, _, _))
                in enumerate(zip(candidate_cues, candidate_cues[1:]), start=1)
                if next_start - previous_end >= 60.0 and previous_end <= 90.0
            ),
            None,
        )
        if long_gap_index is not None and repaired:
            cold_end = candidate_cues[long_gap_index - 1][1]
            window_refs = [
                cue for cue in reference_cues
                if cue[1] >= candidate_cues[0][0] - 2.0 and cue[0] <= cold_end + 10.0
            ]

            def _reference_key(text: str) -> str:
                value = re.sub(r"\{[^}]*\}", " ", text)
                value = re.sub(r"<[^>]+>", " ", value)
                value = re.sub(r"[^0-9A-Za-z]+", "", value).casefold()
                return value

            counts: dict[str, int] = {}
            for _start, _end, text in window_refs:
                key = _reference_key(text)
                if key:
                    counts[key] = counts.get(key, 0) + 1
            duplicated = [cue for cue in window_refs if counts.get(_reference_key(cue[2]), 0) > 1]
            unique = [cue for cue in window_refs if counts.get(_reference_key(cue[2]), 0) == 1]
            dialogue_anchor = next(
                (
                    cue for cue in unique
                    if cue[0] >= candidate_cues[0][0] + 2.0
                    and any(cue[0] < dup[1] and cue[1] > dup[0] for dup in duplicated)
                ),
                None,
            )
            if dialogue_anchor is not None:
                extra_shift = float(dialogue_anchor[0] - repaired[0][0])
                if 2.5 <= extra_shift <= 12.0:
                    shifted = []
                    for index, (start, end, text) in enumerate(repaired):
                        if index < long_gap_index:
                            shifted.append((start + extra_shift, end + extra_shift, text))
                        else:
                            shifted.append((start, end, text))
                    # The opening gap is long by construction, but keep an explicit
                    # monotonic safety check before accepting the special anchor.
                    if all(
                        shifted[index][0] >= shifted[index - 1][0]
                        and shifted[index][1] > shifted[index][0]
                        for index in range(1, len(shifted))
                    ):
                        repaired = shifted
                        cold_open_reference_anchor = {
                            "reference_start": round(float(dialogue_anchor[0]), 3),
                            "extra_shift": round(extra_shift, 3),
                            "cold_cues": int(long_gap_index),
                            "duplicate_reference_cues": len(duplicated),
                        }
                        sequence_safety = dict(sequence_safety)
                        sequence_safety["cold_open_reference_anchor"] = cold_open_reference_anchor
        write_srt(repaired, output, preserve_order=True)

    before = compare_timing_activity(aligned, reference)
    after = compare_timing_activity(output, reference)
    before_cold = compare_timing_activity(aligned, reference, priority_seconds=60.0)
    after_cold = compare_timing_activity(output, reference, priority_seconds=60.0)
    if (
        not before.get("available")
        or not after.get("available")
        or not before_cold.get("available")
        or not after_cold.get("available")
    ):
        return aligned, _result(
            "reference_piecewise_metrics_unavailable",
            applied=False,
            anchors=anchors,
            windows=windows,
            weak_large_cold_probes=weak_large_cold_probes,
            before=before,
            after=after,
            before_cold=before_cold,
            after_cold=after_cold,
            edit_boundaries=edit_boundaries,
        )

    start_gain = float(after.get("start") or 0.0) - float(before.get("start") or 0.0)
    cold_start_gain = float(after_cold.get("start") or 0.0) - float(before_cold.get("start") or 0.0)
    middle_loss = float(before.get("middle") or 0.0) - float(after.get("middle") or 0.0)
    weighted_gain = float(after.get("weighted") or 0.0) - float(before.get("weighted") or 0.0)
    cold_weighted_gain = float(after_cold.get("weighted") or 0.0) - float(
        before_cold.get("weighted") or 0.0
    )
    accepted = (
        middle_loss <= 0.025
        and (
            # A small but consistent opening gain is worthwhile for broadcast
            # captions. Grand Blue S03E05 improved 2.77 percentage points and
            # was previously rejected by the overly strict 3-point boundary.
            (cold_start_gain >= 0.025 and cold_weighted_gain >= 0.004)
            or (start_gain >= 0.025 and weighted_gain >= 0.004)
            or (weighted_gain >= 0.015 and start_gain >= -0.005)
        )
    )
    if verbose:
        print(
            "  Piecewise-проверка по встроенному эталону: "
            f"anchors={[(round(t, 1), round(o, 2)) for t, o in anchors]}, "
            f"cold={before_cold.get('start')}→{after_cold.get('start')}, "
            f"start={before.get('start')}→{after.get('start')}, "
            f"middle={before.get('middle')}→{after.get('middle')}, "
            f"weighted={before.get('weighted')}→{after.get('weighted')}, "
            f"accepted={accepted}"
        )
    if not accepted:
        return aligned, _result(
            "reference_piecewise_no_improvement",
            applied=False,
            anchors=anchors,
            windows=windows,
            weak_large_cold_probes=weak_large_cold_probes,
            before=before,
            after=after,
            before_cold=before_cold,
            after_cold=after_cold,
            start_gain=round(start_gain, 4),
            cold_start_gain=round(cold_start_gain, 4),
            middle_loss=round(middle_loss, 4),
            weighted_gain=round(weighted_gain, 4),
            cold_weighted_gain=round(cold_weighted_gain, 4),
            edit_boundaries=edit_boundaries,
        )
    return output, _result(
        "applied",
        applied=True,
        output=str(output),
        anchors=anchors,
        windows=windows,
        weak_large_cold_probes=weak_large_cold_probes,
        before=before,
        after=after,
        before_cold=before_cold,
        after_cold=after_cold,
        start_gain=round(start_gain, 4),
        cold_start_gain=round(cold_start_gain, 4),
        middle_loss=round(middle_loss, 4),
        weighted_gain=round(weighted_gain, 4),
        cold_weighted_gain=round(cold_weighted_gain, 4),
        sequence_safety=sequence_safety,
        edit_boundaries=edit_boundaries,
    )




def _is_reference_non_dialogue(text: str) -> bool:
    """Return True for captions that should not drive cross-language cue alignment.

    Embedded subtitle tracks often mix dialogue with long legal notices, music
    markers and isolated sound-effect captions. Those entries have no stable
    one-to-one equivalent in a Japanese broadcast-caption track and can pull a
    timing match toward the wrong neighbouring line.
    """
    compact = " ".join(str(text or "").split()).strip()
    if not compact:
        return True
    if len(compact) > 300:
        return True
    if any(symbol in compact for symbol in ("♬", "♪", "♫")):
        return True
    if (
        (compact.startswith("(") and compact.endswith(")"))
        or (compact.startswith("（") and compact.endswith("）"))
    ):
        return True
    meaningful = sum(
        character.isalnum()
        or "\u3040" <= character <= "\u30ff"
        or "\u4e00" <= character <= "\u9fff"
        for character in compact
    )
    return meaningful < max(2, int(len(compact) * 0.15))


def _cue_group_span(
    cues: list[tuple[float, float, str]],
    start_index: int,
    count: int,
) -> tuple[float, float]:
    group = cues[start_index : start_index + count]
    return group[0][0], max(end for _start, end, _text in group)


def refine_with_embedded_reference_groups(
    aligned: Path,
    reference: Path,
    cache_dir: Path,
    *,
    matching_basis: Path | None = None,
    force: bool = False,
    verbose: bool = False,
) -> tuple[Path, dict[str, object]]:
    """Refine small per-cue residuals while allowing one-to-many translations.

    A Japanese caption track commonly splits one spoken sentence into two or
    three short cues while an English subtitle keeps it as one longer cue (and
    vice versa). Comparing individual onset indexes therefore creates apparent
    ±1 second jitter. We align monotonic groups of up to three cues on either
    side, then map the Japanese cue boundaries proportionally inside the matched
    English group. Corrections are deliberately capped to small residuals; large
    edit differences remain the job of ALASS/reference-piecewise repair.
    """
    try:
        candidate_cues = parse_srt(aligned)
        basis_cues = parse_srt(matching_basis) if matching_basis is not None else candidate_cues
        reference_cues = parse_srt(reference)
    except OSError as exc:
        return aligned, _result("reference_groups_read_error", applied=False, error=str(exc))
    if len(candidate_cues) < 12 or len(reference_cues) < 12:
        return aligned, _result("reference_groups_too_few_cues", applied=False)
    if len(basis_cues) != len(candidate_cues):
        return aligned, _result(
            "reference_groups_basis_mismatch",
            applied=False,
            aligned_cues=len(candidate_cues),
            basis_cues=len(basis_cues),
        )

    candidate_dialogue = [
        (index, cue)
        for index, cue in enumerate(basis_cues)
        if not _is_reference_non_dialogue(cue[2])
    ]
    reference_dialogue = [
        (index, cue)
        for index, cue in enumerate(reference_cues)
        if not _is_reference_non_dialogue(cue[2])
    ]
    if len(candidate_dialogue) < 10 or len(reference_dialogue) < 10:
        return aligned, _result("reference_groups_not_enough_dialogue", applied=False)

    candidate = [cue for _index, cue in candidate_dialogue]
    reference_items = [cue for _index, cue in reference_dialogue]
    candidate_count = len(candidate)
    reference_count = len(reference_items)
    infinity = float("inf")
    dp = [[infinity] * (reference_count + 1) for _ in range(candidate_count + 1)]
    back: list[list[tuple[int, int, str, int, int, float | None] | None]] = [
        [None] * (reference_count + 1) for _ in range(candidate_count + 1)
    ]
    dp[0][0] = 0.0
    candidate_skip_cost = 0.95
    reference_skip_cost = 0.70
    maximum_group = 3
    maximum_group_span = 18.0

    for candidate_index in range(candidate_count + 1):
        for reference_index in range(reference_count + 1):
            base = dp[candidate_index][reference_index]
            if base == infinity:
                continue
            if candidate_index < candidate_count:
                value = base + candidate_skip_cost
                if value < dp[candidate_index + 1][reference_index]:
                    dp[candidate_index + 1][reference_index] = value
                    back[candidate_index + 1][reference_index] = (
                        candidate_index,
                        reference_index,
                        "skip_candidate",
                        1,
                        0,
                        None,
                    )
            if reference_index < reference_count:
                value = base + reference_skip_cost
                if value < dp[candidate_index][reference_index + 1]:
                    dp[candidate_index][reference_index + 1] = value
                    back[candidate_index][reference_index + 1] = (
                        candidate_index,
                        reference_index,
                        "skip_reference",
                        0,
                        1,
                        None,
                    )

            for candidate_group_size in range(1, maximum_group + 1):
                if candidate_index + candidate_group_size > candidate_count:
                    break
                candidate_start, candidate_end = _cue_group_span(
                    candidate, candidate_index, candidate_group_size
                )
                candidate_duration = max(0.05, candidate_end - candidate_start)
                if candidate_duration > maximum_group_span:
                    break
                for reference_group_size in range(1, maximum_group + 1):
                    if reference_index + reference_group_size > reference_count:
                        break
                    reference_start, reference_end = _cue_group_span(
                        reference_items, reference_index, reference_group_size
                    )
                    reference_duration = max(0.05, reference_end - reference_start)
                    if reference_duration > maximum_group_span:
                        break
                    start_error = abs(candidate_start - reference_start)
                    end_error = abs(candidate_end - reference_end)
                    center_error = abs(
                        (candidate_start + candidate_end - reference_start - reference_end) / 2.0
                    )
                    duration_ratio = max(candidate_duration, reference_duration) / min(
                        candidate_duration, reference_duration
                    )
                    if (
                        start_error > 2.0
                        or end_error > 2.0
                        or center_error > 1.6
                        or duration_ratio > 2.6
                    ):
                        continue
                    cost = (
                        0.95 * start_error
                        + 0.75 * end_error
                        + 0.28 * abs(math.log(candidate_duration / reference_duration))
                        + 0.10 * (candidate_group_size + reference_group_size - 2)
                    )
                    value = base + cost
                    target_candidate = candidate_index + candidate_group_size
                    target_reference = reference_index + reference_group_size
                    if value < dp[target_candidate][target_reference]:
                        dp[target_candidate][target_reference] = value
                        back[target_candidate][target_reference] = (
                            candidate_index,
                            reference_index,
                            "match",
                            candidate_group_size,
                            reference_group_size,
                            cost,
                        )

    matches: list[tuple[int, int, int, int, float]] = []
    candidate_index = candidate_count
    reference_index = reference_count
    while candidate_index or reference_index:
        transition = back[candidate_index][reference_index]
        if transition is None:
            return aligned, _result("reference_groups_alignment_failed", applied=False)
        (
            previous_candidate,
            previous_reference,
            transition_type,
            candidate_group_size,
            reference_group_size,
            cost,
        ) = transition
        if transition_type == "match" and cost is not None:
            matches.append(
                (
                    previous_candidate,
                    previous_reference,
                    candidate_group_size,
                    reference_group_size,
                    float(cost),
                )
            )
        candidate_index = previous_candidate
        reference_index = previous_reference
    matches.reverse()

    replacements: dict[int, tuple[float, float]] = {}
    accepted_groups: list[dict[str, object]] = []
    before_boundary_errors: list[float] = []
    maximum_boundary_correction = 1.6
    for (
        candidate_index,
        reference_index,
        candidate_group_size,
        reference_group_size,
        cost,
    ) in matches:
        basis_start, basis_end = _cue_group_span(
            candidate, candidate_index, candidate_group_size
        )
        reference_start, reference_end = _cue_group_span(
            reference_items, reference_index, reference_group_size
        )
        basis_duration = max(0.05, basis_end - basis_start)
        reference_duration = max(0.05, reference_end - reference_start)
        scale = reference_duration / basis_duration
        if (
            cost > 1.25
            or abs(basis_start - reference_start) > maximum_boundary_correction
            or abs(basis_end - reference_end) > maximum_boundary_correction
            or not 0.60 <= scale <= 1.65
        ):
            continue

        original_indexes = [
            candidate_dialogue[index][0]
            for index in range(candidate_index, candidate_index + candidate_group_size)
        ]
        if any(index in replacements for index in original_indexes):
            continue

        # Cross-language split/merge groups reveal the common local clock, but
        # not the internal boundary between two Japanese lines inside one long
        # English cue.  The old implementation scaled every Japanese cue to the
        # English group span, changing durations and pauses even when the source
        # SRT already had good speech-level timing.  Use one shared translation
        # for the entire group instead; this preserves every Japanese duration
        # and internal gap exactly.
        current_group = [candidate_cues[index] for index in original_indexes]
        current_start = current_group[0][0]
        current_end = max(end for _start, end, _text in current_group)
        start_correction = reference_start - current_start
        end_correction = reference_end - current_end
        duration_disagreement = abs(start_correction - end_correction)
        split_merge = candidate_group_size != reference_group_size
        maximum_duration_disagreement = 0.45 if split_merge else 0.65
        shared_shift = (start_correction + end_correction) / 2.0
        if (
            abs(shared_shift) > maximum_boundary_correction
            or duration_disagreement > maximum_duration_disagreement
        ):
            continue

        group_replacements: dict[int, tuple[float, float]] = {}
        safe = True
        for original_index in original_indexes:
            source_start, source_end, _text = candidate_cues[original_index]
            target_start = source_start + shared_shift
            target_end = source_end + shared_shift
            if target_start < 0.0 or target_end <= target_start + 0.20:
                safe = False
                break
            group_replacements[original_index] = (target_start, target_end)
        if not safe:
            continue
        replacements.update(group_replacements)
        before_boundary_errors.extend([abs(start_correction), abs(end_correction)])
        accepted_groups.append(
            {
                "candidate_index": candidate_index,
                "reference_index": reference_index,
                "candidate_cues": candidate_group_size,
                "reference_cues": reference_group_size,
                "start_correction": round(start_correction, 3),
                "end_correction": round(end_correction, 3),
                "shared_shift": round(shared_shift, 3),
                "duration_disagreement": round(duration_disagreement, 3),
                "cost": round(cost, 4),
            }
        )

    matched_candidate_count = len(replacements)
    coverage = matched_candidate_count / max(1, len(candidate_dialogue))
    minimum_groups = max(8, int(round(len(candidate_dialogue) * 0.18)))
    mean_boundary_error = (
        statistics.fmean(before_boundary_errors) if before_boundary_errors else 0.0
    )
    if len(accepted_groups) < minimum_groups or coverage < 0.45:
        return aligned, _result(
            "reference_groups_insufficient_coverage",
            applied=False,
            matched_groups=len(accepted_groups),
            matched_candidate_cues=matched_candidate_count,
            dialogue_candidate_cues=len(candidate_dialogue),
            coverage=round(coverage, 4),
        )
    if mean_boundary_error < 0.08:
        return aligned, _result(
            "reference_groups_not_needed",
            applied=False,
            matched_groups=len(accepted_groups),
            coverage=round(coverage, 4),
            mean_boundary_error=round(mean_boundary_error, 4),
        )

    repaired: list[tuple[float, float, str]] = []
    adjusted_cues = 0
    for index, (start, end, text) in enumerate(candidate_cues):
        target_start, target_end = replacements.get(index, (start, end))
        target_start = max(0.02, target_start)
        target_end = max(target_start + 0.25, target_end)
        if abs(target_start - start) >= 0.01 or abs(target_end - end) >= 0.01:
            adjusted_cues += 1
        repaired.append((target_start, target_end, text))

    aligned_stat = aligned.stat()
    reference_stat = reference.stat()
    basis_path = matching_basis or aligned
    basis_stat = basis_path.stat()
    digest = hashlib.sha1(
        (
            f"reference-groups-v3:{aligned.resolve()}:{aligned_stat.st_size}:"
            f"{aligned_stat.st_mtime_ns}:{basis_path.resolve()}:{basis_stat.st_size}:"
            f"{basis_stat.st_mtime_ns}:{reference.resolve()}:{reference_stat.st_size}:"
            f"{reference_stat.st_mtime_ns}:{len(accepted_groups)}:{matched_candidate_count}"
        ).encode()
    ).hexdigest()[:20]
    output_dir = cache_dir / "reference-groups"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{digest}.srt"
    if force:
        output.unlink(missing_ok=True)
    if not output.exists() or output.stat().st_size <= 0:
        write_srt(repaired, output, preserve_order=True)

    # A language-agnostic activity score can improve even when individual cue
    # boundaries become visibly worse.  Guard both the timing score and the
    # original Japanese cue shape before accepting the refinement.
    before_activity = compare_timing_activity(aligned, reference)
    after_activity = compare_timing_activity(output, reference)
    output_cues = parse_srt(output)
    duration_changes: list[float] = []
    if len(output_cues) == len(candidate_cues):
        duration_changes = [
            abs((new_end - new_start) - (old_end - old_start))
            for (old_start, old_end, _old_text), (new_start, new_end, _new_text)
            in zip(candidate_cues, output_cues)
        ]
    large_duration_changes = sum(change > 0.15 for change in duration_changes)
    large_duration_change_ratio = (
        large_duration_changes / len(duration_changes) if duration_changes else 1.0
    )
    weighted_gain = (
        float(after_activity.get("weighted") or 0.0)
        - float(before_activity.get("weighted") or 0.0)
        if before_activity.get("available") and after_activity.get("available")
        else float("-inf")
    )
    start_gain = (
        float(after_activity.get("start") or 0.0)
        - float(before_activity.get("start") or 0.0)
        if before_activity.get("available") and after_activity.get("available")
        else float("-inf")
    )
    activity_improved = weighted_gain >= 0.001 or (
        start_gain >= 0.005 and weighted_gain >= -0.0005
    )
    structurally_safe = (
        len(output_cues) == len(candidate_cues)
        and large_duration_change_ratio <= 0.03
        and (max(duration_changes, default=0.0) <= 0.50)
    )
    accepted = activity_improved and structurally_safe

    if verbose:
        split_groups = sum(
            1
            for group in accepted_groups
            if group["candidate_cues"] != group["reference_cues"]
        )
        print(
            "  Групповая коррекция по встроенному эталону: "
            f"groups={len(accepted_groups)}, split/merge={split_groups}, "
            f"coverage={coverage:.3f}, adjusted={adjusted_cues}, "
            f"mean_boundary_error={mean_boundary_error:.3f}s, "
            f"weighted_gain={weighted_gain:+.4f}, "
            f"duration_changes>0.15s={large_duration_changes}, accepted={accepted}"
        )
    common_result = dict(
        matched_groups=len(accepted_groups),
        split_merge_groups=sum(
            1
            for group in accepted_groups
            if group["candidate_cues"] != group["reference_cues"]
        ),
        matched_candidate_cues=matched_candidate_count,
        dialogue_candidate_cues=len(candidate_dialogue),
        coverage=round(coverage, 4),
        adjusted_cues=adjusted_cues,
        mean_boundary_error_before=round(mean_boundary_error, 4),
        weighted_gain=round(weighted_gain, 4) if math.isfinite(weighted_gain) else None,
        start_gain=round(start_gain, 4) if math.isfinite(start_gain) else None,
        large_duration_changes=large_duration_changes,
        large_duration_change_ratio=round(large_duration_change_ratio, 4),
        before_activity=before_activity,
        after_activity=after_activity,
        groups=accepted_groups,
    )
    if not accepted:
        return aligned, _result(
            "reference_groups_no_safe_improvement",
            applied=False,
            **common_result,
        )
    return output, _result(
        "applied",
        applied=True,
        output=str(output),
        **common_result,
    )


def _validate_embedded_reference_output(
    source: Path,
    aligned: Path,
    reference: Path,
) -> tuple[bool, str, dict[str, object]]:
    """Basic structural gate for direct subtitle-to-subtitle ALASS output.

    The LLM has already established that source and reference describe the same
    episode. At this stage we only need to ensure ALASS produced a complete,
    non-corrupt retiming of the source rather than an empty/truncated file.
    """
    try:
        source_cues = parse_srt(source)
        aligned_cues = parse_srt(aligned)
        reference_cues = parse_srt(reference)
    except OSError as exc:
        return False, "read_error", {"error": str(exc)}
    if not source_cues:
        return False, "source_empty", {}
    if not aligned_cues:
        return False, "aligned_empty", {}
    if not reference_cues:
        return False, "reference_empty", {}

    source_count = len(source_cues)
    aligned_count = len(aligned_cues)
    retained_ratio = aligned_count / max(source_count, 1)
    aligned_end = max(end for _, end, _ in aligned_cues)
    reference_end = max(end for _, end, _ in reference_cues)
    source_end = max(end for _, end, _ in source_cues)
    details: dict[str, object] = {
        "source_cues": source_count,
        "aligned_cues": aligned_count,
        "retained_ratio": round(retained_ratio, 4),
        "source_end_seconds": round(source_end, 3),
        "aligned_end_seconds": round(aligned_end, 3),
        "reference_end_seconds": round(reference_end, 3),
    }

    # ALASS should preserve almost every source cue. A few malformed cues may be
    # dropped during SRT normalization, but losing a substantial fraction means
    # the result is unsafe.
    if retained_ratio < 0.9:
        return False, "too_many_cues_lost", details
    # The output should cover roughly the same episode duration as the exact
    # embedded reference. Keep generous margins for signs/songs and credits.
    if reference_end >= 60.0 and aligned_end < reference_end * 0.65:
        return False, "aligned_too_short", details
    # This gate validates ALASS output, not whether the source subtitle itself
    # contains long sign/SFX cues beyond the embedded dialogue reference. Reject
    # only when alignment materially *extends* the source as well as overshooting
    # the reference. Otherwise a valid movie ASS with ending/sign cues can be
    # falsely rejected even when ALASS applies an essentially zero shift.
    if aligned_end > reference_end + 180.0 and aligned_end > source_end + 30.0:
        return False, "aligned_too_long", details
    return True, "ok", details

def _embedded_reference_is_better(
    baseline: dict[str, object],
    aligned: dict[str, object],
) -> tuple[bool, str]:
    if not aligned.get("available"):
        return False, "aligned_activity_unavailable"
    if not baseline.get("available"):
        return True, "baseline_activity_unavailable"

    def value(payload: dict[str, object], key: str) -> float:
        try:
            return float(payload.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    baseline_start = value(baseline, "start")
    aligned_start = value(aligned, "start")
    baseline_middle = value(baseline, "middle")
    aligned_middle = value(aligned, "middle")
    baseline_weighted = value(baseline, "weighted")
    aligned_weighted = value(aligned, "weighted")

    # Never fix the cold open by materially breaking the main episode.
    if aligned_middle + 0.06 < baseline_middle:
        return False, "middle_degraded"
    # Prefer a clear cold-open improvement, or a clear whole-episode improvement.
    if aligned_start >= baseline_start + 0.035:
        return True, "start_improved"
    if aligned_weighted >= baseline_weighted + 0.025:
        return True, "weighted_improved"
    # If both are already very strong, the embedded track remains the safer clock.
    if (
        aligned_start >= 0.82
        and (aligned_middle >= 0.82 or value(aligned, "full") >= 0.88)
    ):
        return True, "strong_reference_alignment"
    return False, "no_material_improvement"


def _write_onset_pulses(
    starts: list[float],
    destination: Path,
    *,
    pulse_seconds: float,
    prefix: str,
) -> None:
    duration = max(0.08, pulse_seconds)
    cues = [
        (start, start + duration, f"{prefix}-{index:06d}")
        for index, start in enumerate(starts, start=1)
    ]
    write_srt(cues, destination)


def synchronize_pgs_with_embedded_reference(
    video: Path,
    subtitle: Path,
    cache_dir: Path,
    config: SyncConfig,
    *,
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
    alass_path: str = "alass",
    force: bool = False,
    verbose: bool = False,
) -> tuple[Path, dict[str, object]]:
    """Retime bitmap PGS by matching image-onset events to embedded text cues.

    No OCR is involved. Each visible PGS presentation becomes a short pulse, as
    does each embedded English subtitle start. ALASS aligns those pulse trains;
    the resulting monotonic time map is then written back into raw SUP PTS/DTS.
    """
    if subtitle.suffix.casefold() != ".sup":
        return subtitle, _result("pgs_unsupported_format", sync_was_successful=False)
    if not config.pgs_onset_alignment:
        return subtitle, _result("pgs_onset_disabled", sync_was_successful=False)

    reference, reference_result = extract_embedded_timing_reference(
        video,
        cache_dir,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        force=force,
        verbose=verbose,
    )
    if reference is None:
        return subtitle, _result(
            "pgs_reference_not_found",
            sync_was_successful=False,
            reference_reason=reference_result.get("reason"),
            error=reference_result.get("error"),
        )

    try:
        source_cues = parse_pgs_cues(subtitle)
        source_starts = onset_times(source_cues)
        reference_cues = parse_srt(reference)
        reference_starts = sorted({round(start, 3) for start, _, _ in reference_cues})
    except (OSError, ValueError) as exc:
        return subtitle, _result("pgs_parse_error", sync_was_successful=False, error=str(exc))

    if len(source_starts) < 8 or len(reference_starts) < 8:
        return subtitle, _result(
            "pgs_too_few_events",
            sync_was_successful=False,
            pgs_events=len(source_starts),
            reference_events=len(reference_starts),
        )

    source_stat = subtitle.stat()
    reference_stat = reference.stat()
    raw = (
        f"pgs-onset-v1:{subtitle.resolve()}:{source_stat.st_size}:{source_stat.st_mtime_ns}:"
        f"{reference.resolve()}:{reference_stat.st_size}:{reference_stat.st_mtime_ns}:"
        f"{config.pgs_onset_pulse_seconds}:{config.pgs_onset_tolerance_seconds}:"
        f"{config.pgs_onset_min_improvement}:{config.alass_split_penalty}"
    )
    digest = hashlib.sha1(raw.encode()).hexdigest()[:20]
    output_dir = cache_dir / "pgs-onset"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{digest}.sup"
    if force:
        output.unlink(missing_ok=True)
        _metadata_path(output).unlink(missing_ok=True)
    if output.exists() and output.stat().st_size > 0:
        cached = _read_metadata(output)
        return output, _result(
            "cached",
            cached=True,
            sync_was_successful=True,
            engine="pgs-onset+alass",
            output=str(output),
            **{
                key: value
                for key, value in cached.items()
                if key not in {"reason", "cached", "sync_was_successful", "output", "engine"}
            },
        )

    source_pulses = output_dir / f"{digest}.source.srt"
    reference_pulses = output_dir / f"{digest}.reference.srt"
    _write_onset_pulses(
        source_starts,
        source_pulses,
        pulse_seconds=config.pgs_onset_pulse_seconds,
        prefix="PGS",
    )
    _write_onset_pulses(
        reference_starts,
        reference_pulses,
        pulse_seconds=config.pgs_onset_pulse_seconds,
        prefix="REF",
    )

    aligned_pulses, alass_result = synchronize_with_alass(
        reference_pulses,
        source_pulses,
        cache_dir,
        config,
        alass_path=alass_path,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        force=force,
        verbose=verbose,
    )
    if not bool(alass_result.get("sync_was_successful")):
        return subtitle, _result(
            "pgs_onset_alass_failed",
            sync_was_successful=False,
            error=alass_result.get("error"),
            alass_reason=alass_result.get("reason"),
        )

    aligned_cues = parse_srt(aligned_pulses)
    if len(aligned_cues) != len(source_starts):
        return subtitle, _result(
            "pgs_onset_structure_changed",
            sync_was_successful=False,
            source_events=len(source_starts),
            aligned_events=len(aligned_cues),
        )

    aligned_starts = [start for start, _, _ in aligned_cues]
    knots = list(zip(source_starts, aligned_starts))
    mapper = build_time_mapper(knots)
    try:
        retime_sup(subtitle, output, mapper)
        output_starts = onset_times(parse_pgs_cues(output))
    except (OSError, ValueError) as exc:
        output.unlink(missing_ok=True)
        return subtitle, _result("pgs_retime_error", sync_was_successful=False, error=str(exc))

    before = onset_match_score(
        source_starts,
        reference_starts,
        tolerance=config.pgs_onset_tolerance_seconds,
    )
    after = onset_match_score(
        output_starts,
        reference_starts,
        tolerance=config.pgs_onset_tolerance_seconds,
    )
    before_coverage = float(before["coverage"])
    after_coverage = float(after["coverage"])
    improvement = after_coverage - before_coverage
    accepted = (
        after_coverage >= 0.55
        or improvement >= config.pgs_onset_min_improvement
    ) and after_coverage >= before_coverage
    if not accepted:
        output.unlink(missing_ok=True)
        return subtitle, _result(
            "pgs_onset_no_improvement",
            sync_was_successful=False,
            onset_before=before,
            onset_after=after,
            onset_improvement=round(improvement, 4),
        )

    shifts = [target - source for source, target in knots]
    median_shift = statistics.median(shifts) if shifts else 0.0
    result = _result(
        "applied",
        sync_was_successful=True,
        engine="pgs-onset+alass",
        output=str(output),
        offset_seconds=round(median_shift, 3),
        framerate_scale_factor=1.0,
        onset_before=before,
        onset_after=after,
        onset_improvement=round(improvement, 4),
        pgs_events=len(source_starts),
        reference_events=len(reference_starts),
        mapping_knots=len(knots),
        timing_reference=str(reference),
        timing_reference_language=reference_result.get("language"),
        timing_reference_title=reference_result.get("title"),
        selection_reason="pgs_onset_reference",
    )
    _write_metadata(output, result)
    return output, result


def _try_japanese_stt_fallback(
    video: Path,
    subtitle: Path,
    cache_dir: Path,
    config: SyncConfig,
    *,
    ffmpeg_path: str,
    ffprobe_path: str,
    alass_path: str,
    verbose: bool,
) -> tuple[Path | None, dict[str, object]]:
    if not config.japanese_stt_fallback:
        return None, _result("stt_disabled", sync_was_successful=False)
    reference, stt = prepare_japanese_stt_reference(
        video,
        cache_dir,
        ffmpeg_path=ffmpeg_path,
        model=config.japanese_stt_model,
        timeout_seconds=config.japanese_stt_timeout_seconds,
        # Transcription is content-addressed and expensive; force-searching
        # subtitle providers must not invalidate the speech cache.
        force=False,
    )
    if reference is None:
        return None, _result("stt_reference_unavailable", sync_was_successful=False, stt=stt)
    aligned, result = synchronize_with_alass(
        reference,
        subtitle,
        cache_dir,
        config,
        alass_path=alass_path,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        force=False,
        verbose=verbose,
    )
    if not bool(result.get("sync_was_successful")):
        result["stt"] = stt
        result["reason"] = "stt_alass_failed"
        return None, result
    activity = compare_timing_activity(aligned, reference)
    try:
        score = float(activity.get("weighted") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    if not bool(activity.get("available")) or score < config.japanese_stt_min_activity:
        result.update(
            {
                "sync_was_successful": False,
                "reason": "stt_activity_gate_failed",
                "stt": stt,
                "reference_activity": activity,
            }
        )
        return None, result
    result.update(
        {
            "engine": "japanese-stt+alass",
            "reason": "applied",
            "selection_reason": "last_resort_cached_stt",
            "timing_reference": str(reference),
            "timing_reference_language": "ja",
            "reference_activity": activity,
            "reference_activity_score": score,
            "reference_alignment_reliable": True,
            "stt": stt,
        }
    )
    return aligned, result


def optimize_subtitle(
    video: Path,
    subtitle: Path,
    cache_dir: Path,
    config: SyncConfig,
    *,
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
    alass_path: str = "alass",
    force: bool = False,
    verbose: bool = False,
    reference: Path | None = None,
    llm: OllamaClient | None = None,
    validate_embedded_reference_with_llm: bool = False,
) -> tuple[Path, dict[str, object]]:
    """Choose a safe timing strategy and never trust ambiguous local FFT peaks."""
    container_edit_points = (
        probe_container_edit_points(video, ffprobe_path)
        if config.use_container_chapters
        else []
    )
    if subtitle.suffix.casefold() == ".sup":
        if not config.enabled:
            return subtitle, _result("disabled", sync_was_successful=False)
        return synchronize_pgs_with_embedded_reference(
            video,
            subtitle,
            cache_dir,
            config,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            alass_path=alass_path,
            force=force,
            verbose=verbose,
        )

    engine = config.engine.casefold().strip()
    if engine not in {"auto", "ffsubsync", "alass"}:
        engine = "auto"

    successful: list[tuple[Path, dict[str, object]]] = []
    failed: list[dict[str, object]] = []
    rough_alignment_path: Path | None = None
    timing_reference_validation: dict[str, object] | None = None

    timing_reference: Path | None = None
    timing_reference_result: dict[str, object] = {}
    if engine in {"auto", "alass"}:
        timing_reference, timing_reference_result = extract_embedded_timing_reference(
            video,
            cache_dir,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            force=force,
            verbose=verbose,
        )
        if verbose and timing_reference is not None:
            language = timing_reference_result.get("language") or "-"
            title = timing_reference_result.get("title") or "-"
            print(f"  Эталон тайминга: встроенная дорожка {language}, {title}")

    prealigned_reference_path: Path | None = None
    prealigned_reference_result: dict[str, object] | None = None

    if timing_reference is not None and validate_embedded_reference_with_llm:
        if llm is None:
            timing_reference_validation = {
                "accepted": False,
                "reason": "llm_unavailable",
                "similarity": 0.0,
                "alignment_mode": "alass-timestamp",
            }
            timing_reference = None
        else:
            # Do not guess a constant offset before semantic verification. ALASS
            # already solves subtitle-to-subtitle timing from the complete onset
            # structure, including cue splits and local cuts. Validate its actual
            # aligned output semantically, then use audio/FFT only as a fallback.
            semantic_source = subtitle
            if semantic_source.suffix.casefold() in {".ass", ".ssa"}:
                semantic_source, conversion = convert_to_plain_srt(
                    semantic_source,
                    cache_dir,
                    ffmpeg_path=ffmpeg_path,
                    force=force,
                    verbose=verbose,
                )
                if semantic_source.suffix.casefold() != ".srt":
                    timing_reference_validation = {
                        "accepted": False,
                        "reason": "semantic_conversion_failed",
                        "similarity": 0.0,
                        "conversion_reason": conversion.get("reason"),
                        "alignment_mode": "alass-timestamp",
                    }
                    timing_reference = None

            if timing_reference is not None:
                direct_path, direct_result = synchronize_with_alass(
                    timing_reference,
                    semantic_source,
                    cache_dir,
                    config,
                    alass_path=alass_path,
                    ffmpeg_path=ffmpeg_path,
                    ffprobe_path=ffprobe_path,
                    force=force,
                    verbose=verbose,
                )
                if bool(direct_result.get("sync_was_successful")):
                    aligned_semantic = direct_path
                    if direct_result.get("offset_seconds") is None:
                        direct_result.update(_subtitle_shift_summary(semantic_source, aligned_semantic))
                    if aligned_semantic.suffix.casefold() in {".ass", ".ssa"}:
                        aligned_semantic, conversion = convert_to_plain_srt(
                            aligned_semantic,
                            cache_dir,
                            ffmpeg_path=ffmpeg_path,
                            force=force,
                            verbose=verbose,
                        )
                        direct_result["plain_srt_conversion"] = conversion.get("reason")

                    structure_ok, structure_reason, structure = _validate_embedded_reference_output(
                        semantic_source,
                        aligned_semantic,
                        timing_reference,
                    )
                    activity = compare_timing_activity(aligned_semantic, timing_reference)
                    if verbose:
                        offset = direct_result.get("offset_seconds")
                        constant_shift = direct_result.get("alass_constant_shift")
                        if isinstance(offset, (int, float)) and constant_shift is not False:
                            offset_text = f"{float(offset):+.2f}s"
                        elif isinstance(offset, (int, float)):
                            offset_text = f"non-linear (median={float(offset):+.2f}s)"
                        else:
                            offset_text = "non-linear"
                        print(
                            "  Прямое сопоставление субтитров через ALASS: "
                            f"offset={offset_text}, структура={structure_reason}, "
                            f"cues={structure.get('aligned_cues', '-')} / "
                            f"{structure.get('source_cues', '-')}, "
                            f"activity={activity.get('weighted', '-')}"
                        )

                    validation = llm.compare_subtitle_semantics(
                        aligned_semantic,
                        timing_reference,
                        alignment_mode="timestamp",
                        force=force,
                    )
                    validation["alignment_mode"] = "alass-timestamp"
                    validation["estimated_offset_seconds"] = direct_result.get("offset_seconds")
                    validation["alass_constant_shift"] = direct_result.get("alass_constant_shift")
                    validation["alass_shift_spread_seconds"] = direct_result.get(
                        "alass_shift_spread_seconds"
                    )
                    validation["reference_output_structure"] = structure
                    validation["reference_activity"] = activity
                    validation["structure_reason"] = structure_reason
                    validation = _apply_robust_semantic_activity_gate(validation, activity)

                    accepted = bool(validation.get("accepted")) and structure_ok
                    validation["accepted"] = accepted
                    if not structure_ok:
                        validation["reason"] = f"invalid_alass_structure:{structure_reason}"
                    timing_reference_validation = validation

                    if accepted:
                        repaired_path, repair_result = repair_with_embedded_reference_piecewise(
                            aligned_semantic,
                            timing_reference,
                            cache_dir,
                            config,
                            llm=llm,
                            edit_points=container_edit_points,
                            force=force,
                            verbose=verbose,
                        )
                        if repair_result.get("applied"):
                            aligned_semantic = repaired_path
                            activity = compare_timing_activity(aligned_semantic, timing_reference)
                            validation["reference_activity"] = activity
                        prealigned_reference_path = aligned_semantic
                        prealigned_reference_result = dict(direct_result)
                        prealigned_reference_result["engine"] = "embedded-reference+alass"
                        if repair_result.get("applied"):
                            prealigned_reference_result["reference_piecewise_repair"] = repair_result
                            prealigned_reference_result["output"] = str(aligned_semantic)
                    else:
                        # ALASS can overfit different broadcast/streaming masters.  Before
                        # falling back to audio VAD/FFT, try the constant-offset hypotheses
                        # already derived from subtitle onset structure.  The LLM validates
                        # each shifted hypothesis at its *actual* timestamps, so a correct
                        # global clock can recover even when a non-linear ALASS map sampled
                        # the wrong scenes (BLEACH absolute E43 / DSNP vs TVA).
                        offset_attempts: list[dict[str, object]] = []
                        accepted_offset_candidates: list[
                            tuple[
                                tuple[float, float, float, float, float],
                                Path,
                                dict[str, object],
                                dict[str, object],
                                float,
                            ]
                        ] = []
                        recovered = False
                        for estimate in estimate_constant_subtitle_offsets(
                            semantic_source,
                            timing_reference,
                            max_offset_seconds=config.max_offset_seconds,
                            maximum_results=6,
                        ):
                            if not bool(estimate.get("available")) or not bool(
                                estimate.get("usable_for_semantic_sampling")
                            ):
                                continue
                            try:
                                candidate_offset = float(estimate.get("offset_seconds"))
                            except (TypeError, ValueError):
                                continue
                            shifted_semantic = _shift_subtitle_for_semantic_validation(
                                semantic_source,
                                cache_dir,
                                candidate_offset,
                                force=force,
                            )
                            shifted_activity = compare_timing_activity(
                                shifted_semantic,
                                timing_reference,
                            )
                            shifted_validation = llm.compare_subtitle_semantics(
                                shifted_semantic,
                                timing_reference,
                                alignment_mode="timestamp",
                                force=force,
                            )
                            shifted_validation["alignment_mode"] = "constant-offset-timestamp"
                            shifted_validation["estimated_offset_seconds"] = round(
                                candidate_offset, 3
                            )
                            shifted_validation["constant_offset_estimate"] = dict(estimate)
                            shifted_validation["reference_activity"] = shifted_activity
                            shifted_validation = _apply_robust_semantic_activity_gate(
                                shifted_validation, shifted_activity
                            )
                            structure_ok, structure_reason, shifted_structure = (
                                _validate_embedded_reference_output(
                                    semantic_source,
                                    shifted_semantic,
                                    timing_reference,
                                )
                            )
                            shifted_validation["structure_reason"] = structure_reason
                            shifted_validation["reference_output_structure"] = shifted_structure
                            shifted_accepted = bool(shifted_validation.get("accepted")) and structure_ok
                            shifted_validation["accepted"] = shifted_accepted
                            offset_attempts.append(
                                {
                                    "offset_seconds": round(candidate_offset, 3),
                                    "accepted": shifted_accepted,
                                    "similarity": shifted_validation.get("similarity"),
                                    "matched_samples": shifted_validation.get("matched_samples"),
                                    "total_samples": shifted_validation.get("total_samples"),
                                    "activity": shifted_activity.get("weighted"),
                                    "estimate": dict(estimate),
                                }
                            )
                            if verbose:
                                state = "принят" if shifted_accepted else "отклонён"
                                print(
                                    "  Проверка constant-offset эталона: "
                                    f"offset={candidate_offset:+.2f}s, {state}, "
                                    f"activity={shifted_activity.get('weighted', '-')}, "
                                    f"phrases={shifted_validation.get('matched_samples', '-')}/"
                                    f"{shifted_validation.get('total_samples', '-')}"
                                )
                            if not shifted_accepted:
                                continue

                            try:
                                semantic_matches = float(
                                    shifted_validation.get("matched_samples") or 0
                                )
                                semantic_total = max(
                                    1.0, float(shifted_validation.get("total_samples") or 1)
                                )
                                semantic_similarity = float(
                                    shifted_validation.get("similarity") or 0.0
                                )
                                activity_score = float(
                                    shifted_activity.get("weighted") or 0.0
                                )
                                estimate_priority = float(
                                    estimate.get("semantic_priority") or 0.0
                                )
                            except (TypeError, ValueError):
                                semantic_matches = 0.0
                                semantic_total = 1.0
                                semantic_similarity = 0.0
                                activity_score = 0.0
                                estimate_priority = 0.0
                            accepted_offset_candidates.append(
                                (
                                    (
                                        semantic_matches / semantic_total,
                                        semantic_similarity,
                                        activity_score,
                                        estimate_priority,
                                        -abs(candidate_offset),
                                    ),
                                    shifted_semantic,
                                    dict(shifted_validation),
                                    dict(estimate),
                                    candidate_offset,
                                )
                            )

                        if accepted_offset_candidates:
                            (
                                _best_rank,
                                best_shifted_path,
                                best_validation,
                                best_estimate,
                                best_offset,
                            ) = max(accepted_offset_candidates, key=lambda item: item[0])
                            best_validation["constant_offset_attempts"] = list(offset_attempts)

                            repaired_path, repair_result = repair_with_embedded_reference_piecewise(
                                best_shifted_path,
                                timing_reference,
                                cache_dir,
                                config,
                                llm=llm,
                                edit_points=container_edit_points,
                                force=force,
                                verbose=verbose,
                            )
                            final_reference_path = (
                                repaired_path
                                if repair_result.get("applied")
                                else best_shifted_path
                            )

                            best_validation["reference_piecewise_repair"] = repair_result
                            best_validation["reference_activity"] = compare_timing_activity(
                                final_reference_path,
                                timing_reference,
                            )
                            timing_reference_validation = best_validation
                            prealigned_reference_path = final_reference_path

                            prealigned_reference_result = _result(
                                "applied",
                                sync_was_successful=True,
                                engine="embedded-reference+constant-offset",
                                output=str(final_reference_path),
                                offset_seconds=round(best_offset, 3),
                                framerate_scale_factor=1.0,
                                constant_offset_estimate=best_estimate,
                            )
                            if repair_result.get("applied"):
                                prealigned_reference_result["reference_piecewise_repair"] = repair_result
                            recovered = True
                            if verbose:
                                print(
                                    "  Выбран лучший constant-offset эталон: "
                                    f"offset={best_offset:+.2f}s, "
                                    f"similarity={best_validation.get('similarity', '-')}, "
                                    f"phrases={best_validation.get('matched_samples', '-')}/"
                                    f"{best_validation.get('total_samples', '-')}, "
                                    f"activity={best_validation.get('reference_activity', {}).get('weighted', '-')}"
                                )

                        if not recovered:
                            if offset_attempts:
                                validation["constant_offset_attempts"] = offset_attempts
                                timing_reference_validation = validation
                            timing_reference = None
                else:
                    timing_reference_validation = {
                        "accepted": False,
                        "reason": direct_result.get("reason") or "direct_alass_failed",
                        "similarity": 0.0,
                        "alignment_mode": "alass-timestamp",
                        "alass_error": direct_result.get("error"),
                    }
                    timing_reference = None

        if verbose:
            validation = timing_reference_validation or {}
            state = "принят" if validation.get("accepted") else "отклонён"
            print(
                "  LLM-проверка встроенного эталона: "
                f"{state}, mode={validation.get('alignment_mode', '-')}, "
                f"offset={validation.get('estimated_offset_seconds', '-')}, "
                f"similarity={validation.get('similarity', '-')}, "
                f"phrases={validation.get('matched_samples', '-')}/"
                f"{validation.get('total_samples', '-')}, "
                f"reason={validation.get('reason', '-')}"
            )

    if engine in {"auto", "alass"}:
        alass_reference = timing_reference or video
        if timing_reference is not None and prealigned_reference_path is not None:
            alass_out = prealigned_reference_path
            alass_result = dict(prealigned_reference_result or {})
            alass_result.setdefault("reason", "applied")
            alass_result.setdefault("sync_was_successful", True)
            alass_result.setdefault("engine", "alass")
            alass_result.setdefault("output", str(alass_out))
        else:
            timeline_source = subtitle
            if (
                timing_reference is not None
                and timeline_source.suffix.casefold() in {".ass", ".ssa"}
            ):
                timeline_source, _timeline_conversion = convert_to_plain_srt(
                    timeline_source,
                    cache_dir,
                    ffmpeg_path=ffmpeg_path,
                    force=force,
                    verbose=verbose,
                )

            if timing_reference is not None and timeline_source.suffix.casefold() == ".srt":
                alass_out, alass_result = align_subtitle_timelines(
                    timeline_source,
                    timing_reference,
                    cache_dir,
                    max_offset_seconds=config.max_offset_seconds,
                    force=force,
                )
                timeline_attempt = dict(alass_result)
                _record_timeline_debug_attempt(
                    cache_dir, video, timeline_source, timing_reference, timeline_attempt,
                    stage="synchronize_subtitle",
                )
            else:
                alass_out, alass_result = (
                    timeline_source,
                    {
                        "reason": "timeline_reference_unavailable",
                        "sync_was_successful": False,
                    },
                )

            if not bool(alass_result.get("timeline_alignment_reliable")):
                timeline_attempt = dict(alass_result)
                alass_out, alass_result = synchronize_with_alass(
                    alass_reference,
                    subtitle,
                    cache_dir,
                    config,
                    alass_path=alass_path,
                    ffmpeg_path=ffmpeg_path,
                    ffprobe_path=ffprobe_path,
                    force=force,
                    verbose=verbose,
                )
                alass_result["timeline_alignment_attempt"] = timeline_attempt
            elif verbose:
                print(
                    "  Subtitle-only timeline alignment принят до ALASS: "
                    f"{alass_result.get('timeline_segments', [])}"
                )
        if bool(alass_result.get("sync_was_successful")):
            base_engine = (
                str(alass_result.get("engine") or "embedded-reference+alass")
                if timing_reference is not None
                else "alass"
            )
            if (
                timing_reference is not None
                and isinstance(alass_result.get("reference_piecewise_repair"), dict)
                and alass_result["reference_piecewise_repair"].get("applied")
            ):
                suffix = _reference_repair_engine_suffix(
                    alass_result["reference_piecewise_repair"]
                )
                if suffix not in base_engine:
                    base_engine += suffix
            alass_result["engine"] = base_engine
            if timing_reference is not None:
                alass_result["timing_reference"] = str(timing_reference)
                alass_result["timing_reference_language"] = timing_reference_result.get("language")
                alass_result["timing_reference_title"] = timing_reference_result.get("title")
                if timing_reference_validation is not None:
                    alass_result["timing_reference_validation"] = timing_reference_validation

                # The reference track comes from the exact video being played.
                # A structurally valid direct ALASS result is therefore the preferred
                # clock; deterministic local windows and chapter cuts can refine it
                # without waking the LLM or letting global audio override the cold open.
                activity_path = alass_out
                if activity_path.suffix.casefold() in {".ass", ".ssa"}:
                    activity_path, conversion = convert_to_plain_srt(
                        activity_path,
                        cache_dir,
                        ffmpeg_path=ffmpeg_path,
                        force=force,
                        verbose=verbose,
                    )
                    alass_result["plain_srt_conversion"] = conversion.get("reason")

                source_for_gate = subtitle
                if source_for_gate.suffix.casefold() in {".ass", ".ssa"}:
                    source_for_gate, _ = convert_to_plain_srt(
                        source_for_gate,
                        cache_dir,
                        ffmpeg_path=ffmpeg_path,
                        force=force,
                        verbose=verbose,
                    )
                reference_ok, reference_reason, structure = _validate_embedded_reference_output(
                    source_for_gate,
                    activity_path,
                    timing_reference,
                )
                baseline_activity = (
                    compare_timing_activity(rough_alignment_path, timing_reference)
                    if rough_alignment_path is not None
                    else {"available": False, "reason": "baseline_unavailable"}
                )
                aligned_activity = compare_timing_activity(activity_path, timing_reference)
                if (
                    reference_ok
                    and config.piecewise_repair
                    and not bool(alass_result.get("timeline_alignment_reliable"))
                    and not isinstance(alass_result.get("reference_piecewise_repair"), dict)
                ):
                    repaired_path, repair_result = repair_with_embedded_reference_piecewise(
                        activity_path,
                        timing_reference,
                        cache_dir,
                        config,
                        llm=None,
                        edit_points=container_edit_points,
                        force=force,
                        verbose=verbose,
                    )
                    alass_result["reference_piecewise_repair"] = repair_result
                    if repair_result.get("applied"):
                        activity_path = repaired_path
                        reference_ok, reference_reason, structure = (
                            _validate_embedded_reference_output(
                                source_for_gate,
                                activity_path,
                                timing_reference,
                            )
                        )
                        aligned_activity = compare_timing_activity(
                            activity_path,
                            timing_reference,
                        )
                        suffix = _reference_repair_engine_suffix(repair_result)
                        if suffix not in str(alass_result.get("engine") or ""):
                            alass_result["engine"] = str(alass_result.get("engine") or "alass") + suffix
                alass_result["baseline_reference_activity"] = baseline_activity
                alass_result["reference_activity"] = aligned_activity
                reference_ok, reference_reason, discontinuity_gate = (
                    _gate_embedded_reference_alass_discontinuity(
                        alass_result,
                        aligned_activity,
                        reference_ok=reference_ok,
                        reference_reason=reference_reason,
                    )
                )
                alass_result["reference_discontinuity_gate"] = discontinuity_gate
                if (
                    not reference_ok
                    and discontinuity_gate.get("reason")
                    == "extreme_unconfirmed_alass_discontinuity"
                ):
                    alass_result["reference_discontinuity_rejected"] = True
                alass_result["reference_activity_reason"] = reference_reason
                alass_result["reference_output_structure"] = structure
                alass_result["reference_alignment_reliable"] = reference_ok

                if verbose:
                    print(
                        "  Проверка прямого ALASS по английскому эталону: "
                        f"структура={reference_reason}, cues="
                        f"{structure.get('aligned_cues', '-')} / {structure.get('source_cues', '-')}, "
                        f"start={aligned_activity.get('start', '-')}, "
                        f"middle={aligned_activity.get('middle', '-')}"
                    )

                if reference_ok:
                    alass_out = activity_path
                    alass_result["selection_reason"] = "embedded_timing_reference_direct"
                    try:
                        alass_result["reference_activity_score"] = float(
                            aligned_activity.get("weighted") or 0.0
                        )
                    except (TypeError, ValueError):
                        pass
                    successful.append((alass_out, alass_result))
                else:
                    alass_result["reason"] = "embedded_reference_output_invalid"
                    alass_result["sync_was_successful"] = False
                    failed.append(alass_result)
            else:
                # Video/audio ALASS remains a fallback. A small ffsubsync finish
                # may remove a residual constant offset, but is never used for an
                # accepted embedded subtitle reference.
                finishing_config = replace(
                    config,
                    fix_framerate=False,
                    gss=False,
                    max_offset_seconds=min(config.max_offset_seconds, 20.0),
                    quality_max_offset_seconds=min(config.quality_max_offset_seconds, 20.0),
                    segment_validation=False,
                    piecewise_repair=False,
                )
                finished_path, finished_result = synchronize_subtitle(
                    video,
                    alass_out,
                    cache_dir,
                    finishing_config,
                    ffmpeg_path=ffmpeg_path,
                    force=force,
                    verbose=verbose,
                    tag="alass-finish",
                    reference=reference,
                )
                if bool(finished_result.get("sync_was_successful")):
                    preserved = {
                        "alass_blocks": alass_result.get("alass_blocks"),
                        "alass_distinct_shifts": alass_result.get("alass_distinct_shifts"),
                    }
                    alass_out = finished_path
                    alass_result.update(finished_result)
                    alass_result.update(preserved)
                    alass_result["engine"] = "alass+ffsubsync"
                successful.append((alass_out, alass_result))
        else:
            failed.append(alass_result)
            if timing_reference is not None:
                failure_reason = str(alass_result.get("reason") or "alass_error")
                failure_error = str(alass_result.get("error") or "")
                if verbose:
                    detail = f": {failure_error}" if failure_error else ""
                    print(
                        "  Выравнивание по английскому эталону не выполнено "
                        f"({failure_reason}){detail}"
                    )
                for _, existing_result in successful:
                    existing_result["embedded_reference_failure_reason"] = failure_reason
                    if failure_error:
                        existing_result["embedded_reference_failure_error"] = failure_error

        if engine == "alass":
            if not successful:
                return subtitle, alass_result
            path, result = successful[-1]
            if result.get("reference_alignment_reliable"):
                return path, result
            return _maybe_repair_piecewise(
                video,
                path,
                result,
                cache_dir,
                config,
                ffmpeg_path=ffmpeg_path,
                ffprobe_path=ffprobe_path,
                force=force,
                verbose=verbose,
                source_path=subtitle,
            )

    # Audio/FFT is a fallback. Prefer an embedded text subtitle clock:
    # onset matching -> semantic verification -> direct ALASS.
    reference_aligned_already = any(
        bool(item_result.get("reference_alignment_reliable"))
        for _item_path, item_result in successful
    )

    discontinuity_rejection = _best_reference_discontinuity_rejection(
        [(Path(str(item.get("output") or subtitle)), item) for item in failed]
    )
    if discontinuity_rejection is not None and not reference_aligned_already:
        rejected_path, rejected_result = discontinuity_rejection
        rejected_result["selection_reason"] = (
            "embedded_reference_discontinuity_rejected_before_audio"
        )
        rejected_result["audio_fallback_skipped"] = True
        rejected_result["audio_fallback_skip_reason"] = (
            "embedded_text_reference_is_stronger_than_audio_for_catastrophic_discontinuity"
        )
        if verbose:
            gate = rejected_result.get("reference_discontinuity_gate")
            gate_data = gate if isinstance(gate, dict) else {}
            print(
                "  Audio/ffsubsync fallback пропущен: "
                "встроенная текстовая дорожка уже отклонила большой ALASS-скачок "
                f"(spread={gate_data.get('spread_seconds', '?')}s)"
            )
        return rejected_path, rejected_result

    if engine in {"auto", "ffsubsync"} and not reference_aligned_already:
        ff_path, ff_result = synchronize_subtitle(
            video,
            subtitle,
            cache_dir,
            config,
            ffmpeg_path=ffmpeg_path,
            force=force,
            verbose=verbose,
            tag="ffsubsync",
            reference=reference,
        )
        ff_result["engine"] = "ffsubsync"
        if (
            timing_reference_validation
            and not timing_reference_validation.get("accepted")
            and timing_reference_validation.get("alignment_mode") == "alass-timestamp"
            and timing_reference_validation.get("reason")
            in {"alass_error", "alass_timeout", "alass_missing", "direct_alass_failed"}
        ):
            ff_result["embedded_reference_failure_reason"] = timing_reference_validation.get("reason")
            if timing_reference_validation.get("alass_error"):
                ff_result["embedded_reference_failure_error"] = timing_reference_validation.get("alass_error")
        if timing_reference_validation and timing_reference_validation.get("accepted"):
            reference_failure = next(
                (
                    item
                    for item in reversed(failed)
                    if str(item.get("reason") or "").startswith("alass")
                    or item.get("reason") == "embedded_reference_output_invalid"
                ),
                None,
            )
            if reference_failure is not None:
                ff_result["embedded_reference_failure_reason"] = reference_failure.get("reason")
                if reference_failure.get("error"):
                    ff_result["embedded_reference_failure_error"] = reference_failure.get("error")
        if bool(ff_result.get("sync_was_successful")):
            rough_alignment_path = ff_path
            successful.append((ff_path, ff_result))
        else:
            failed.append(ff_result)
        if engine == "ffsubsync" or not config.compare_engines:
            if bool(ff_result.get("sync_was_successful")):
                return _maybe_repair_piecewise(
                    video,
                    ff_path,
                    ff_result,
                    cache_dir,
                    config,
                    ffmpeg_path=ffmpeg_path,
                    ffprobe_path=ffprobe_path,
                    force=force,
                    verbose=verbose,
                    source_path=subtitle,
                )
            if not successful:
                if engine == "auto":
                    stt_path, stt_result = _try_japanese_stt_fallback(
                        video,
                        subtitle,
                        cache_dir,
                        config,
                        ffmpeg_path=ffmpeg_path,
                        ffprobe_path=ffprobe_path,
                        alass_path=alass_path,
                        verbose=verbose,
                    )
                    if stt_path is not None:
                        return stt_path, stt_result
                    ff_result["stt_fallback"] = stt_result
                return subtitle, ff_result
            path, result = successful[-1]
            return _maybe_repair_piecewise(
                video,
                path,
                result,
                cache_dir,
                config,
                ffmpeg_path=ffmpeg_path,
                ffprobe_path=ffprobe_path,
                force=force,
                verbose=verbose,
                source_path=subtitle,
            )

    if timing_reference_validation is not None:
        for _, item_result in successful:
            item_result.setdefault("timing_reference_validation", timing_reference_validation)
        for item_result in failed:
            item_result.setdefault("timing_reference_validation", timing_reference_validation)

    if not successful:
        if engine == "auto":
            stt_path, stt_result = _try_japanese_stt_fallback(
                video,
                subtitle,
                cache_dir,
                config,
                ffmpeg_path=ffmpeg_path,
                ffprobe_path=ffprobe_path,
                alass_path=alass_path,
                verbose=verbose,
            )
            if stt_path is not None:
                return stt_path, stt_result
            if failed:
                failed[0]["stt_fallback"] = stt_result
        return subtitle, failed[0] if failed else _result("alignment_failed", sync_was_successful=False)

    # A text subtitle embedded in the same video is a stronger timing reference
    # than audio VAD. If ALASS aligns to it and the residual offset is small, use
    # that result directly and do not run experimental audio-piecewise repair.
    reference_aligned = [
        item for item in successful if item[1].get("reference_alignment_reliable")
    ]
    if reference_aligned:
        best_path, best_result = max(
            reference_aligned,
            key=lambda item: float(item[1].get("alignment_score") or float("-inf")),
        )
        best_result["selection_reason"] = "embedded_timing_reference"
        return best_path, best_result

    # Global score rewards the largest correctly aligned region, but local FFT
    # windows are allowed to override it only when their quality gate passes.
    accepted_reference_failed = bool(
        timing_reference_validation
        and timing_reference_validation.get("accepted")
        and (
            any(item.get("embedded_reference_failure_reason") for _, item in successful)
            or any(
                str(item.get("reason") or "").startswith("alass")
                or item.get("reason") == "embedded_reference_output_invalid"
                for item in failed
            )
        )
    )
    if config.segment_validation and not accepted_reference_failed:
        for path, result in successful:
            diagnostics = evaluate_segment_alignment(
                video,
                path,
                cache_dir,
                config,
                ffmpeg_path=ffmpeg_path,
                ffprobe_path=ffprobe_path,
                force=force,
                verbose=verbose,
            )
            result["segment_diagnostics"] = diagnostics
            if verbose and diagnostics.get("available"):
                if diagnostics.get("reliable"):
                    print(
                        f"    {result.get('engine')}: локальная проверка надёжна, max offset="
                        f"{diagnostics.get('max_abs_offset_seconds')}s, spread="
                        f"{diagnostics.get('offset_spread_seconds')}s"
                    )
                else:
                    print(
                        f"    {result.get('engine')}: локальная проверка отклонена "
                        f"({diagnostics.get('quality_reason')})"
                    )

    def global_score(item: tuple[Path, dict[str, object]]) -> float:
        raw = item[1].get("alignment_score")
        try:
            return float(raw) if raw is not None else float("-inf")
        except (TypeError, ValueError):
            return float("-inf")

    trusted = [
        item
        for item in successful
        if isinstance(item[1].get("segment_diagnostics"), dict)
        and item[1]["segment_diagnostics"].get("reliable", True)
    ]
    if trusted:
        best_path, best_result = max(
            trusted,
            key=lambda item: (
                _diagnostic_rank(item[1].get("segment_diagnostics")),
                global_score(item),
            ),
        )
        best_result["selection_reason"] = "trusted_local_diagnostics"
    else:
        best_path, best_result = max(successful, key=global_score)
        best_result["selection_reason"] = "global_score_fallback"
        best_result["local_diagnostics_ignored"] = True

    compared: list[dict[str, object]] = []
    for _, result in successful:
        diagnostics = result.get("segment_diagnostics")
        compared.append(
            {
                "engine": str(result.get("engine", "unknown")),
                "alignment_score": result.get("alignment_score"),
                "reason": result.get("reason"),
                "local_reliable": diagnostics.get("reliable")
                if isinstance(diagnostics, dict)
                else None,
                "local_quality_reason": diagnostics.get("quality_reason")
                if isinstance(diagnostics, dict)
                else None,
                "max_abs_segment_offset": diagnostics.get("max_abs_offset_seconds")
                if isinstance(diagnostics, dict)
                else None,
                "segment_offset_spread": diagnostics.get("offset_spread_seconds")
                if isinstance(diagnostics, dict)
                else None,
            }
        )
    best_result["compared_engines"] = compared
    if accepted_reference_failed:
        best_result["selection_reason"] = "embedded_reference_failed_global_fallback"
        best_result["local_diagnostics_ignored"] = True
        return best_path, best_result
    return _maybe_repair_piecewise(
        video,
        best_path,
        best_result,
        cache_dir,
        config,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        force=force,
        verbose=verbose,
        source_path=subtitle,
    )


def subtitle_quality_accepted(result: dict[str, object]) -> tuple[bool, str]:
    """Reject a subtitle when the synchronization diagnostics clearly look wrong.

    Missing optional diagnostics are not treated as failure, but an explicit
    semantic mismatch, failed synchronization, or unreliable multi-window timing
    check is strong evidence that the subtitle belongs to another episode.
    """
    if not bool(result.get("sync_was_successful")):
        return False, str(result.get("reason") or "синхронизация не удалась")

    validation = result.get("timing_reference_validation")
    if isinstance(validation, dict):
        compared = int(validation.get("total_samples") or 0) > 0
        if compared and validation.get("accepted") is False:
            context = result.get("candidate_context")
            structure = validation.get("reference_output_structure")
            activity = validation.get("reference_activity")
            exact_single_special = bool(
                isinstance(context, dict)
                and context.get("source") == "jimaku"
                and bool(context.get("entry_anilist_match"))
                and bool(context.get("entry_exact_title_match"))
                and bool(context.get("single_special_exact_entry"))
                and str(context.get("subtitle_suffix") or "").casefold()
                    not in {".sup", ".pgs", ".sub", ".idx"}
            )
            exact_linked_movie = bool(
                isinstance(context, dict)
                and context.get("source") == "jimaku"
                and bool(context.get("exact_anilist_movie_entry"))
                and bool(context.get("entry_exact_title_match"))
                and str(context.get("episode_match") or "") == "exact"
                and str(context.get("media_format") or "").casefold() == "movie"
                and str(context.get("subtitle_suffix") or "").casefold()
                    not in {".sup", ".pgs", ".sub", ".idx"}
            )
            exact_linked_numbered_episode = bool(
                isinstance(context, dict)
                and context.get("source") == "jimaku"
                and bool(context.get("entry_anilist_match"))
                and str(context.get("episode_match") or "") == "exact"
                and (
                    bool(context.get("entry_exact_title_match"))
                    or float(context.get("filename_score") or 0.0) >= 80.0
                )
                and str(context.get("media_format") or "").casefold()
                    in {"tv", "tv_short", "ona", "ova"}
                and str(context.get("subtitle_suffix") or "").casefold()
                    not in {".sup", ".pgs", ".sub", ".idx"}
            )
            structurally_complete = bool(
                validation.get("alignment_mode") == "alass-timestamp"
                and validation.get("structure_reason") == "ok"
                and isinstance(structure, dict)
                and float(structure.get("retained_ratio") or 0.0) >= 0.95
                and isinstance(activity, dict)
                and float(activity.get("weighted") or 0.0) >= 0.80
            )
            if exact_single_special and structurally_complete:
                return (
                    True,
                    "точный одноэпизодный SPECIAL Jimaku; "
                    "текстовая дорожка совпадает по структуре и таймингу",
                )
            if exact_linked_movie and structurally_complete:
                return (
                    True,
                    "точный фильм AniList/Jimaku; "
                    "текстовая дорожка совпадает по структуре и таймингу",
                )
            # Exact AniList-linked episode files are substantially stronger identity
            # evidence than semantic spot checks against an embedded translation.
            # Broadcast/streaming masters can differ in sponsor cards, recaps and
            # dialogue cue placement; that produced false negatives for exact Jimaku
            # files such as Otome Kaijuu ep.6 and BLEACH absolute ep.43.  Accept only
            # when the complete subtitle structure survives ALASS and there is still
            # meaningful independent clock/semantic evidence.
            exact_numbered_structure = bool(
                exact_linked_numbered_episode
                and validation.get("alignment_mode") == "alass-timestamp"
                and validation.get("structure_reason") == "ok"
                and isinstance(structure, dict)
                and float(structure.get("retained_ratio") or 0.0) >= 0.95
                and int(structure.get("source_cues") or 0) >= 120
                and int(structure.get("aligned_cues") or 0)
                    == int(structure.get("source_cues") or 0)
                and isinstance(activity, dict)
            )
            if exact_numbered_structure:
                diagnostics = result.get("segment_diagnostics")
                severe_local_failure = False
                severe_local_reason = ""
                if (
                    isinstance(diagnostics, dict)
                    and bool(diagnostics.get("available"))
                    and diagnostics.get("reliable") is False
                ):
                    severe_local_reason = str(diagnostics.get("quality_reason") or "")
                    severe_reasons = {
                        part.strip()
                        for part in severe_local_reason.split(",")
                        if part.strip()
                    }
                    severe_local_failure = bool(
                        severe_reasons
                        & {
                            "full_range_oscillation",
                            "too_many_large_jumps",
                            "no_stable_offset_cluster",
                        }
                    )
                if severe_local_failure:
                    return (
                        False,
                        "точная серия найдена, но выбранный тайминг нестабилен: "
                        + severe_local_reason,
                    )

                weighted_activity = float(activity.get("weighted") or 0.0)
                matched_samples = int(validation.get("matched_samples") or 0)
                total_samples = int(validation.get("total_samples") or 0)
                # High timing activity is enough on its own; with noisier broadcast
                # clocks require at least two semantic samples to agree as a second
                # independent signal. This identity exception must never override
                # a severe multi-window timing failure.
                if weighted_activity >= 0.78 or (
                    weighted_activity >= 0.65
                    and total_samples >= 4
                    and matched_samples >= 2
                ):
                    return (
                        True,
                        "точная серия AniList/Jimaku; номер и полная структура "
                        "подтверждены таймингом",
                    )
            return False, str(validation.get("reason") or "содержание не совпало с серией")

    if bool(result.get("reference_discontinuity_rejected")):
        gate = result.get("reference_discontinuity_gate")
        gate_data = gate if isinstance(gate, dict) else {}
        return (
            False,
            "встроенная дорожка: ALASS дал неподтверждённый большой скачок "
            f"(spread={gate_data.get('spread_seconds', '?')}s)",
        )

    if bool(result.get("reference_alignment_reliable")):
        return True, "надёжная встроенная временная дорожка"

    diagnostics = result.get("segment_diagnostics")
    if isinstance(diagnostics, dict) and bool(diagnostics.get("available")):
        if not bool(diagnostics.get("reliable")):
            quality_reason = str(
                diagnostics.get("quality_reason")
                or "нестабильные метрики в разных участках серии"
            )
            reasons = {
                part.strip()
                for part in quality_reason.split(",")
                if part.strip()
            }
            context = result.get("candidate_context")
            if isinstance(context, dict):
                title_similarity_value = float(context.get("title_similarity") or 0.0)
                filename_score = float(context.get("filename_score") or 0.0)
                exact_identity = (
                    context.get("source") == "jimaku"
                    and context.get("episode_match") == "exact"
                    and (
                        bool(context.get("entry_anilist_match"))
                        or title_similarity_value >= 85.0
                    )
                    and filename_score >= 80.0
                )
                # Broadcast masters such as AT-X/BS-EX can differ from a CR video
                # at the opening, sponsor cards and credits. That produces boundary
                # hits while the interior timing and exact Jimaku identity remain
                # trustworthy. Do not treat this single diagnostic as a different
                # episode; semantic mismatch and large-jump failures still reject.
                if exact_identity and reasons == {"too_many_boundary_hits"}:
                    return True, "точная серия Jimaku; различаются только границы эфира"
            return False, str(
                quality_reason
            )

    return True, "ok"


_EMBEDDED_REFERENCE_SRT_ACTIVITY_TOLERANCE = 0.005


def _rank_embedded_reference_candidates(
    items: list[
        tuple[
            tuple[float, ...],
            SubtitleCandidate,
            Path,
            dict[str, object],
            dict[str, object],
            dict[str, object],
        ]
    ],
    *,
    prefer_srt: bool,
    activity_tolerance: float = _EMBEDDED_REFERENCE_SRT_ACTIVITY_TOLERANCE,
) -> tuple[
    list[
        tuple[
            tuple[float, ...],
            SubtitleCandidate,
            Path,
            dict[str, object],
            dict[str, object],
            dict[str, object],
        ]
    ],
    dict[str, object],
]:
    """Rank embedded-reference candidates without overfitting tiny score noise.

    Timing-activity scores are estimates, not exact quality measurements. A tiny
    difference (for example 0.8940 versus 0.8935) must not override an explicit
    SRT preference. Candidates inside ``activity_tolerance`` of the best valid
    clock are treated as timing-equivalent; format preference and filename score
    decide between them. A materially better ASS/SSA candidate still wins.
    """
    if not items:
        return [], {
            "activity_tolerance": max(0.0, float(activity_tolerance)),
            "best_activity": None,
            "format_preference_applied": False,
        }

    tolerance = max(0.0, float(activity_tolerance))

    def activity_value(item: tuple[object, ...]) -> float:
        activity = item[4]
        try:
            return float(activity.get("weighted") or 0.0) if activity.get("available") else 0.0
        except (AttributeError, TypeError, ValueError):
            return 0.0

    def structure_ok(item: tuple[object, ...]) -> bool:
        structure = item[5]
        return isinstance(structure, dict) and structure.get("reason") == "ok"

    structurally_valid = [item for item in items if structure_ok(item)]
    activity_pool = structurally_valid or items
    best_activity = max((activity_value(item) for item in activity_pool), default=0.0)

    rebuilt = []
    for _old_rank, candidate, aligned, alass_result, activity, structure in items:
        weighted = activity_value((_old_rank, candidate, aligned, alass_result, activity, structure))
        valid = structure_ok((_old_rank, candidate, aligned, alass_result, activity, structure))
        near_best = valid and weighted >= best_activity - tolerance
        native_srt = bool(prefer_srt and candidate.path.suffix.casefold() == ".srt")

        if near_best:
            # Inside the noise band, honor the user's format preference first.
            rank = (
                1.0 if valid else 0.0,
                1.0,
                1.0 if native_srt else 0.0,
                float(candidate.score),
                weighted,
            )
        else:
            # Outside the tolerance, real timing superiority remains decisive.
            rank = (
                1.0 if valid else 0.0,
                0.0,
                weighted,
                1.0 if native_srt else 0.0,
                float(candidate.score),
            )
        rebuilt.append((rank, candidate, aligned, alass_result, activity, structure))

    ordered = sorted(rebuilt, key=lambda item: item[0], reverse=True)
    selected = ordered[0] if ordered else None
    selected_activity = activity_value(selected) if selected is not None else 0.0
    best_raw = max(items, key=activity_value) if items else None
    best_raw_candidate = best_raw[1] if best_raw is not None else None
    selected_candidate = selected[1] if selected is not None else None
    preference_applied = bool(
        prefer_srt
        and selected_candidate is not None
        and selected_candidate.path.suffix.casefold() == ".srt"
        and best_raw_candidate is not None
        and best_raw_candidate.path != selected_candidate.path
        and best_activity - selected_activity <= tolerance
    )
    return ordered, {
        "activity_tolerance": tolerance,
        "best_activity": round(best_activity, 6),
        "selected_activity": round(selected_activity, 6),
        "format_preference_applied": preference_applied,
        "selected": selected_candidate.name if selected_candidate is not None else None,
        "raw_best": best_raw_candidate.name if best_raw_candidate is not None else None,
    }

def _exact_jimaku_timing_consensus(
    items: list[
        tuple[
            tuple[float, ...],
            SubtitleCandidate,
            Path,
            dict[str, object],
            dict[str, object],
            dict[str, object],
        ]
    ],
) -> tuple[
    tuple[
        tuple[float, ...],
        SubtitleCandidate,
        Path,
        dict[str, object],
        dict[str, object],
        dict[str, object],
    ] | None,
    dict[str, object],
]:
    """Trust several exact Jimaku releases that independently fit one clock.

    Some releases contain layered signs, tweets, songs, or accessibility cues.
    A semantic sampler can then compare a Japanese dialogue cue with an English
    on-screen-text cue at the same timestamp and falsely call the whole episode
    unrelated. Three independently authored subtitle files that all have the
    exact AniList/episode identity, preserve their structure through ALASS, and
    strongly match the same embedded clock are better evidence than that noisy
    sample. The thresholds are deliberately strict, so ambiguous entries still
    use the normal LLM gate.
    """
    qualified = []
    for item in items:
        _rank, candidate, _aligned, _alass, activity, structure = item
        details = candidate.details
        try:
            weighted = float(activity.get("weighted") or 0.0)
        except (TypeError, ValueError):
            weighted = 0.0
        exact_identity = (
            candidate.source == "jimaku"
            and details.get("episode_match") == "exact"
            and bool(details.get("entry_anilist_match"))
            and float(candidate.score) >= 75.0
        )
        structurally_complete = (
            structure.get("reason") == "ok"
            and float(structure.get("retained_ratio") or 0.0) >= 0.95
        )
        if exact_identity and structurally_complete and weighted >= 0.80:
            qualified.append((weighted, item))

    scores = sorted((score for score, _item in qualified), reverse=True)
    payload: dict[str, object] = {
        "accepted": False,
        "reason": "insufficient_exact_timing_consensus",
        "qualified_candidates": len(qualified),
        "activity_scores": [round(score, 4) for score in scores],
    }
    if len(qualified) < 3:
        # A single exact episode can still be safer than semantic sampling when
        # every independent identity/timing signal is exceptionally strong.
        # This covers entries that publish the same release as SRT+ASS rather
        # than three independently authored subtitle files.
        strict_candidates = []
        for weighted, item in qualified:
            _rank, candidate, _aligned, _alass, _activity, structure = item
            details = candidate.details
            source_cues = int(structure.get("source_cues") or 0)
            aligned_cues = int(structure.get("aligned_cues") or 0)
            try:
                title_similarity = float(details.get("title_similarity") or 0.0)
            except (TypeError, ValueError):
                title_similarity = 0.0
            strict_identity = (
                bool(details.get("entry_exact_title_match"))
                and title_similarity >= 95.0
                and float(candidate.score) >= 90.0
            )
            strict_structure = (
                float(structure.get("retained_ratio") or 0.0) >= 0.99
                and source_cues >= 120
                and aligned_cues == source_cues
            )
            if strict_identity and strict_structure and weighted >= 0.92:
                strict_candidates.append((weighted, item))

        if strict_candidates:
            selected_score, selected = max(strict_candidates, key=lambda value: value[1][0])
            payload.update(
                {
                    "accepted": True,
                    "reason": "exact_jimaku_strong_clock",
                    "selected": selected[1].name,
                    "best_activity": round(selected_score, 4),
                }
            )
            return selected, payload
        return None, payload

    median_score = statistics.median(scores)
    best_score = max(scores)
    payload.update(
        {
            "median_activity": round(median_score, 4),
            "best_activity": round(best_score, 4),
        }
    )
    if median_score < 0.88 or best_score < 0.91:
        payload["reason"] = "exact_timing_consensus_too_weak"
        return None, payload

    selected = max((item for _score, item in qualified), key=lambda item: item[0])
    payload.update(
        {
            "accepted": True,
            "reason": "exact_jimaku_timing_consensus",
            "selected": selected[1].name,
        }
    )
    return selected, payload


def optimize_candidates(
    video: Path,
    candidates: Iterable[SubtitleCandidate],
    cache_dir: Path,
    config: SyncConfig,
    *,
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
    alass_path: str = "alass",
    force: bool = False,
    verbose: bool = False,
    prefer_srt: bool = True,
    srt_tolerance_ratio: float = 0.002,
    srt_tolerance_absolute: float = 50.0,
    llm: OllamaClient | None = None,
    validate_embedded_reference_with_llm: bool = False,
) -> tuple[SubtitleCandidate | None, Path | None, dict[str, object]]:
    """Score every candidate with one shared audio analysis, then optimize the winner."""
    all_candidates = list(candidates)
    identity_rejected = [
        candidate for candidate in all_candidates
        if _candidate_explicit_anilist_mismatch(candidate)
    ]
    candidate_list = [
        candidate for candidate in all_candidates
        if not _candidate_explicit_anilist_mismatch(candidate)
    ]
    if not candidate_list:
        reason = (
            "explicit_anilist_identity_mismatch"
            if identity_rejected
            else "no_candidates"
        )
        return None, None, _result(
            reason,
            sync_was_successful=False,
            identity_rejected=[
                {
                    "name": candidate.name,
                    "source": candidate.source,
                    "entry_anilist_id": candidate.details.get("entry_anilist_id"),
                    "requested_anilist_id": candidate.details.get("requested_anilist_id"),
                }
                for candidate in identity_rejected
            ],
        )
    if identity_rejected:
        configure_logging().info(
            "REJECT step=subtitle.optimize reason=explicit_anilist_mismatch count=%s candidates=%s",
            len(identity_rejected),
            [
                (
                    candidate.name,
                    candidate.details.get("entry_anilist_id"),
                    candidate.details.get("requested_anilist_id"),
                )
                for candidate in identity_rejected
            ],
        )
    container_edit_points = (
        probe_container_edit_points(video, ffprobe_path)
        if config.use_container_chapters
        else []
    )
    # Prefer a subtitle clock before extracting audio. Container chapters make
    # the deterministic exact-episode consensus and local cut repair useful even
    # when semantic LLM checks are disabled (the default). Only ambiguous
    # candidates enter the optional LLM loop below.
    if config.use_container_chapters or (
        validate_embedded_reference_with_llm and llm is not None
    ):
        timing_reference, timing_reference_result = extract_embedded_timing_reference(
            video,
            cache_dir,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            force=force,
            verbose=verbose,
        )
        if timing_reference is not None:
            if verbose:
                language = timing_reference_result.get("language") or "-"
                title = timing_reference_result.get("title") or "-"
                print(f"  Эталон до аудиоанализа: встроенная дорожка {language}, {title}")

            prealigned: list[
                tuple[
                    tuple[float, ...],
                    SubtitleCandidate,
                    Path,
                    dict[str, object],
                    dict[str, object],
                    dict[str, object],
                ]
            ] = []
            for index, candidate in enumerate(candidate_list, start=1):
                if candidate.path.suffix.casefold() not in {".srt", ".ass", ".ssa"}:
                    continue
                semantic_source = candidate.path
                if semantic_source.suffix.casefold() in {".ass", ".ssa"}:
                    semantic_source, conversion = convert_to_plain_srt(
                        semantic_source,
                        cache_dir,
                        ffmpeg_path=ffmpeg_path,
                        force=force,
                        verbose=verbose,
                    )
                    if semantic_source.suffix.casefold() != ".srt":
                        continue

                aligned, alass_result = align_subtitle_timelines(
                    semantic_source,
                    timing_reference,
                    cache_dir,
                    max_offset_seconds=config.max_offset_seconds,
                    force=force,
                )
                timeline_attempt = dict(alass_result)
                _record_timeline_debug_attempt(
                    cache_dir, video, semantic_source, timing_reference, timeline_attempt,
                    stage=f"optimize_candidate_{index}",
                )
                if not bool(alass_result.get("timeline_alignment_reliable")):
                    aligned, alass_result = synchronize_with_alass(
                        timing_reference,
                        semantic_source,
                        cache_dir,
                        config,
                        alass_path=alass_path,
                        ffmpeg_path=ffmpeg_path,
                        ffprobe_path=ffprobe_path,
                        force=force,
                        verbose=verbose,
                    )
                    alass_result["timeline_alignment_attempt"] = timeline_attempt
                elif verbose:
                    print(
                        "  Subtitle-only timeline alignment принят: "
                        f"segments={alass_result.get('timeline_segments', [])}"
                    )
                if not bool(alass_result.get("sync_was_successful")):
                    if verbose:
                        print(
                            f"  Субтитровый кандидат {index}/{len(candidate_list)}: "
                            f"ALASS не выполнен ({alass_result.get('reason', '-')})"
                        )
                    continue
                if alass_result.get("offset_seconds") is None:
                    alass_result.update(_subtitle_shift_summary(semantic_source, aligned))

                aligned_plain = aligned
                if aligned_plain.suffix.casefold() in {".ass", ".ssa"}:
                    aligned_plain, _ = convert_to_plain_srt(
                        aligned_plain,
                        cache_dir,
                        ffmpeg_path=ffmpeg_path,
                        force=force,
                        verbose=verbose,
                    )
                structure_ok, structure_reason, structure = _validate_embedded_reference_output(
                    semantic_source,
                    aligned_plain,
                    timing_reference,
                )
                activity = compare_timing_activity(aligned_plain, timing_reference)
                weighted = float(activity.get("weighted") or 0.0) if activity.get("available") else 0.0
                native_srt = float(prefer_srt and candidate.path.suffix.casefold() == ".srt")
                rank = (
                    1.0 if structure_ok else 0.0,
                    weighted,
                    native_srt,
                    float(candidate.score),
                )
                structure_payload = dict(structure)
                structure_payload["reason"] = structure_reason
                prealigned.append(
                    (
                        rank,
                        candidate,
                        aligned_plain,
                        dict(alass_result),
                        activity,
                        structure_payload,
                    )
                )
                if verbose:
                    offset = alass_result.get("offset_seconds")
                    constant_shift = alass_result.get("alass_constant_shift")
                    if isinstance(offset, (int, float)) and constant_shift is not False:
                        offset_text = f"{float(offset):+.2f}s"
                    elif isinstance(offset, (int, float)):
                        offset_text = f"non-linear (median={float(offset):+.2f}s)"
                    else:
                        offset_text = "non-linear"
                    print(
                        f"  Субтитровый кандидат {index}/{len(candidate_list)}: "
                        f"offset={offset_text}, структура={structure_reason}, "
                        f"activity={activity.get('weighted', '-')}"
                    )

            sorted_prealigned, embedded_rank_meta = _rank_embedded_reference_candidates(
                prealigned,
                prefer_srt=prefer_srt,
            )
            if embedded_rank_meta.get("format_preference_applied"):
                configure_logging().info(
                    "SELECT step=subtitle.embedded_reference_format_preference "
                    "video=%s raw_best=%r selected=%r best_activity=%s selected_activity=%s tolerance=%s",
                    video.name,
                    embedded_rank_meta.get("raw_best"),
                    embedded_rank_meta.get("selected"),
                    embedded_rank_meta.get("best_activity"),
                    embedded_rank_meta.get("selected_activity"),
                    embedded_rank_meta.get("activity_tolerance"),
                )
                if verbose:
                    print(
                        "  Разница activity находится в пределах шума; "
                        f"выбран SRT: {embedded_rank_meta.get('selected')}"
                    )
            deterministic_timeline_item = next(
                (
                    item
                    for item in sorted_prealigned
                    if bool(item[3].get("timeline_alignment_reliable"))
                    and isinstance(item[5], dict)
                    and item[5].get("reason") == "ok"
                    and (
                        (
                            item[1].source == "jimaku"
                            and item[1].details.get("episode_match") == "exact"
                            and bool(item[1].details.get("entry_anilist_match"))
                        )
                        or (
                            isinstance(item[3].get("timeline_validation"), dict)
                            and float(
                                item[3]["timeline_validation"]
                                .get("after", {})
                                .get("f1", 0.0)
                                or 0.0
                            ) >= 0.72
                            and float(
                                item[3]["timeline_validation"].get("activity_f1", 0.0)
                                or 0.0
                            ) >= 0.72
                        )
                    )
                ),
                None,
            )
            if deterministic_timeline_item is not None:
                (
                    _rank,
                    candidate,
                    aligned,
                    timeline_result,
                    activity,
                    structure,
                ) = deterministic_timeline_item
                final_result = dict(timeline_result)
                final_result.update(
                    {
                        "reason": "applied",
                        "sync_was_successful": True,
                        "engine": "embedded-reference+timeline",
                        "output": str(aligned),
                        "timing_reference": str(timing_reference),
                        "timing_reference_language": timing_reference_result.get("language"),
                        "timing_reference_title": timing_reference_result.get("title"),
                        "reference_activity": activity,
                        "reference_output_structure": structure,
                        "reference_alignment_reliable": True,
                        "selection_reason": "subtitle_timeline_alignment",
                        "candidate_selection": {
                            "mode": "subtitle_timeline_alignment",
                            "candidate_count": len(candidate_list),
                            "prealigned_count": len(prealigned),
                            "prefer_srt": prefer_srt,
                            "embedded_reference_ranking": embedded_rank_meta,
                        },
                    }
                )
                if verbose:
                    print(
                        "  Выбран deterministic subtitle timeline: "
                        f"{candidate.name}; "
                        f"segments={timeline_result.get('timeline_segments', [])}"
                    )
                return candidate, aligned, final_result

            consensus_item, consensus = _exact_jimaku_timing_consensus(
                sorted_prealigned
            )
            if consensus_item is not None:
                _rank, candidate, aligned, alass_result, activity, structure = consensus_item
                consensus_reason = str(
                    consensus.get("reason") or "exact_jimaku_timing_consensus"
                )
                group_matching_basis = aligned
                repaired_aligned, repair_result = repair_with_embedded_reference_piecewise(
                    aligned,
                    timing_reference,
                    cache_dir,
                    config,
                    llm=llm,
                    edit_points=container_edit_points,
                    force=force,
                    verbose=verbose,
                )
                if repair_result.get("applied"):
                    aligned = repaired_aligned
                    activity = compare_timing_activity(aligned, timing_reference)
                grouped_aligned, group_result = refine_with_embedded_reference_groups(
                    aligned,
                    timing_reference,
                    cache_dir,
                    matching_basis=group_matching_basis,
                    force=force,
                    verbose=verbose,
                )
                if group_result.get("applied"):
                    aligned = grouped_aligned
                    activity = compare_timing_activity(aligned, timing_reference)
                validation = {
                    "accepted": True,
                    "reason": consensus_reason,
                    "same_episode": True,
                    "usable_for_timing": True,
                    "similarity": None,
                    "matched_samples": 0,
                    "total_samples": 0,
                    "semantic_check_skipped": True,
                    "reference_activity": activity,
                    "consensus": consensus,
                }
                final_result = dict(alass_result)
                final_result.update(
                    {
                        "reason": "applied",
                        "sync_was_successful": True,
                        "engine": (
                            "embedded-reference+alass"
                            + (
                                _reference_repair_engine_suffix(repair_result)
                                if repair_result.get("applied")
                                else ""
                            )
                            + "+trusted-clock"
                        ),
                        "output": str(aligned),
                        "timing_reference": str(timing_reference),
                        "timing_reference_language": timing_reference_result.get("language"),
                        "timing_reference_title": timing_reference_result.get("title"),
                        "timing_reference_validation": validation,
                        "reference_activity": activity,
                        "reference_output_structure": structure,
                        "reference_alignment_reliable": True,
                        "selection_reason": consensus_reason,
                        "candidate_selection": {
                            "mode": consensus_reason,
                            "candidate_count": len(candidate_list),
                            "prealigned_count": len(prealigned),
                            "prefer_srt": prefer_srt,
                            "consensus": consensus,
                            "embedded_reference_ranking": embedded_rank_meta,
                        },
                    }
                )
                if repair_result.get("applied"):
                    final_result["reference_piecewise_repair"] = repair_result
                if group_result.get("applied"):
                    final_result["reference_group_refinement"] = group_result
                    final_result["engine"] += "+reference-groups"
                configure_logging().info(
                    "ACCEPT step=subtitle.timing_consensus video=%s candidate=%r qualified=%s median_activity=%s best_activity=%s",
                    video.name,
                    candidate.name,
                    consensus.get("qualified_candidates"),
                    consensus.get("median_activity"),
                    consensus.get("best_activity"),
                )
                if verbose:
                    print(
                        "  Точная серия подтверждена строгим совпадением "
                        f"AniList/Jimaku и таймингов: {candidate.name}"
                    )
                return candidate, aligned, final_result

            semantic_candidates = (
                sorted_prealigned[:5]
                if validate_embedded_reference_with_llm and llm is not None
                else []
            )
            for semantic_index, item in enumerate(
                semantic_candidates,
                start=1,
            ):
                _rank, candidate, aligned, alass_result, activity, structure = item
                if structure.get("reason") != "ok":
                    continue
                validation = llm.compare_subtitle_semantics(
                    aligned,
                    timing_reference,
                    alignment_mode="timestamp",
                    force=force,
                )
                validation["alignment_mode"] = "alass-timestamp"
                validation["estimated_offset_seconds"] = alass_result.get("offset_seconds")
                validation["alass_constant_shift"] = alass_result.get("alass_constant_shift")
                validation["alass_shift_spread_seconds"] = alass_result.get(
                    "alass_shift_spread_seconds"
                )
                validation["reference_activity"] = activity
                validation["reference_output_structure"] = structure
                validation = _apply_robust_semantic_activity_gate(validation, activity)
                if verbose:
                    state = "принят" if validation.get("accepted") else "отклонён"
                    print(
                        f"  LLM-проверка субтитрового кандидата {semantic_index}/"
                        f"{min(5, len(prealigned))}: {state}, "
                        f"similarity={validation.get('similarity', '-')}, "
                        f"phrases={validation.get('matched_samples', '-')}/"
                        f"{validation.get('total_samples', '-')}, "
                        f"reason={validation.get('reason', '-')}"
                    )
                if not bool(validation.get("accepted")):
                    continue

                final_result = dict(alass_result)
                group_matching_basis = aligned
                repaired_aligned, repair_result = repair_with_embedded_reference_piecewise(
                    aligned,
                    timing_reference,
                    cache_dir,
                    config,
                    llm=llm,
                    edit_points=container_edit_points,
                    force=force,
                    verbose=verbose,
                )
                if repair_result.get("applied"):
                    aligned = repaired_aligned
                    activity = compare_timing_activity(aligned, timing_reference)
                    validation["reference_activity"] = activity
                grouped_aligned, group_result = refine_with_embedded_reference_groups(
                    aligned,
                    timing_reference,
                    cache_dir,
                    matching_basis=group_matching_basis,
                    force=force,
                    verbose=verbose,
                )
                if group_result.get("applied"):
                    aligned = grouped_aligned
                    activity = compare_timing_activity(aligned, timing_reference)
                    validation["reference_activity"] = activity
                final_result.update(
                    {
                        "reason": "applied",
                        "sync_was_successful": True,
                        "engine": (
                            "embedded-reference+alass"
                            + (
                                _reference_repair_engine_suffix(repair_result)
                                if repair_result.get("applied")
                                else ""
                            )
                        ),
                        "output": str(aligned),
                        "timing_reference": str(timing_reference),
                        "timing_reference_language": timing_reference_result.get("language"),
                        "timing_reference_title": timing_reference_result.get("title"),
                        "timing_reference_validation": validation,
                        "reference_activity": activity,
                        "reference_output_structure": structure,
                        "reference_alignment_reliable": True,
                        "selection_reason": "subtitle_reference_before_audio",
                        "candidate_selection": {
                            "mode": "subtitle_reference_before_audio",
                            "candidate_count": len(candidate_list),
                            "prealigned_count": len(prealigned),
                            "semantic_rank": semantic_index,
                            "prefer_srt": prefer_srt,
                            "embedded_reference_ranking": embedded_rank_meta,
                        },
                    }
                )
                if repair_result.get("applied"):
                    final_result["reference_piecewise_repair"] = repair_result
                if group_result.get("applied"):
                    final_result["reference_group_refinement"] = group_result
                    final_result["engine"] += "+reference-groups"
                return candidate, aligned, final_result

    reference, reference_result = prepare_speech_reference(
        video,
        cache_dir,
        config,
        ffmpeg_path=ffmpeg_path,
        force=force,
        verbose=verbose,
    )
    if verbose and reference is not None:
        state = "кэш" if reference_result.get("reason") == "cached" else "готов"
        print(f"  Общий аудиоанализ: {state}")

    evaluation_config = replace(config, engine="ffsubsync", compare_engines=False)
    evaluated: list[tuple[SubtitleCandidate, Path, dict[str, object]]] = []
    for index, candidate in enumerate(candidate_list, start=1):
        if verbose:
            print(f"  Проверка {index}/{len(candidate_list)}: {candidate.name}")
        path, result = synchronize_subtitle(
            video,
            candidate.path,
            cache_dir,
            evaluation_config,
            ffmpeg_path=ffmpeg_path,
            force=force,
            verbose=verbose,
            quiet=True,
            tag="candidate-eval",
            reference=reference,
        )
        result["engine"] = "ffsubsync-evaluation"
        evaluated.append((candidate, path, result))
        if verbose:
            print(
                f"    score={result.get('alignment_score', '-')}, "
                f"offset={result.get('offset_seconds', '-')}, reason={result.get('reason')}"
            )

    successful_evaluated = [
        item for item in evaluated if bool(item[2].get("sync_was_successful"))
    ]
    selection_pool = successful_evaluated or evaluated

    def alignment_value(item: tuple[SubtitleCandidate, Path, dict[str, object]]) -> float:
        raw = item[2].get("alignment_score")
        try:
            return float(raw) if raw is not None else float("-inf")
        except (TypeError, ValueError):
            return float("-inf")

    best_alignment = max((alignment_value(item) for item in selection_pool), default=float("-inf"))
    tolerance = max(
        max(0.0, srt_tolerance_absolute),
        abs(best_alignment) * max(0.0, srt_tolerance_ratio) if best_alignment != float("-inf") else 0.0,
    )
    near_best = [
        item for item in selection_pool
        if alignment_value(item) >= best_alignment - tolerance
    ] or selection_pool

    def candidate_score(item: tuple[SubtitleCandidate, Path, dict[str, object]]) -> tuple[int, float, float]:
        candidate, _, _ = item
        native_srt = int(prefer_srt and candidate.path.suffix.casefold() == ".srt")
        return native_srt, candidate.score, alignment_value(item)

    primary_item = max(near_best, key=candidate_score)

    def fallback_score(
        item: tuple[SubtitleCandidate, Path, dict[str, object]],
    ) -> tuple[int, int, float, int, float]:
        candidate, _, result = item
        exact_identity = int(
            candidate.source == "jimaku"
            and candidate.details.get("episode_match") in {"exact", "range"}
            and bool(candidate.details.get("entry_anilist_match"))
        )
        successful = int(bool(result.get("sync_was_successful")))
        native_srt = int(prefer_srt and candidate.path.suffix.casefold() == ".srt")
        return exact_identity, successful, alignment_value(item), native_srt, candidate.score

    # The old flow optimized exactly one filename winner. If that source had a
    # different broadcast master or a broken caption clock, all other Jimaku
    # variants were silently discarded. Keep the normal winner first, then try
    # source-diverse alternatives until one passes the full quality gate.
    ordered_items = [primary_item]
    seen_candidate_paths = {primary_item[0].path}
    for item in sorted(selection_pool, key=fallback_score, reverse=True):
        if item[0].path in seen_candidate_paths:
            continue
        ordered_items.append(item)
        seen_candidate_paths.add(item[0].path)

    candidate_results = [
        {
            "name": candidate.name,
            "source": candidate.source,
            "filename_score": candidate.score,
            "alignment_score": result.get("alignment_score"),
            "offset_seconds": result.get("offset_seconds"),
            "successful": bool(result.get("sync_was_successful")),
        }
        for candidate, _, result in evaluated
    ]

    quality_attempts: list[dict[str, object]] = []
    quality_logger = configure_logging()
    last_result: dict[str, object] = _result(
        "all_candidate_quality_checks_failed",
        sync_was_successful=False,
    )
    # Eight is intentionally above the usual number of per-episode Jimaku
    # sources while still preventing pathological entries from causing an
    # unbounded cold-start loop.
    for attempt_index, (candidate, _, evaluation_result) in enumerate(
        ordered_items[:8],
        start=1,
    ):
        final_path, final_result = optimize_subtitle(
            video,
            candidate.path,
            cache_dir,
            config,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            alass_path=alass_path,
            force=force,
            verbose=verbose,
            reference=reference,
            llm=llm,
            validate_embedded_reference_with_llm=validate_embedded_reference_with_llm,
        )
        if final_result.get("alignment_score") is None:
            final_result["alignment_score"] = evaluation_result.get("alignment_score")
        final_result["candidate_selection"] = {
            "best_alignment": best_alignment,
            "srt_tolerance": tolerance,
            "near_best_count": len(near_best),
            "prefer_srt": prefer_srt,
            "quality_attempt": attempt_index,
            "quality_attempt_limit": min(8, len(ordered_items)),
        }
        final_result["candidate_results"] = candidate_results
        final_result["candidate_context"] = {
            "source": candidate.source,
            "name": candidate.name,
            "filename_score": candidate.score,
            "episode_match": candidate.details.get("episode_match"),
            "title_similarity": candidate.details.get("title_similarity"),
            "entry_anilist_match": candidate.details.get("entry_anilist_match"),
            "entry_exact_title_match": candidate.details.get("entry_exact_title_match"),
            "single_special_exact_entry": candidate.details.get("single_special_exact_entry"),
            "exact_anilist_movie_entry": candidate.details.get("exact_anilist_movie_entry"),
            "media_format": candidate.details.get("media_format"),
            "subtitle_suffix": candidate.path.suffix.casefold(),
            "entry_id": candidate.details.get("entry_id"),
            "entry_anilist_id": candidate.details.get("entry_anilist_id"),
            "requested_anilist_id": candidate.details.get("requested_anilist_id"),
        }
        accepted, quality_reason = subtitle_quality_accepted(final_result)
        final_result["candidate_quality_accepted"] = accepted
        final_result["candidate_quality_reason"] = quality_reason
        quality_attempts.append(
            {
                "attempt": attempt_index,
                "name": candidate.name,
                "source": candidate.source,
                "accepted": accepted,
                "reason": quality_reason,
                "alignment_score": final_result.get("alignment_score"),
                "engine": final_result.get("engine"),
            }
        )
        quality_logger.info(
            "RESULT step=subtitle.quality_fallback video=%s attempt=%s/%s accepted=%s reason=%r candidate=%r source=%s",
            video.name,
            attempt_index,
            min(8, len(ordered_items)),
            accepted,
            quality_reason,
            candidate.name,
            candidate.source,
        )
        final_result["quality_fallback_attempts"] = list(quality_attempts)
        last_result = final_result
        if accepted and final_path is not None:
            if attempt_index > 1:
                final_result["selection_reason"] = "quality_fallback_candidate"
            return candidate, final_path, final_result
        if verbose and attempt_index < min(8, len(ordered_items)):
            print(
                f"  Вариант {attempt_index} отклонён ({quality_reason}); "
                "проверяю следующий источник…"
            )

    last_result["quality_fallback_attempts"] = quality_attempts
    last_result["candidate_quality_accepted"] = False
    last_result["candidate_quality_reason"] = "все проверенные варианты отклонены"
    return None, None, last_result
