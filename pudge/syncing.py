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
from typing import Iterable

from rapidfuzz import fuzz

from .branding import APP_SLUG
from .cache_management import (
    cleanup_segment_audio_cache,
    mark_segment_audio_active,
    touch_segment_audio,
)
from .config import SyncConfig
from .language import TEXT_SUBTITLE_EXTENSIONS
from .llm import OllamaClient
from .logging_utils import configure_logging, timed_step
from .models import SubtitleCandidate
from .reading_audio_alignment import (
    align_light_novel_to_transcript,
    audio_position_for_light_novel_offset,
    normalize_reading_text,
)
from .pgs import build_time_mapper, onset_match_score, onset_times, parse_pgs_cues, retime_sup
from .subtitle_formats import convert_to_plain_srt, parse_srt, write_srt
from .subtitles.video_segments import choose_edit_boundary, probe_container_edit_points
from .subtitles.stt import prepare_japanese_stt_reference
from .subtitles.timeline_alignment import align_subtitle_timelines

_SCORE_RE = re.compile(r"\bscore:\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_ALASS_SHIFT_RE = re.compile(r"shifted block .*? by\s+([+-]?\d+:\d{2}:\d{2}(?:\.\d+)?)", re.IGNORECASE)
_TEXT_REFERENCE_CODECS = {"ass", "ssa", "subrip", "srt", "webvtt", "mov_text", "text"}
_RELEASE_CRC_RE = re.compile(r"\[(?P<crc>[0-9A-Fa-f]{8})\]")


def _matching_release_crc(video: Path, subtitle: Path) -> str | None:
    """Return a shared release CRC embedded in both filenames.

    Fansub/WebDL filenames commonly preserve the release CRC in ``[XXXXXXXX]``.
    When a Jimaku subtitle carries the same CRC as the video, it is stronger
    evidence than a different embedded-language timing reference: the subtitle
    was authored for this exact release and should keep its native clock.
    """
    video_crcs = {m.group("crc").casefold() for m in _RELEASE_CRC_RE.finditer(video.name)}
    subtitle_crcs = {m.group("crc").casefold() for m in _RELEASE_CRC_RE.finditer(subtitle.name)}
    shared = sorted(video_crcs & subtitle_crcs)
    return shared[0] if len(shared) == 1 else None


def _exact_release_zero_offset_result(
    video: Path, subtitle: Path, output: Path
) -> dict[str, object] | None:
    crc = _matching_release_crc(video, subtitle)
    if crc is None:
        return None
    return {
        "reason": "exact_release_crc_zero_offset",
        "sync_was_successful": True,
        "timeline_alignment_reliable": True,
        "engine": "exact-release-crc",
        "output": str(output),
        "offset_seconds": 0.0,
        "framerate_scale_factor": 1.0,
        "timeline_segments": [
            {
                "source_start": 0.0,
                "source_end": None,
                "offset_seconds": 0.0,
                "support": 0,
                "mean_score": 0.0,
                "mean_coverage": 0.0,
                "kind": "exact_release_crc",
            }
        ],
        "timeline_boundaries": [],
        "timeline_exact_release_guard": {
            "applied": True,
            "reason": "matching_release_crc",
            "crc": crc.upper(),
            "video_name": video.name,
            "subtitle_name": subtitle.name,
        },
        "timeline_early_edit_audio_verification": {
            "required": False,
            "reason": "exact_release_crc",
            "reasons": [],
        },
    }


def _candidate_has_exact_anilist_identity(candidate: SubtitleCandidate) -> bool:
    """Recover exact AniList identity from persisted primary IDs.

    Live Jimaku candidates normally carry the derived ``entry_anilist_match``
    boolean. Stored benchmark candidates predate/persist only the primary IDs,
    so replay must not lose exact identity merely because that redundant flag is
    absent. Equal explicit IDs are equivalent evidence.
    """
    details = candidate.details if isinstance(candidate.details, dict) else {}
    if details.get("entry_anilist_match") is True:
        return True
    entry_id = details.get("entry_anilist_id")
    requested_id = details.get("requested_anilist_id")
    if entry_id in {None, ""} or requested_id in {None, ""}:
        return False
    try:
        return int(entry_id) == int(requested_id)
    except (TypeError, ValueError):
        return False


def _candidate_explicit_anilist_mismatch(candidate: SubtitleCandidate) -> bool:
    details = candidate.details if isinstance(candidate.details, dict) else {}
    # Narrow stale-parent exception: a separately linked special/movie may use
    # another AniList ID when its entry title exactly matches the opened video.
    if bool(details.get("entry_identity_exact_title_match")) and (
        details.get("requested_episode") in {None, ""}
        or bool(details.get("single_special_exact_entry"))
        or bool(details.get("exact_anilist_movie_entry"))
    ):
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
    # Previous cache generation: syncing-v0.3.43-preop-embedded-reference-refine
    video_stat = video.stat()
    subtitle_stat = subtitle.stat()
    raw = (
        f"syncing-v0.3.46-tri-modal-cold-open:{tag}:"
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


def _timeline_needs_audio_verification(result: dict[str, object]) -> bool:
    risk = result.get("timeline_early_edit_audio_verification")
    return isinstance(risk, dict) and bool(risk.get("required"))


def _salvage_sparse_preopening_timeline_attempt(
    result: dict[str, object],
) -> dict[str, object] | None:
    """Recover only a sparse early clock hint from an otherwise unstable path.

    Some exact-release subtitles have a very short cold open on a different
    clock, followed by a long opening and a stable main episode.  The generic
    timeline solver correctly rejects the full path because dialogue-dense
    scenes can create huge transient aliases, but the first early window and
    the dominant near-zero main clock can still be useful as a *trigger* for
    Japanese STT verification.  This helper never makes the rejected timeline
    authoritative; it only exposes two conservative clock plateaus so the
    dual-reference opening scaffold can verify the sparse cold open later.
    """
    if str(result.get("reason") or "") != "timeline_unstable_segments":
        return None
    path = result.get("path")
    if not isinstance(path, list) or len(path) < 12:
        return None

    rows: list[dict[str, float | int]] = []
    for raw in path:
        if not isinstance(raw, dict):
            continue
        try:
            rows.append(
                {
                    "center": float(raw.get("center")),
                    "offset": float(raw.get("offset_seconds")),
                    "score": float(raw.get("score") or 0.0),
                    "coverage": float(raw.get("onset_coverage") or 0.0),
                    "matched": int(raw.get("matched_onsets") or 0),
                }
            )
        except (TypeError, ValueError):
            continue
    if len(rows) < 12:
        return None

    early = next(
        (
            row
            for row in rows
            if float(row["center"]) <= 90.0
            and 4.0 <= abs(float(row["offset"])) <= 12.0
            and float(row["score"]) >= 2.50
            and float(row["coverage"]) >= 0.70
            and int(row["matched"]) >= 6
        ),
        None,
    )
    if early is None:
        return None

    # Only salvage the common near-zero main clock.  Large global offsets (for
    # example another broadcast master) stay on the normal ALASS path.
    main_pool = [
        row
        for row in rows
        if float(row["center"]) >= 120.0
        and abs(float(row["offset"])) <= 8.0
        and float(row["score"]) >= 3.0
        and float(row["coverage"]) >= 0.75
    ]
    if len(main_pool) < 12:
        return None
    main_median = float(statistics.median(float(row["offset"]) for row in main_pool))
    main_inliers = [
        row
        for row in main_pool
        if abs(float(row["offset"]) - main_median) <= 3.0
    ]
    if len(main_inliers) < 12:
        return None

    post_offset = float(statistics.median(float(row["offset"]) for row in main_inliers))
    clock_delta = float(early["offset"]) - post_offset
    if not (4.0 <= abs(clock_delta) <= 12.0):
        return None

    # Require the unstable path to reacquire the main clock soon after the
    # early probe.  This rejects cases where the whole episode is simply on a
    # different global clock.
    reacquired = any(
        90.0 <= float(row["center"]) <= 190.0
        and abs(float(row["offset"]) - post_offset) <= 3.0
        for row in rows
    )
    if not reacquired:
        return None

    mean_main_score = statistics.fmean(float(row["score"]) for row in main_inliers)
    mean_main_coverage = statistics.fmean(float(row["coverage"]) for row in main_inliers)
    if mean_main_score < 3.15 or mean_main_coverage < 0.86:
        return None

    payload = dict(result)
    salvage = {
        "accepted": True,
        "reason": "unstable_path_sparse_preopening_signal",
        "early_center_seconds": round(float(early["center"]), 3),
        "early_offset_seconds": round(float(early["offset"]), 3),
        "early_score": round(float(early["score"]), 4),
        "early_coverage": round(float(early["coverage"]), 4),
        "early_matched_onsets": int(early["matched"]),
        "post_offset_seconds": round(post_offset, 3),
        "post_support": len(main_inliers),
        "post_mean_score": round(mean_main_score, 4),
        "post_mean_coverage": round(mean_main_coverage, 4),
        "clock_delta_seconds": round(clock_delta, 3),
    }
    payload.update(
        {
            "timeline_unstable_sparse_preopening_salvage": salvage,
            "timeline_segments": [
                {
                    "offset_seconds": round(float(early["offset"]), 3),
                    "support": 1,
                    "mean_score": round(float(early["score"]), 4),
                    "mean_coverage": round(float(early["coverage"]), 4),
                    "kind": "stable",
                },
                {
                    "offset_seconds": round(post_offset, 3),
                    "support": len(main_inliers),
                    "mean_score": round(mean_main_score, 4),
                    "mean_coverage": round(mean_main_coverage, 4),
                    "kind": "stable",
                },
            ],
            "timeline_early_edit_audio_verification": {
                "required": True,
                "reasons": [
                    "early_path_clock_change",
                    "unstable_path_sparse_preopening_salvage",
                ],
                "early_offset_span_seconds": round(abs(clock_delta), 3),
                "early_max_jump_seconds": round(abs(clock_delta), 3),
            },
        }
    )
    return payload


def _prefer_embedded_timeline_over_conflicting_speech(
    timeline_result: dict[str, object],
    speech_result: dict[str, object],
    opening_scaffold: dict[str, object],
) -> tuple[bool, dict[str, object]]:
    """Prefer a strongly validated same-video timeline when STT lands on another clock.

    This is deliberately conservative: it only fires for the exact failure mode
    where opening-scaffold repair refused because the Japanese speech clock and
    the dominant post-edit embedded timeline disagree by a large amount, while
    independent timeline holdouts are tight.
    """
    scaffold_conflict = (
        str(opening_scaffold.get("reason_detail") or "")
        == "speech_clock_disagrees_with_post_plateau"
    )
    opening_reacquire = timeline_result.get("timeline_opening_gap_reacquire")
    reacquired_clock = bool(
        isinstance(opening_reacquire, dict)
        and opening_reacquire.get("applied")
        and str(opening_reacquire.get("reason") or "")
        == "dominant_clock_reacquired_after_opening_gap"
    )
    if not scaffold_conflict and not reacquired_clock:
        return False, {"reason": "no_speech_clock_conflict"}

    segments = timeline_result.get("timeline_segments")
    validation = timeline_result.get("timeline_validation")
    if not isinstance(segments, list) or not isinstance(validation, dict):
        return False, {"reason": "timeline_metrics_unavailable"}

    stable = [
        row
        for row in segments
        if isinstance(row, dict)
        and str(row.get("kind") or "stable") in {"stable", "post_opening_reacquire"}
    ]
    if len(stable) < 2:
        return False, {"reason": "timeline_not_piecewise"}
    post = max(stable[1:], key=lambda row: max(0, int(row.get("support") or 0)))
    holdout = validation.get("holdout")
    after = validation.get("after")
    if not isinstance(holdout, dict) or not isinstance(after, dict):
        return False, {"reason": "timeline_validation_incomplete"}

    try:
        speech_offset = float(speech_result.get("offset_seconds"))
        post_offset = float(post.get("offset_seconds"))
        post_support = max(0, int(post.get("support") or 0))
        after_f1 = float(after.get("f1") or 0.0)
        activity_f1 = float(validation.get("activity_f1") or 0.0)
        holdout_p90 = float(holdout.get("p90_abs_residual_seconds"))
        holdout_coverage = float(holdout.get("mean_coverage") or 0.0)
    except (TypeError, ValueError):
        return False, {"reason": "timeline_validation_not_numeric"}

    conflict = abs(speech_offset - post_offset)
    strong = bool(
        conflict >= 8.0
        and post_support >= 8
        and after_f1 >= 0.82
        and activity_f1 >= 0.85
        and holdout_p90 <= 1.0
        and holdout_coverage >= 0.85
    )
    return strong, {
        "reason": "strong_embedded_timeline" if strong else "embedded_timeline_not_strong_enough",
        "speech_offset_seconds": round(speech_offset, 3),
        "post_offset_seconds": round(post_offset, 3),
        "clock_conflict_seconds": round(conflict, 3),
        "post_support": post_support,
        "after_f1": round(after_f1, 4),
        "activity_f1": round(activity_f1, 4),
        "holdout_p90_seconds": round(holdout_p90, 4),
        "holdout_mean_coverage": round(holdout_coverage, 4),
    }


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
        if any(marker in lowered for marker in ("cr", "dialog", "dialogue", "full")):
            score += 15.0
        if any(marker in lowered for marker in ("commentary", "comment", "comms")) or int(disposition.get("comment", 0) or 0):
            score -= 200.0
        if any(marker in lowered for marker in ("forced", "sign", "song", "karaoke")) or int(disposition.get("forced", 0) or 0):
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
        try:
            cached_cues = parse_srt(output)
        except (OSError, UnicodeError, ValueError):
            cached_cues = []
        if cached_cues:
            return output, _result(
                "cached",
                output=str(output),
                stream_index=index,
                language=language,
                title=title,
            )
        output.unlink(missing_ok=True)
    # Write beside the cache target and publish atomically.  An interrupted
    # ffmpeg must never leave a truncated file that a later run treats as a hit.
    temp_output = output.with_name(f".{output.stem}.{os.getpid()}.{time.time_ns()}.tmp.srt")
    command = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-nostdin",
        "-probesize",
        "10M",
        "-analyzeduration",
        "10000000",
        "-i",
        str(video),
        "-map",
        f"0:{index}",
        "-c:s",
        "srt",
        str(temp_output),
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
        valid_cues = parse_srt(temp_output) if temp_output.exists() and temp_output.stat().st_size > 0 else []
        if extracted.returncode != 0 or not valid_cues:
            return None, _result(
                "timing_reference_extract_failed",
                error=extracted.stdout[-1000:] if verbose else "ffmpeg не извлёк валидную текстовую дорожку",
            )
        os.replace(temp_output, output)
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError) as exc:
        return None, _result("timing_reference_extract_failed", error=str(exc))
    finally:
        temp_output.unlink(missing_ok=True)
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
            f"{start:.3f}:{duration:.3f}:{padding:.3f}:mono-16k-flac-v3"
        ).encode()
    ).hexdigest()[:20]
    output_dir = cache_dir / "segment-audio"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{digest}.flac"
    # This audio is content-addressed by every input that affects its samples.
    # A forced subtitle resync therefore does not need to re-run ffmpeg here.
    # Reusing it avoids repeatedly decoding the same episode.
    cleanup_segment_audio_cache(cache_dir)
    if output.exists() and output.stat().st_size > 0:
        touch_segment_audio(output)
        return output, None
    output.unlink(missing_ok=True)
    temporary = output.with_suffix(".flac.tmp")
    temporary.unlink(missing_ok=True)
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
    # Long local-offset windows are mostly silence. FLAC makes those caches tiny
    # compared with PCM WAV while remaining lossless for ffsubsync.
    command.extend(["-c:a", "flac", "-compression_level", "0", "-f", "flac", str(temporary)])
    mark_segment_audio_active(output, True)
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
        temporary.unlink(missing_ok=True)
        return None, str(exc)
    finally:
        mark_segment_audio_active(output, False)
    if completed.returncode != 0 or not temporary.exists() or temporary.stat().st_size == 0:
        temporary.unlink(missing_ok=True)
        return None, (completed.stdout or "ffmpeg_audio_extract_failed")[-1000:]
    temporary.replace(output)
    touch_segment_audio(output)
    cleanup_segment_audio_cache(cache_dir)
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
        score_improvement = float(window.get("score_improvement") or 0.0)
        has_detailed_onset_diagnostics = (
            source_onsets > 0
            and reference_onsets > 0
            and ("first_edge_error" in window or "last_edge_error" in window)
        )
        # Do not treat every sub-8s opening shift as trustworthy.  Sparse or
        # repetitive cold opens can produce a plausible 2-7s alias with only
        # four matched onsets and a terrible boundary error.  Small residual
        # corrections still get a cheap pass; anything large enough to be
        # noticeable during playback must also have coherent edge evidence.
        large_shift_is_strong = (
            offset <= 1.5
            or (offset <= 8.0 and not has_detailed_onset_diagnostics)
            or (
                offset <= 8.0
                and coverage >= 0.60
                and onset_ratio >= 0.60
                and matched >= 4
                and edge_error <= 2.5
                and score_improvement >= 0.10
            )
            or (
                offset > 8.0
                and coverage >= 0.30
                and onset_ratio >= 0.58
                and matched >= 6
                and edge_error <= 5.0
                and score_improvement >= 0.20
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

    # A broad opening window can rediscover exactly the alias rejected above.
    # This happened on Hyakkano S03E08: the short 0-35s +9.6s probe was rejected
    # as weak, then the 0-134s broad window reintroduced +9.6s as an anchor and
    # distorted every pre-opening cue.  If an early broad estimate mirrors a
    # rejected cold estimate but disagrees with the next broad windows, treat it
    # as contaminated by the cold-open cadence rather than independent evidence.
    contaminated_broad_times: set[float] = set()
    if weak_large_cold_probes and broad_records:
        weak_offsets = [float(item["offset_seconds"]) for item in weak_large_cold_probes]
        for index, (timepoint, offset, _kind) in enumerate(broad_records):
            if timepoint > 120.0:
                continue
            if not any(abs(offset - weak_offset) <= 1.0 for weak_offset in weak_offsets):
                continue
            later_offsets = [
                later_offset
                for later_time, later_offset, _later_kind in broad_records[index + 1 : index + 4]
                if later_time >= timepoint + 45.0
            ]
            if not later_offsets:
                continue
            later_median = float(statistics.median(later_offsets))
            if abs(offset - later_median) > max(2.5, float(config.piecewise_jump_threshold_seconds)):
                contaminated_broad_times.add(round(float(timepoint), 3))

    trusted_broad_records = [
        record
        for record in broad_records
        if round(float(record[0]), 3) not in contaminated_broad_times
    ]

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
        rounded_time = round(float(timepoint), 3)
        if kind == "cold" and rounded_time in weak_large_cold_times:
            continue
        if kind == "broad" and rounded_time in contaminated_broad_times:
            continue
        # Two agreeing short cold-open probes are more trustworthy than a broad
        # 0-150s window that straddles a real edit point. Do not overwrite that
        # cluster with the later, already-correct episode trend.
        if kind == "cold" and cold_cluster_offset is not None:
            offset = cold_cluster_offset
        elif kind == "transition" and trusted_broad_records:
            nearest_time, nearest_offset, _ = min(
                trusted_broad_records,
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
            contaminated_broad_times=sorted(contaminated_broad_times),
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
            contaminated_broad_times=sorted(contaminated_broad_times),
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
    large_early_correction = any(
        timepoint <= 120.0 and abs(offset) >= 2.5 for timepoint, offset in anchors
    )
    cold_absolute_quality_ok = (
        not large_early_correction
        or float(after_cold.get("start") or 0.0) >= 0.72
        or cold_start_gain >= 0.15
    )
    accepted = (
        middle_loss <= 0.025
        and cold_absolute_quality_ok
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
            large_early_correction=large_early_correction,
            cold_absolute_quality_ok=cold_absolute_quality_ok,
            contaminated_broad_times=sorted(contaminated_broad_times),
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
        large_early_correction=large_early_correction,
        cold_absolute_quality_ok=cold_absolute_quality_ok,
        contaminated_broad_times=sorted(contaminated_broad_times),
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



def _stt_alass_transition_safety(
    source: Path,
    aligned: Path,
) -> dict[str, object]:
    """Describe whether large ALASS clock jumps sit on real subtitle gaps."""
    try:
        source_cues = parse_srt(source)
        aligned_cues = parse_srt(aligned)
    except OSError as exc:
        return {
            "available": False,
            "accepted": False,
            "reason": "read_error",
            "error": str(exc),
        }
    if not source_cues or len(source_cues) != len(aligned_cues):
        return {
            "available": False,
            "accepted": False,
            "reason": "structure_mismatch",
            "source_cues": len(source_cues),
            "aligned_cues": len(aligned_cues),
        }

    shifts = [
        float(aligned_start) - float(source_start)
        for (source_start, _source_end, _text), (aligned_start, _aligned_end, _aligned_text)
        in zip(source_cues, aligned_cues)
    ]
    transitions: list[dict[str, object]] = []

    def nearby_gap(index: int) -> tuple[float, float]:
        best_gap = 0.0
        best_time = 0.0
        lo = max(1, index - 4)
        hi = min(len(source_cues), index + 5)
        for cue_index in range(lo, hi):
            previous_end = float(source_cues[cue_index - 1][1])
            current_start = float(source_cues[cue_index][0])
            gap = max(0.0, current_start - previous_end)
            if gap > best_gap:
                best_gap = gap
                best_time = (previous_end + current_start) / 2.0
        return best_gap, best_time

    for index in range(1, len(shifts)):
        jump = float(shifts[index] - shifts[index - 1])
        if abs(jump) < 4.0:
            continue
        gap, gap_time = nearby_gap(index)
        transitions.append(
            {
                "cue_index": index,
                "source_time": round(float(source_cues[index][0]), 3),
                "jump_seconds": round(jump, 3),
                "nearby_gap_seconds": round(gap, 3),
                "nearby_gap_time": round(gap_time, 3),
                "gap_supported": gap >= 30.0,
            }
        )

    unsupported = [row for row in transitions if not bool(row["gap_supported"])]
    transient_excursions: list[dict[str, object]] = []
    for left_index, left in enumerate(transitions):
        try:
            left_jump = float(left.get("jump_seconds") or 0.0)
            left_time = float(left.get("source_time") or 0.0)
        except (TypeError, ValueError):
            continue
        if abs(left_jump) < 20.0:
            continue
        for right in transitions[left_index + 1 :]:
            try:
                right_jump = float(right.get("jump_seconds") or 0.0)
                right_time = float(right.get("source_time") or 0.0)
            except (TypeError, ValueError):
                continue
            duration = right_time - left_time
            if duration < 12.0 or duration > 12 * 60.0:
                continue
            if left_jump * right_jump >= 0.0:
                continue
            # A large excursion which later returns to almost the exact prior
            # clock is especially suspicious when one edge does not coincide
            # with a real subtitle gap. Bleach TYBW E46 exposed this shape:
            # ALASS inserted a temporary +86s island around the ending and then
            # returned by -86s, even though the correct clock stayed continuous.
            return_error = abs(left_jump + right_jump)
            return_tolerance = max(2.0, 0.08 * max(abs(left_jump), abs(right_jump)))
            if return_error > return_tolerance:
                continue
            if bool(left.get("gap_supported")) and bool(right.get("gap_supported")):
                continue
            transient_excursions.append(
                {
                    "start_source_time": round(left_time, 3),
                    "end_source_time": round(right_time, 3),
                    "duration_seconds": round(duration, 3),
                    "excursion_seconds": round(left_jump, 3),
                    "return_jump_seconds": round(right_jump, 3),
                    "return_error_seconds": round(return_error, 3),
                }
            )
            break

    return {
        "available": True,
        "accepted": not unsupported,
        "reason": "ok" if not unsupported else "large_transition_without_real_gap",
        "large_transition_count": len(transitions),
        "unsupported_transition_count": len(unsupported),
        "transient_excursion_count": len(transient_excursions),
        "transient_excursions": transient_excursions,
        "transitions": transitions,
    }


def _stt_alass_map_safe(
    result: dict[str, object],
    transition_safety: dict[str, object],
) -> tuple[bool, str]:
    try:
        blocks = int(result.get("alass_blocks") or 0)
        spread = abs(float(result.get("alass_shift_spread_seconds") or 0.0))
    except (TypeError, ValueError):
        return False, "stt_alass_metrics_unavailable"

    # A large insert/remove around an OP should normally produce one clock jump
    # (two plateaus). Three or more ALASS blocks with a huge spread is a classic
    # false attachment: dialogue from after the OP gets pulled into the OP.
    if blocks >= 3 and spread >= 20.0:
        return False, "stt_alass_fragmented_large_edit"

    # Do not trust the aggregate ALASS spread alone. A map can report a modest
    # spread while the actual cue-to-cue mapping contains a huge temporary
    # excursion (for example +86s followed by -86s around an ending). That was
    # the Bleach TYBW E46 failure: the safety probe found the bad transition, but
    # this gate ignored it because alass_shift_spread_seconds was only ~10s.
    max_unsupported_jump = 0.0
    transitions = transition_safety.get("transitions")
    if isinstance(transitions, list):
        for row in transitions:
            if not isinstance(row, dict) or bool(row.get("gap_supported")):
                continue
            try:
                max_unsupported_jump = max(
                    max_unsupported_jump,
                    abs(float(row.get("jump_seconds") or 0.0)),
                )
            except (TypeError, ValueError):
                continue
    if max_unsupported_jump >= 20.0:
        return False, str(
            transition_safety.get("reason")
            or "stt_alass_large_edit_without_gap_support"
        )

    if spread >= 20.0 and not bool(transition_safety.get("accepted")):
        return False, str(
            transition_safety.get("reason")
            or "stt_alass_large_edit_without_gap_support"
        )

    return True, "ok"


def _strong_embedded_timeline_after_unsafe_stt(
    timeline_result: dict[str, object],
    speech_result: dict[str, object],
) -> tuple[bool, dict[str, object]]:
    """Allow a strong same-video timeline when every STT+ALASS map is unsafe."""

    if str(speech_result.get("reason") or "") != "stt_alass_no_safe_map":
        return False, {"reason": "speech_failure_not_map_safety"}

    attempts = speech_result.get("stt_alass_attempts")
    if not isinstance(attempts, list):
        return False, {"reason": "stt_attempts_unavailable"}

    largest_unsupported_jump = 0.0
    transient_excursions = 0
    unsafe_attempts = 0
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        safety = attempt.get("transition_safety")
        if not isinstance(safety, dict) or bool(safety.get("accepted")):
            continue
        unsafe_attempts += 1
        try:
            transient_excursions += int(safety.get("transient_excursion_count") or 0)
        except (TypeError, ValueError):
            pass
        rows = safety.get("transitions")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or bool(row.get("gap_supported")):
                continue
            try:
                largest_unsupported_jump = max(
                    largest_unsupported_jump,
                    abs(float(row.get("jump_seconds") or 0.0)),
                )
            except (TypeError, ValueError):
                continue

    if unsafe_attempts <= 0 or largest_unsupported_jump < 20.0:
        return False, {
            "reason": "no_large_unsafe_stt_transition",
            "unsafe_attempts": unsafe_attempts,
            "largest_unsupported_jump_seconds": round(largest_unsupported_jump, 3),
        }

    validation = timeline_result.get("timeline_validation")
    segments = timeline_result.get("timeline_segments")
    if not isinstance(validation, dict) or not isinstance(segments, list):
        return False, {"reason": "timeline_metrics_unavailable"}
    after = validation.get("after")
    holdout = validation.get("holdout")
    if not isinstance(after, dict) or not isinstance(holdout, dict):
        return False, {"reason": "timeline_validation_incomplete"}
    try:
        after_f1 = float(after.get("f1") or 0.0)
        activity_f1 = float(validation.get("activity_f1") or 0.0)
        holdout_p90 = float(holdout.get("p90_abs_residual_seconds"))
        holdout_coverage = float(holdout.get("mean_coverage") or 0.0)
        dominant_support = max(
            (int(row.get("support") or 0) for row in segments if isinstance(row, dict)),
            default=0,
        )
    except (TypeError, ValueError):
        return False, {"reason": "timeline_validation_not_numeric"}

    strong = bool(
        dominant_support >= 8
        and after_f1 >= 0.82
        and activity_f1 >= 0.85
        and holdout_p90 <= 1.0
        and holdout_coverage >= 0.85
    )
    return strong, {
        "reason": "strong_embedded_timeline_after_unsafe_stt" if strong else "embedded_timeline_not_strong_enough",
        "unsafe_attempts": unsafe_attempts,
        "transient_excursions": transient_excursions,
        "largest_unsupported_jump_seconds": round(largest_unsupported_jump, 3),
        "timeline_after_f1": round(after_f1, 4),
        "timeline_activity_f1": round(activity_f1, 4),
        "timeline_holdout_p90_seconds": round(holdout_p90, 4),
        "timeline_holdout_coverage": round(holdout_coverage, 4),
        "timeline_dominant_support": dominant_support,
    }


def _nearest_reference_error(
    timestamp: float,
    reference_starts: list[float],
) -> float:
    if not reference_starts:
        return float("inf")
    best = float("inf")
    for value in reference_starts:
        error = abs(value - timestamp)
        if error < best:
            best = error
        if value > timestamp and error > best:
            break
    return best


def _local_speech_shift_estimate(
    starts: list[float],
    reference_starts: list[float],
    *,
    max_shift_seconds: float = 8.0,
) -> dict[str, object]:
    if len(starts) < 4 or len(reference_starts) < 4:
        return {
            "accepted": False,
            "reason": "too_few_onsets",
            "shift_seconds": 0.0,
        }

    ordered_starts = sorted(float(value) for value in starts)
    ordered_reference = sorted(float(value) for value in reference_starts)

    def evaluate(shift: float) -> dict[str, float | int]:
        shifted = [value + shift for value in ordered_starts]
        # Sequence alignment instead of independent nearest-neighbour matching.
        # A Whisper segment can explain at most one subtitle cue and cue order
        # must be preserved, which prevents dense speech from manufacturing a
        # convincing but wrong cold-open offset.
        previous: list[tuple[int, float]] = [
            (0, 0.0) for _ in range(len(ordered_reference) + 1)
        ]
        for source_time in shifted:
            current: list[tuple[int, float]] = [(0, 0.0)]
            for ref_index, reference_time in enumerate(ordered_reference, start=1):
                best = previous[ref_index]
                if current[ref_index - 1] > best:
                    best = current[ref_index - 1]
                error = abs(source_time - reference_time)
                if error <= 0.90:
                    matched, neg_error = previous[ref_index - 1]
                    candidate = (matched + 1, neg_error - error)
                    if candidate > best:
                        best = candidate
                current.append(best)
            previous = current

        matched, neg_error = previous[-1]
        mean_error = (-neg_error / matched) if matched else float("inf")
        return {
            "matched": matched,
            "coverage": matched / max(1, len(ordered_starts)),
            "mean_error": mean_error,
        }

    baseline = evaluate(0.0)
    candidates: list[tuple[tuple[float, ...], float, dict[str, float | int]]] = []
    steps = int(round(max_shift_seconds * 10.0))
    for step in range(-steps, steps + 1):
        shift = step / 10.0
        metrics = evaluate(shift)
        matched = int(metrics["matched"])
        coverage = float(metrics["coverage"])
        mean_error = float(metrics["mean_error"])
        rank = (
            float(matched),
            coverage,
            -mean_error if math.isfinite(mean_error) else -999.0,
            -abs(shift),
        )
        candidates.append((rank, shift, metrics))

    candidates.sort(key=lambda item: item[0], reverse=True)
    _rank, best_shift, best = candidates[0]

    baseline_matched = int(baseline["matched"])
    best_matched = int(best["matched"])
    baseline_error = float(baseline["mean_error"])
    best_error = float(best["mean_error"])
    matched_gain = best_matched - baseline_matched
    error_gain = (
        baseline_error - best_error
        if math.isfinite(baseline_error) and math.isfinite(best_error)
        else 0.0
    )

    accepted = bool(
        abs(best_shift) >= 0.20
        and float(best["coverage"]) >= 0.45
        and (
            matched_gain >= 2
            or (
                best_matched >= max(4, baseline_matched - 1)
                and error_gain >= 0.12
            )
        )
    )
    return {
        "accepted": accepted,
        "reason": "improved" if accepted else "no_clear_improvement",
        "shift_seconds": round(float(best_shift), 3) if accepted else 0.0,
        # Keep the best candidate visible even when the generic gate rejects it.
        # A later caller may have an independent signal that can safely confirm
        # this otherwise-borderline local estimate.
        "best_shift_seconds": round(float(best_shift), 3),
        "baseline": {
            "matched": baseline_matched,
            "coverage": round(float(baseline["coverage"]), 4),
            "mean_error_seconds": (
                round(baseline_error, 4) if math.isfinite(baseline_error) else None
            ),
        },
        "best": {
            "matched": best_matched,
            "coverage": round(float(best["coverage"]), 4),
            "mean_error_seconds": (
                round(best_error, 4) if math.isfinite(best_error) else None
            ),
        },
        "matched_gain": matched_gain,
        "mean_error_gain_seconds": round(error_gain, 4),
        "matching": "monotonic_one_to_one",
    }



def _refine_sparse_preopening_cues(
    cues: list[tuple[float, float, str]],
    split_index: int,
    speech_result: dict[str, object],
    embedded_reference: Path | None,
    *,
    max_adjust_seconds: float = 2.5,
    preferred_shift_seconds: float | None = None,
    speech_reference_clock_adjust_seconds: float = 0.0,
) -> tuple[list[tuple[float, float, str]], dict[str, object]]:
    """Micro-adjust a handful of cold-open cues using two independent clocks.

    This is deliberately limited to sparse cold opens.  A cue moves only when
    the exact embedded subtitle and Japanese STT each expose a nearby onset and
    those two targets agree.  Durations are preserved and the whole refinement
    is rejected if it would reorder cues.
    """
    sparse_limit = 10 if preferred_shift_seconds is not None else 6
    if split_index < 1 or split_index > sparse_limit:
        return cues, {"attempted": False, "accepted": False, "reason": "not_sparse"}
    if embedded_reference is None or not Path(embedded_reference).is_file():
        return cues, {"attempted": False, "accepted": False, "reason": "embedded_reference_unavailable"}
    speech_path_raw = speech_result.get("timing_reference")
    speech_path = Path(str(speech_path_raw)).expanduser() if speech_path_raw not in {None, ""} else None
    if speech_path is None or not speech_path.is_file():
        return cues, {"attempted": False, "accepted": False, "reason": "speech_reference_unavailable"}
    try:
        embedded_cues = parse_srt(Path(embedded_reference))
        speech_cues = parse_srt(speech_path)
    except OSError as exc:
        return cues, {"attempted": True, "accepted": False, "reason": "reference_read_error", "error": str(exc)}

    embedded_starts = sorted(float(start) for start, _end, _text in embedded_cues)
    speech_clock_adjust = float(speech_reference_clock_adjust_seconds)
    speech_starts = sorted(
        float(start) + speech_clock_adjust
        for start, _end, _text in speech_cues
    )
    if len(embedded_starts) < 2 or len(speech_starts) < 2:
        return cues, {"attempted": True, "accepted": False, "reason": "too_few_reference_onsets"}

    nearest_limit = max(0.5, float(max_adjust_seconds) + 0.1)

    def matching_reference_pair(value: float) -> tuple[float, float] | None:
        embedded_near = [
            candidate
            for candidate in embedded_starts
            if abs(candidate - value) <= nearest_limit
        ]
        speech_near = [
            candidate
            for candidate in speech_starts
            if abs(candidate - value) <= nearest_limit
        ]
        pairs = [
            (embedded_target, speech_target)
            for embedded_target in embedded_near
            for speech_target in speech_near
            if abs(embedded_target - speech_target) <= 0.90
        ]
        if not pairs:
            return None
        if preferred_shift_seconds is None:
            return min(
                pairs,
                key=lambda pair: (
                    abs(((pair[0] + pair[1]) / 2.0) - value),
                    abs(pair[0] - pair[1]),
                ),
            )
        preferred = float(preferred_shift_seconds)
        return min(
            pairs,
            key=lambda pair: (
                abs((((pair[0] + pair[1]) / 2.0) - value) - preferred),
                abs(pair[0] - pair[1]),
                abs(((pair[0] + pair[1]) / 2.0) - value),
            ),
        )

    proposals: dict[int, tuple[float, float, float, float]] = {}
    before_errors: list[float] = []
    after_errors: list[float] = []
    for index, (start, end, cue_text) in enumerate(cues[:split_index]):
        if _is_reference_non_dialogue(cue_text):
            continue
        start = float(start)
        end = float(end)
        pair = matching_reference_pair(start)
        if pair is None:
            continue
        embedded_target, speech_target = pair
        target = (embedded_target + speech_target) / 2.0
        delta = target - start
        if abs(delta) > float(max_adjust_seconds):
            continue
        before = (abs(start - embedded_target) + abs(start - speech_target)) / 2.0
        after = (abs(target - embedded_target) + abs(target - speech_target)) / 2.0
        proposals[index] = (start + delta, end + delta, delta, target)
        before_errors.append(before)
        after_errors.append(after)

    # A very short cold open can contain only two spoken subtitle cues plus
    # signs/SFX cues that neither speech clock can confirm. Requiring proposals
    # for 60% of every cue then averages two real speech clocks into one coarse
    # correction. Recover the common two-plateau shape instead: the first
    # independently confirmed spoken cue keeps its own residual, while the
    # stable clock from the next confirmed cue is held until the opening gap.
    # Post-opening cues are never touched here.
    proposal_indices = sorted(proposals)
    first_proposal_index = proposal_indices[0] if proposal_indices else None
    later_indices = [
        index
        for index in proposal_indices
        if first_proposal_index is not None and index > first_proposal_index
    ]
    if first_proposal_index is not None and later_indices:
        first_delta = float(proposals[first_proposal_index][2])
        later_deltas = [float(proposals[index][2]) for index in later_indices]
        later_delta = float(statistics.median(later_deltas))
        later_dispersion = max(
            abs(value - later_delta) for value in later_deltas
        )
        first_later_jump = abs(first_delta - later_delta)
        first_later_errors_before = [
            before_errors[proposal_indices.index(first_proposal_index)]
        ]
        first_later_errors_after = [
            after_errors[proposal_indices.index(first_proposal_index)]
        ]
        for index in later_indices:
            position = proposal_indices.index(index)
            first_later_errors_before.append(before_errors[position])
            first_later_errors_after.append(after_errors[position])
        plateau_before = statistics.mean(first_later_errors_before)
        plateau_after = statistics.mean(first_later_errors_after)
        plateau_gain = plateau_before - plateau_after
        plateau_start = later_indices[0]

        if (
            first_later_jump >= 0.35
            and later_dispersion <= 0.35
            and plateau_gain >= 0.18
            and plateau_after <= 0.50
        ):
            plateau_refined = list(cues)
            for index, (start, end, cue_text) in enumerate(cues[:split_index]):
                shift = (
                    0.0
                    if index < first_proposal_index
                    else first_delta
                    if index < plateau_start
                    else later_delta
                )
                plateau_refined[index] = (
                    float(start) + shift,
                    float(end) + shift,
                    cue_text,
                )
            starts = [float(start) for start, _end, _text in plateau_refined]
            timestamps_valid = all(
                start >= 0.0 and end > start
                for start, end, _text in plateau_refined
            )
            ordered = not any(
                current + 1e-6 < previous
                for previous, current in zip(starts, starts[1:])
            )
            opening_boundary_safe = bool(
                split_index >= len(plateau_refined)
                or split_index <= 0
                or float(plateau_refined[split_index - 1][1])
                <= float(plateau_refined[split_index][0]) - 0.10
            )
            if timestamps_valid and ordered and opening_boundary_safe:
                return plateau_refined, {
                    "attempted": True,
                    "accepted": True,
                    "reason": "dual_reference_sparse_two_plateaus",
                    "strategy": "first_cue_then_preopening_plateau",
                    "matched": len(proposals),
                    "first_cue_index": first_proposal_index,
                    "plateau_start_cue_index": plateau_start,
                    "plateau_support": len(later_indices),
                    "first_cue_shift_seconds": round(first_delta, 3),
                    "later_plateau_shift_seconds": round(later_delta, 3),
                    "first_later_jump_seconds": round(first_later_jump, 3),
                    "later_plateau_dispersion_seconds": round(
                        later_dispersion, 3
                    ),
                    "mean_before_error_seconds": round(plateau_before, 4),
                    "mean_after_error_seconds": round(plateau_after, 4),
                    "mean_error_gain_seconds": round(plateau_gain, 4),
                }

    required = max(2, math.ceil(split_index * 0.60))
    coverage = len(proposals) / max(1, split_index)
    mean_before = statistics.mean(before_errors) if before_errors else float("inf")
    mean_after = statistics.mean(after_errors) if after_errors else float("inf")
    gain = mean_before - mean_after
    if len(proposals) < required or coverage < 0.60 or gain < 0.18 or mean_after > 0.50:
        return cues, {
            "attempted": True,
            "accepted": False,
            "reason": "insufficient_dual_reference_support",
            "matched": len(proposals),
            "required": required,
            "coverage": round(coverage, 4),
            "mean_error_gain_seconds": round(gain if math.isfinite(gain) else 0.0, 4),
        }

    refined = list(cues)
    shifts: list[dict[str, object]] = []
    for index, (new_start, new_end, delta, target) in proposals.items():
        refined[index] = (max(0.0, new_start), max(0.05, new_end), refined[index][2])
        shifts.append({"cue_index": index, "shift_seconds": round(delta, 3), "target_seconds": round(target, 3)})

    starts = [float(start) for start, _end, _text in refined]
    if any(current + 1e-6 < previous for previous, current in zip(starts, starts[1:])):
        return cues, {
            "attempted": True,
            "accepted": False,
            "reason": "would_reorder_cues",
            "matched": len(proposals),
        }
    return refined, {
        "attempted": True,
        "accepted": True,
        "reason": "dual_reference_sparse_refinement",
        "matched": len(proposals),
        "coverage": round(coverage, 4),
        "mean_before_error_seconds": round(mean_before, 4),
        "mean_after_error_seconds": round(mean_after, 4),
        "mean_error_gain_seconds": round(gain, 4),
        "shifts": shifts,
    }


def _repair_salvaged_sparse_preop_after_bad_stt_map(
    baseline_aligned: Path,
    embedded_result: dict[str, object],
    speech_result: dict[str, object],
    embedded_reference: Path | None,
    cache_dir: Path,
) -> tuple[Path, dict[str, object]]:
    """Keep the embedded baseline when STT builds a contradictory huge map.

    For a salvaged sparse cold open, STT is only corroborating evidence. If its
    ALASS map invents a huge early clock jump that is incompatible with the
    same-video embedded clock hint, use raw STT/embedded onsets to refine only
    the pre-opening cues instead of accepting the bad STT map wholesale.
    """
    salvage = embedded_result.get("timeline_unstable_sparse_preopening_salvage")
    if not isinstance(salvage, dict) or not bool(salvage.get("accepted")):
        return baseline_aligned, {
            "applied": False,
            "reason": "salvaged_sparse_preop_unavailable",
        }

    safety = speech_result.get("stt_alass_transition_safety")
    transitions = safety.get("transitions") if isinstance(safety, dict) else None
    if not isinstance(transitions, list):
        return baseline_aligned, {
            "applied": False,
            "reason": "stt_transition_safety_unavailable",
        }

    try:
        expected_delta = float(salvage.get("clock_delta_seconds") or 0.0)
    except (TypeError, ValueError):
        expected_delta = 0.0
    if abs(expected_delta) < 3.0:
        return baseline_aligned, {
            "applied": False,
            "reason": "salvaged_clock_delta_too_small",
        }

    large_jumps: list[float] = []
    for row in transitions:
        if not isinstance(row, dict):
            continue
        try:
            jump = float(row.get("jump_seconds") or 0.0)
        except (TypeError, ValueError):
            continue
        if abs(jump) >= 20.0:
            large_jumps.append(jump)
    if not large_jumps:
        return baseline_aligned, {
            "applied": False,
            "reason": "stt_map_not_catastrophic",
        }
    largest_jump = max(large_jumps, key=abs)
    if abs(largest_jump) < max(30.0, abs(expected_delta) * 3.0):
        return baseline_aligned, {
            "applied": False,
            "reason": "stt_map_compatible_with_salvaged_clock",
            "expected_clock_delta_seconds": round(expected_delta, 3),
            "largest_stt_jump_seconds": round(largest_jump, 3),
        }

    try:
        cues = parse_srt(baseline_aligned)
    except OSError as exc:
        return baseline_aligned, {
            "applied": False,
            "reason": "baseline_read_error",
            "error": str(exc),
        }
    if len(cues) < 8:
        return baseline_aligned, {
            "applied": False,
            "reason": "too_few_baseline_cues",
        }

    origin = float(cues[0][0])
    gaps: list[tuple[float, int, float]] = []
    for index in range(1, len(cues)):
        previous_end = float(cues[index - 1][1])
        current_start = float(cues[index][0])
        gap = current_start - previous_end
        midpoint = (previous_end + current_start) / 2.0
        if gap >= 45.0 and midpoint - origin <= 240.0:
            gaps.append((gap, index, midpoint))
    if not gaps:
        return baseline_aligned, {
            "applied": False,
            "reason": "opening_gap_not_found",
            "conflict_detected": True,
            "expected_clock_delta_seconds": round(expected_delta, 3),
            "largest_stt_jump_seconds": round(largest_jump, 3),
        }

    _gap, split_index, gap_midpoint = max(gaps, key=lambda item: item[0])
    if not 1 <= split_index <= 10:
        return baseline_aligned, {
            "applied": False,
            "reason": "opening_not_sparse",
            "conflict_detected": True,
            "split_index": split_index,
            "expected_clock_delta_seconds": round(expected_delta, 3),
            "largest_stt_jump_seconds": round(largest_jump, 3),
        }

    try:
        post_offset = float(salvage.get("post_offset_seconds") or 0.0)
        speech_offset = float(speech_result.get("offset_seconds") or 0.0)
    except (TypeError, ValueError):
        post_offset = 0.0
        speech_offset = 0.0
    # The cached Japanese STT reference has its own absolute clock.  The ALASS
    # result tells us how that clock relates to the subtitle source, while the
    # salvaged embedded timeline gives us the stable post-opening video clock.
    # Normalize raw STT onsets onto that post-opening clock before asking the
    # two references to agree on sparse cold-open cues.  Without this, a real
    # multi-second STT clock bias makes otherwise identical 18.4s/29.8s speech
    # onsets look contradictory and the safe repair is incorrectly rejected.
    speech_reference_clock_adjust = -(speech_offset - post_offset)

    max_adjust = min(15.0, max(6.0, abs(expected_delta) + 5.0))
    refined, refinement = _refine_sparse_preopening_cues(
        cues,
        split_index,
        speech_result,
        embedded_reference,
        max_adjust_seconds=max_adjust,
        preferred_shift_seconds=expected_delta,
        speech_reference_clock_adjust_seconds=speech_reference_clock_adjust,
    )
    refinement = dict(refinement)
    refinement["speech_reference_clock_adjust_seconds"] = round(
        speech_reference_clock_adjust, 4
    )
    if not bool(refinement.get("accepted")):
        return baseline_aligned, {
            "applied": False,
            "reason": "dual_reference_refinement_failed",
            "conflict_detected": True,
            "expected_clock_delta_seconds": round(expected_delta, 3),
            "largest_stt_jump_seconds": round(largest_jump, 3),
            "opening_gap_midpoint_seconds": round(gap_midpoint, 3),
            "refinement": refinement,
        }

    shift_values: list[float] = []
    for key in ("first_cue_shift_seconds", "later_plateau_shift_seconds"):
        try:
            if refinement.get(key) is not None:
                shift_values.append(float(refinement[key]))
        except (TypeError, ValueError):
            pass
    shifts_payload = refinement.get("shifts")
    if not shift_values and isinstance(shifts_payload, list):
        for row in shifts_payload:
            if not isinstance(row, dict):
                continue
            try:
                shift_values.append(float(row.get("shift_seconds") or 0.0))
            except (TypeError, ValueError):
                continue

    same_direction = all(
        value == 0.0 or value * expected_delta > 0.0
        for value in shift_values
    )
    close_to_hint = bool(
        shift_values
        and all(abs(value - expected_delta) <= 4.0 for value in shift_values)
    )
    if not (same_direction and close_to_hint):
        return baseline_aligned, {
            "applied": False,
            "reason": "refinement_disagrees_with_salvaged_clock",
            "conflict_detected": True,
            "expected_clock_delta_seconds": round(expected_delta, 3),
            "largest_stt_jump_seconds": round(largest_jump, 3),
            "refinement_shift_seconds": [round(value, 3) for value in shift_values],
            "refinement": refinement,
        }

    stat = baseline_aligned.stat()
    signature = ",".join(f"{value:.3f}" for value in shift_values)
    digest = hashlib.sha1(
        (
            f"salvaged-sparse-preop-v1:{baseline_aligned.resolve()}:{stat.st_size}:"
            f"{stat.st_mtime_ns}:{expected_delta:.4f}:{largest_jump:.4f}:{signature}"
        ).encode()
    ).hexdigest()[:20]
    output_dir = cache_dir / "salvaged-sparse-preop"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{digest}.srt"
    if not output.exists() or output.stat().st_size <= 0:
        write_srt(refined, output, preserve_order=True)

    return output, {
        "applied": True,
        "reason": "salvaged_sparse_preop_dual_reference",
        "conflict_detected": True,
        "strategy": "embedded_baseline_plus_raw_dual_reference",
        "expected_clock_delta_seconds": round(expected_delta, 3),
        "largest_stt_jump_seconds": round(largest_jump, 3),
        "opening_gap_midpoint_seconds": round(gap_midpoint, 3),
        "split_index": split_index,
        "max_adjust_seconds": round(max_adjust, 3),
        "refinement": refinement,
        "output": str(output),
    }



def _tri_modal_sparse_cold_open_clock(
    plateau: dict[str, object] | None,
    embedded_result: dict[str, object],
    *,
    first_support: int,
    post_support: int,
    post_offset: float,
    speech_offset: float,
    cold_overlap_ambiguity: bool,
) -> dict[str, object]:
    """Recover a sparse cold-open clock from three independent weak signals.

    Very short pre-opening dialogue can fail both normal STT support gates even
    when the clock is obvious: a few monotonic speech onsets, a couple Japanese
    text anchors, and the exact-video embedded timeline can all point at the
    same edit.  Treat none of those as authoritative alone.  A robust median is
    allowed only when all three modalities agree in direction and within tight
    bounds, while the post-opening speech/timeline clock is already stable.
    """
    rejected: dict[str, object] = {
        "accepted": False,
        "reason": "tri_modal_evidence_unavailable",
    }
    if not (
        cold_overlap_ambiguity
        and first_support == 1
        and post_support >= 12
        and isinstance(plateau, dict)
        and abs(float(speech_offset) - float(post_offset)) <= 0.50
    ):
        return rejected

    onset = plateau.get("pre")
    semantic = plateau.get("pre_semantic")
    if not isinstance(onset, dict) or not isinstance(semantic, dict):
        return rejected
    try:
        onset_shift = float(onset.get("best_shift_seconds") or 0.0)
        onset_best = onset.get("best") if isinstance(onset.get("best"), dict) else {}
        onset_matched = int(onset_best.get("matched") or 0)
        onset_coverage = float(onset_best.get("coverage") or 0.0)
        onset_error_raw = onset_best.get("mean_error_seconds")
        onset_error = (
            float(onset_error_raw)
            if onset_error_raw not in {None, ""}
            else float("inf")
        )
        semantic_eligible = int(semantic.get("eligible_cues") or 0)
    except (TypeError, ValueError):
        return rejected

    semantic_anchors = [
        row for row in (semantic.get("anchors") or []) if isinstance(row, dict)
    ]
    semantic_shifts: list[float] = []
    for row in semantic_anchors:
        try:
            value = float(row.get("shift_seconds"))
            similarity = float(row.get("similarity") or 0.0)
        except (TypeError, ValueError):
            continue
        if similarity < 0.70 or abs(value) < 0.20 or abs(value) > 12.75:
            continue
        semantic_shifts.append(value)
    if len(semantic_shifts) < 2:
        return {
            **rejected,
            "reason": "too_few_semantic_anchors",
            "semantic_anchor_count": len(semantic_shifts),
        }
    semantic_shifts.sort()
    mid = len(semantic_shifts) // 2
    semantic_shift = (
        semantic_shifts[mid]
        if len(semantic_shifts) % 2
        else (semantic_shifts[mid - 1] + semantic_shifts[mid]) / 2.0
    )
    semantic_spread = semantic_shifts[-1] - semantic_shifts[0]

    edge_hints: list[float] = []
    for raw in embedded_result.get("timeline_edge_hints_seconds") or []:
        try:
            value = float(raw) - float(post_offset)
        except (TypeError, ValueError):
            continue
        if abs(value) >= 0.20 and abs(value) <= 12.75:
            edge_hints.append(value)
    same_direction_edges = [
        value for value in edge_hints
        if value * onset_shift > 0.0 and value * semantic_shift > 0.0
    ]
    if not same_direction_edges:
        return {**rejected, "reason": "edge_hint_direction_mismatch"}
    edge_shift = min(same_direction_edges, key=lambda value: abs(value - semantic_shift))

    accepted = bool(
        4 <= semantic_eligible <= 12
        and onset_matched >= 3
        and onset_coverage >= 0.30
        and onset_error <= 0.30
        and 0.20 <= abs(onset_shift) <= 12.01
        and semantic_spread <= 3.0
        and onset_shift * semantic_shift > 0.0
        and abs(onset_shift - semantic_shift) <= 1.50
        and abs(edge_shift - semantic_shift) <= 2.00
    )
    if not accepted:
        return {
            **rejected,
            "reason": "tri_modal_clocks_disagree",
            "onset_shift_seconds": round(onset_shift, 3),
            "semantic_shift_seconds": round(semantic_shift, 3),
            "edge_shift_seconds": round(edge_shift, 3),
            "semantic_spread_seconds": round(semantic_spread, 3),
        }

    clocks = sorted([onset_shift, semantic_shift, edge_shift])
    chosen = clocks[1]
    return {
        "accepted": True,
        "reason": "tri_modal_sparse_cold_open_consensus",
        "shift_seconds": round(chosen, 3),
        "onset_shift_seconds": round(onset_shift, 3),
        "semantic_shift_seconds": round(semantic_shift, 3),
        "edge_shift_seconds": round(edge_shift, 3),
        "semantic_anchor_count": len(semantic_shifts),
        "semantic_spread_seconds": round(semantic_spread, 3),
        "onset_matched": onset_matched,
        "onset_coverage": round(onset_coverage, 4),
        "onset_mean_error_seconds": round(onset_error, 4),
    }

def _restore_embedded_opening_clock_scaffold(
    aligned: Path,
    embedded_result: dict[str, object],
    speech_result: dict[str, object],
    cache_dir: Path,
    *,
    embedded_reference: Path | None = None,
) -> tuple[Path, dict[str, object]]:
    if aligned.parent.name == "embedded-opening-scaffold":
        return aligned, _result(
            "already_applied",
            applied=False,
            idempotent=True,
            output=str(aligned),
        )

    segments = embedded_result.get("timeline_segments")
    risk = embedded_result.get("timeline_early_edit_audio_verification")
    if not isinstance(segments, list) or len(segments) < 2:
        return aligned, _result(
            "opening_scaffold_unavailable",
            applied=False,
            reason_detail="not_piecewise",
        )
    if not isinstance(risk, dict) or not bool(risk.get("required")):
        return aligned, _result(
            "opening_scaffold_unavailable",
            applied=False,
            reason_detail="timeline_not_risky",
        )

    stable = [
        row for row in segments
        if isinstance(row, dict)
        and str(row.get("kind") or "stable") == "stable"
    ]
    if len(stable) < 2:
        return aligned, _result(
            "opening_scaffold_unavailable",
            applied=False,
            reason_detail="too_few_stable_segments",
        )

    first = stable[0]
    post = max(
        stable[1:],
        key=lambda row: max(0, int(row.get("support") or 0)),
    )
    try:
        first_offset = float(first.get("offset_seconds") or 0.0)
        post_offset = float(post.get("offset_seconds") or 0.0)
        first_support = max(0, int(first.get("support") or 0))
        post_support = max(0, int(post.get("support") or 0))
        speech_offset = float(speech_result.get("offset_seconds"))
    except (TypeError, ValueError):
        return aligned, _result(
            "opening_scaffold_unavailable",
            applied=False,
            reason_detail="clock_metrics_unavailable",
        )

    target_relative_clock = first_offset - post_offset

    plateau = speech_result.get("stt_opening_plateau_refinement")
    pre_refinement = 0.0
    post_refinement = 0.0
    if isinstance(plateau, dict) and bool(plateau.get("applied")):
        try:
            pre_refinement = float(plateau.get("pre_shift_seconds") or 0.0)
            post_refinement = float(plateau.get("post_shift_seconds") or 0.0)
        except (TypeError, ValueError):
            pre_refinement = 0.0
            post_refinement = 0.0

    existing_relative_clock = pre_refinement - post_refinement
    correction = target_relative_clock - existing_relative_clock

    risk_reasons_raw = risk.get("reasons")
    risk_reasons = (
        {str(value) for value in risk_reasons_raw}
        if isinstance(risk_reasons_raw, list)
        else set()
    )
    cold_start = embedded_result.get("timeline_cold_start")
    cold_overlap_delta = 0.0
    cold_overlap_gap = 0.0
    cold_overlap_boundary = 0.0
    cold_overlap_ambiguity = False
    if isinstance(cold_start, dict):
        try:
            cold_overlap_delta = float(cold_start.get("delta_seconds") or 0.0)
            cold_overlap_gap = float(cold_start.get("gap_seconds") or 0.0)
            cold_overlap_boundary = float(
                cold_start.get("boundary_source_time") or 0.0
            )
        except (TypeError, ValueError):
            cold_overlap_delta = cold_overlap_gap = cold_overlap_boundary = 0.0
        cold_overlap_ambiguity = bool(
            str(cold_start.get("reason") or "")
            == "cold_start_overlaps_main_boundary"
            and "opening_gap_clock_ambiguity" in risk_reasons
            and abs(cold_overlap_delta) >= 1.5
            and cold_overlap_gap >= 45.0
            and 0.0 < cold_overlap_boundary <= 240.0
        )

    tri_modal_cold_open = _tri_modal_sparse_cold_open_clock(
        plateau if isinstance(plateau, dict) else None,
        embedded_result,
        first_support=first_support,
        post_support=post_support,
        post_offset=post_offset,
        speech_offset=speech_offset,
        cold_overlap_ambiguity=cold_overlap_ambiguity,
    )
    tri_modal_cold_open_authoritative = bool(tri_modal_cold_open.get("accepted"))
    if tri_modal_cold_open_authoritative:
        target_relative_clock = float(tri_modal_cold_open["shift_seconds"])
        correction = target_relative_clock - existing_relative_clock

    # If the Japanese STT plateau was recovered from two independent sparse
    # signals (monotonic onsets + matching Japanese text), it is stronger than
    # the one-window subtitle-only early clock that triggered this verification.
    # Do not "restore" that coarse clock on top of the verified speech result.
    plateau_choice = plateau.get("pre_choice") if isinstance(plateau, dict) else None
    plateau_choice = plateau_choice if isinstance(plateau_choice, dict) else {}
    sparse_speech_plateau_authoritative = bool(
        cold_overlap_ambiguity
        and isinstance(plateau, dict)
        and bool(plateau.get("applied"))
        and str(plateau_choice.get("mode") or "")
        == "sparse_cross_modal_consensus"
        and abs(pre_refinement) >= 0.20
        and abs(post_refinement) < 0.50
    )
    if sparse_speech_plateau_authoritative:
        return aligned, _result(
            "speech_plateau_authoritative",
            applied=False,
            reason_detail="sparse_cross_modal_speech_clock",
            target_relative_clock_seconds=round(target_relative_clock, 3),
            speech_relative_clock_seconds=round(existing_relative_clock, 3),
            pre_refinement_seconds=round(pre_refinement, 3),
            post_refinement_seconds=round(post_refinement, 3),
            cold_overlap_ambiguity=True,
            pre_choice=plateau_choice,
            output=str(aligned),
        )

    # A subtitle-only one-window cold-open estimate can be useful but coarse.
    # When the timeline itself marked that cold open ambiguous and routed it to
    # Japanese STT, allow a smaller 2s scaffold delta; the local speech stage
    # below must still verify the residual before it becomes authoritative.
    minimum_scaffold_delta = 2.0 if cold_overlap_ambiguity else 4.0
    if not (minimum_scaffold_delta <= abs(correction) <= 20.0):
        return aligned, _result(
            "opening_scaffold_unavailable",
            applied=False,
            reason_detail="clock_delta_out_of_range",
            correction_seconds=round(correction, 3),
            cold_overlap_ambiguity=cold_overlap_ambiguity,
        )
    strong_single_window_early_clock = tri_modal_cold_open_authoritative
    single_window_evidence: dict[str, object] = (
        {
            "accepted": True,
            "evidence_mode": "tri_modal_sparse_cold_open_consensus",
            "tri_modal": dict(tri_modal_cold_open),
        }
        if tri_modal_cold_open_authoritative
        else {}
    )
    if first_support == 1 and post_support >= 12:
        salvaged_unstable = embedded_result.get(
            "timeline_unstable_sparse_preopening_salvage"
        )
        salvage_confirmed = bool(
            isinstance(salvaged_unstable, dict)
            and salvaged_unstable.get("accepted")
            and abs(speech_offset - post_offset) <= 0.50
        )
        if salvage_confirmed:
            strong_single_window_early_clock = True
            single_window_evidence = {
                "accepted": True,
                "evidence_mode": "unstable_path_salvage",
                "speech_post_error_seconds": round(
                    abs(speech_offset - post_offset), 4
                ),
                "salvage": dict(salvaged_unstable),
            }

        validation = embedded_result.get("timeline_validation")
        after_validation = (
            validation.get("after")
            if isinstance(validation, dict)
            and isinstance(validation.get("after"), dict)
            else {}
        )
        holdout = (
            validation.get("holdout")
            if isinstance(validation, dict)
            and isinstance(validation.get("holdout"), dict)
            else {}
        )
        edge_hints_raw = embedded_result.get("timeline_edge_hints_seconds")
        edge_hints: list[float] = []
        if isinstance(edge_hints_raw, list):
            for value in edge_hints_raw:
                try:
                    edge_hints.append(float(value))
                except (TypeError, ValueError):
                    continue
        try:
            first_score = float(first.get("mean_score") or 0.0)
            first_coverage = float(first.get("mean_coverage") or 0.0)
            after_f1 = float(after_validation.get("f1") or 0.0)
            activity_f1 = (
                float(validation.get("activity_f1") or 0.0)
                if isinstance(validation, dict)
                else 0.0
            )
            holdout_p90 = float(holdout.get("p90_abs_residual_seconds"))
            holdout_coverage = float(holdout.get("mean_coverage") or 0.0)
            early_span = float(risk.get("early_offset_span_seconds") or 0.0)
            early_jump = float(risk.get("early_max_jump_seconds") or 0.0)
        except (TypeError, ValueError):
            first_score = 0.0
            first_coverage = 0.0
            after_f1 = 0.0
            activity_f1 = 0.0
            holdout_p90 = float("inf")
            holdout_coverage = 0.0
            early_span = 0.0
            early_jump = 0.0

        first_hint_error = (
            min(abs(first_offset - value) for value in edge_hints)
            if edge_hints
            else float("inf")
        )
        post_hint_error = (
            min(abs(post_offset - value) for value in edge_hints)
            if edge_hints
            else float("inf")
        )
        speech_post_error = abs(speech_offset - post_offset)
        edge_confirmed_single_window = bool(
            first_score >= 3.0
            and first_coverage >= 0.85
            and "early_path_clock_change" in risk_reasons
            and early_span >= 6.0
            and early_jump >= 4.0
            and first_hint_error <= 2.0
            and post_hint_error <= 1.0
            and after_f1 >= 0.78
            and activity_f1 >= 0.86
            and holdout_p90 <= 1.0
            and holdout_coverage >= 0.84
            and speech_post_error <= 2.5
        )
        # Edge hints are intentionally weak evidence: a short cold open can have
        # only one usable timeline window and both edge hypotheses can land on
        # nearby title-card/OP activity.  Accept that one window without edge
        # agreement only when timeline holdout, activity, coverage and Japanese
        # speech all independently say the main/post-OP clock is exceptionally
        # stable.  This is much stricter than the normal edge-confirmed path.
        validation_confirmed_single_window = bool(
            first_score >= 3.0
            and first_coverage >= 0.95
            and "early_path_clock_change" in risk_reasons
            and early_span >= 8.0
            and early_jump >= 8.0
            and after_f1 >= 0.82
            and activity_f1 >= 0.88
            and holdout_p90 <= 0.60
            and holdout_coverage >= 0.90
            and speech_post_error <= 0.35
        )
        speech_verified_cold_ambiguity = bool(
            cold_overlap_ambiguity
            and first_score >= 2.20
            and first_coverage >= 0.80
            and after_f1 >= 0.72
            and activity_f1 >= 0.86
            and holdout_p90 <= 1.0
            and holdout_coverage >= 0.84
            and speech_post_error <= 0.75
        )
        if not strong_single_window_early_clock:
            strong_single_window_early_clock = bool(
                edge_confirmed_single_window
                or validation_confirmed_single_window
                or speech_verified_cold_ambiguity
            )
            single_window_evidence = {
                "accepted": strong_single_window_early_clock,
                "evidence_mode": (
                    "edge_confirmed"
                    if edge_confirmed_single_window
                    else "validation_confirmed"
                    if validation_confirmed_single_window
                    else "speech_verified_cold_ambiguity"
                    if speech_verified_cold_ambiguity
                    else "rejected"
                ),
                "first_score": round(first_score, 4),
                "first_coverage": round(first_coverage, 4),
                "first_hint_error_seconds": (
                    round(first_hint_error, 4)
                    if math.isfinite(first_hint_error)
                    else None
                ),
                "post_hint_error_seconds": (
                    round(post_hint_error, 4)
                    if math.isfinite(post_hint_error)
                    else None
                ),
                "after_f1": round(after_f1, 4),
                "activity_f1": round(activity_f1, 4),
                "holdout_p90_seconds": (
                    round(holdout_p90, 4)
                    if math.isfinite(holdout_p90)
                    else None
                ),
                "holdout_mean_coverage": round(holdout_coverage, 4),
                "early_offset_span_seconds": round(early_span, 4),
                "early_max_jump_seconds": round(early_jump, 4),
                "speech_post_error_seconds": round(speech_post_error, 4),
            }

    if (
        (first_support < 2 and not strong_single_window_early_clock)
        or post_support < 6
    ):
        return aligned, _result(
            "opening_scaffold_unavailable",
            applied=False,
            reason_detail="insufficient_segment_support",
            first_support=first_support,
            post_support=post_support,
            single_window_evidence=single_window_evidence,
        )
    if abs(speech_offset - post_offset) > 2.5:
        return aligned, _result(
            "opening_scaffold_unavailable",
            applied=False,
            reason_detail="speech_clock_disagrees_with_post_plateau",
            speech_offset_seconds=round(speech_offset, 3),
            post_offset_seconds=round(post_offset, 3),
        )

    try:
        cues = parse_srt(aligned)
    except OSError as exc:
        return aligned, _result(
            "opening_scaffold_read_error",
            applied=False,
            error=str(exc),
        )
    if len(cues) < 8:
        return aligned, _result(
            "opening_scaffold_unavailable",
            applied=False,
            reason_detail="too_few_cues",
        )

    gaps = []
    timeline_origin = float(cues[0][0])
    for index in range(1, len(cues)):
        previous_end = float(cues[index - 1][1])
        current_start = float(cues[index][0])
        gap = current_start - previous_end
        midpoint = (previous_end + current_start) / 2.0
        if gap >= 45.0 and midpoint - timeline_origin <= 240.0:
            gaps.append((gap, index, midpoint))
    if not gaps:
        return aligned, _result(
            "opening_scaffold_unavailable",
            applied=False,
            reason_detail="opening_gap_not_found",
        )

    gap_seconds, split_index, gap_midpoint = max(gaps)
    repaired = []
    for index, (start, end, cue_text) in enumerate(cues):
        shift = correction if index < split_index else 0.0
        repaired.append(
            (
                max(0.0, float(start) + shift),
                max(0.05, float(end) + shift),
                cue_text,
            )
        )

    # A single strong early timeline window can recover the large pre-OP
    # edit, but its coarse 1-second clock may still leave a small residual.
    # Once the large jump is fixed, measure only that remaining residual
    # against the cached Japanese STT reference. Apply it only when:
    #   1) the local speech estimate itself is accepted, and
    #   2) it agrees with the independent cold-start edge hint.
    # This keeps the normal post-OP/main-episode clock untouched.
    base_correction = correction
    residual_speech_refinement: dict[str, object] = {
        "attempted": False,
        "accepted": False,
        "reason": "not_needed",
    }
    residual_shift = 0.0
    if strong_single_window_early_clock and not tri_modal_cold_open_authoritative:
        reference_raw = speech_result.get("timing_reference")
        cold_start = embedded_result.get("timeline_cold_start")
        cold_delta: float | None = None
        if isinstance(cold_start, dict):
            try:
                candidate_delta = float(cold_start.get("delta_seconds"))
            except (TypeError, ValueError):
                candidate_delta = 0.0
            if (
                str(cold_start.get("reason") or "")
                == "cold_start_overlaps_main_boundary"
                and 0.45 <= abs(candidate_delta) <= 8.0
            ):
                cold_delta = candidate_delta

        reference = (
            Path(str(reference_raw)).expanduser()
            if reference_raw not in {None, ""}
            else None
        )
        if reference is None or not reference.is_file():
            residual_speech_refinement = {
                "attempted": False,
                "accepted": False,
                "reason": "timing_reference_unavailable",
                "cold_hint_delta_seconds": (
                    round(cold_delta, 3) if cold_delta is not None else None
                ),
            }
        elif cold_delta is None:
            residual_speech_refinement = {
                "attempted": False,
                "accepted": False,
                "reason": "independent_cold_hint_unavailable",
            }
        else:
            try:
                reference_cues = parse_srt(reference)
            except OSError as exc:
                residual_speech_refinement = {
                    "attempted": True,
                    "accepted": False,
                    "reason": "timing_reference_read_error",
                    "error": str(exc),
                }
            else:
                pre_starts = [
                    float(start)
                    for start, _end, _text in repaired[:split_index]
                ][:48]
                reference_starts = [
                    float(start)
                    for start, _end, _text in reference_cues
                ]
                if pre_starts and split_index > 0:
                    low = min(pre_starts) - 3.0
                    high = float(repaired[split_index - 1][1]) + 3.0
                    local_reference = [
                        value
                        for value in reference_starts
                        if low <= value <= high
                    ]
                    estimate = _local_speech_shift_estimate(
                        pre_starts,
                        local_reference,
                        max_shift_seconds=(3.0 if cold_overlap_ambiguity else 2.5),
                    )
                    try:
                        raw_candidate_shift = estimate.get("best_shift_seconds")
                        if raw_candidate_shift in {None, ""}:
                            raw_candidate_shift = estimate.get("shift_seconds")
                        candidate_shift = float(raw_candidate_shift or 0.0)
                    except (TypeError, ValueError):
                        candidate_shift = 0.0

                    best_metrics = estimate.get("best")
                    best_metrics = (
                        best_metrics if isinstance(best_metrics, dict) else {}
                    )
                    try:
                        best_matched = int(best_metrics.get("matched") or 0)
                        best_coverage = float(best_metrics.get("coverage") or 0.0)
                        matched_gain = int(estimate.get("matched_gain") or 0)
                        error_gain = float(
                            estimate.get("mean_error_gain_seconds") or 0.0
                        )
                    except (TypeError, ValueError):
                        best_matched = matched_gain = 0
                        best_coverage = error_gain = 0.0

                    # The generic local-STT gate deliberately wants >=45%
                    # coverage or a larger match gain. In a short cold open
                    # that can be too strict. Allow the borderline estimate
                    # only in this already-strong single-window edit path and
                    # only when the independent embedded edge hint agrees.
                    borderline_speech = bool(
                        not bool(estimate.get("accepted"))
                        and best_matched >= 6
                        and best_coverage >= 0.38
                        and matched_gain >= 1
                        and error_gain >= 0.10
                    )
                    speech_supported = bool(
                        bool(estimate.get("accepted")) or borderline_speech
                    )
                    if cold_overlap_ambiguity:
                        # The first-edge hint can overshoot when the two subtitle
                        # tracks do not begin with corresponding cues.  In this
                        # path Japanese speech is authoritative for the small
                        # residual; the edge hint is only a direction/sanity
                        # check, never the target value.  Borderline STT is not
                        # enough here.
                        agrees_with_hint = bool(
                            bool(estimate.get("accepted"))
                            and 0.20 <= abs(candidate_shift) <= 3.0
                            and candidate_shift * cold_delta > 0.0
                            and abs(candidate_shift)
                            <= abs(cold_delta) + 0.75
                        )
                    else:
                        agrees_with_hint = bool(
                            speech_supported
                            and 0.20 <= abs(candidate_shift) <= 2.5
                            and candidate_shift * cold_delta > 0.0
                            and abs(candidate_shift - cold_delta) <= 0.85
                        )
                    residual_speech_refinement = {
                        "attempted": True,
                        "accepted": agrees_with_hint,
                        "reason": (
                            (
                                "local_speech_confirms_ambiguous_cold_open"
                                if cold_overlap_ambiguity
                                else "borderline_speech_and_cold_hint_agree"
                                if borderline_speech
                                else "local_speech_and_cold_hint_agree"
                            )
                            if agrees_with_hint
                            else "residual_not_independently_confirmed"
                        ),
                        "speech": estimate,
                        "borderline_speech": borderline_speech,
                        "cold_hint_delta_seconds": round(cold_delta, 3),
                        "speech_shift_seconds": round(candidate_shift, 3),
                        "agreement_error_seconds": round(
                            abs(candidate_shift - cold_delta), 3
                        ),
                    }
                    if agrees_with_hint:
                        residual_shift = candidate_shift
                        repaired = [
                            (
                                max(
                                    0.0,
                                    float(start)
                                    + (
                                        residual_shift
                                        if index < split_index
                                        else 0.0
                                    ),
                                ),
                                max(
                                    0.05,
                                    float(end)
                                    + (
                                        residual_shift
                                        if index < split_index
                                        else 0.0
                                    ),
                                ),
                                cue_text,
                            )
                            for index, (start, end, cue_text)
                            in enumerate(repaired)
                        ]
                        correction += residual_shift
                else:
                    residual_speech_refinement = {
                        "attempted": True,
                        "accepted": False,
                        "reason": "pre_opening_cues_unavailable",
                    }

    # Final small correction: the embedded text subtitle belongs to the exact
    # video and is a better local clock than Whisper in a short/noisy cold open.
    # The large edit is still recovered by the timeline+STT scaffold above.
    # Here we only measure a <=2s residual before the opening, and only accept it
    # when the independent cold-start edge hint agrees in direction/magnitude.
    embedded_reference_shift = 0.0
    embedded_reference_refinement: dict[str, object] = {
        "attempted": False,
        "accepted": False,
        "reason": "not_available",
    }

    if (
        strong_single_window_early_clock
        and not tri_modal_cold_open_authoritative
        and embedded_reference is not None
        and Path(embedded_reference).is_file()
        and split_index > 0
    ):
        cold_start = embedded_result.get("timeline_cold_start")
        cold_delta: float | None = None
        if isinstance(cold_start, dict):
            try:
                candidate_delta = float(cold_start.get("delta_seconds"))
            except (TypeError, ValueError):
                candidate_delta = 0.0
            if (
                str(cold_start.get("reason") or "")
                == "cold_start_overlaps_main_boundary"
                and 0.40 <= abs(candidate_delta) <= 2.5
            ):
                cold_delta = candidate_delta

        try:
            reference_cues = parse_srt(Path(embedded_reference))
        except OSError as exc:
            embedded_reference_refinement = {
                "attempted": True,
                "accepted": False,
                "reason": "embedded_reference_read_error",
                "error": str(exc),
            }
        else:
            pre_cues = repaired[:split_index]
            if cold_delta is None:
                embedded_reference_refinement = {
                    "attempted": True,
                    "accepted": False,
                    "reason": "cold_hint_unavailable",
                }
            elif len(pre_cues) < 6 or len(reference_cues) < 8:
                embedded_reference_refinement = {
                    "attempted": True,
                    "accepted": False,
                    "reason": "too_few_cues",
                }
            else:
                region_start = max(
                    0.0,
                    min(float(start) for start, _end, _text in pre_cues) - 1.5,
                )
                region_end = (
                    max(float(end) for _start, end, _text in pre_cues) + 1.5
                )
                probe = _windowed_reference_shift(
                    repaired,
                    reference_cues,
                    region_start=region_start,
                    region_end=region_end,
                    max_shift_seconds=2.0,
                )
                try:
                    candidate_shift = float(probe.get("shift_seconds") or 0.0)
                    matched = int(probe.get("matched_onsets") or 0)
                    coverage = float(probe.get("coverage") or 0.0)
                    improvement = float(probe.get("score_improvement") or 0.0)
                except (TypeError, ValueError):
                    candidate_shift = 0.0
                    matched = 0
                    coverage = 0.0
                    improvement = 0.0

                basic_probe = bool(
                    bool(probe.get("confident"))
                    and matched >= 6
                    and coverage >= 0.35
                    and improvement >= 0.04
                    and 0.20 <= abs(candidate_shift) <= 1.80
                )
                cold_hint_agrees = bool(
                    basic_probe
                    and candidate_shift * cold_delta > 0.0
                    and abs(candidate_shift - cold_delta) <= 1.25
                )
                agrees = cold_hint_agrees
                embedded_reference_refinement = {
                    "attempted": True,
                    "accepted": agrees,
                    "reason": (
                        "embedded_reference_and_cold_hint_agree"
                        if agrees
                        else "embedded_reference_not_confirmed"
                    ),
                    "cold_hint_agrees": cold_hint_agrees,
                    "shift_seconds": round(candidate_shift, 3),
                    "cold_hint_delta_seconds": round(cold_delta, 3),
                    "agreement_error_seconds": round(
                        abs(candidate_shift - cold_delta), 3
                    ),
                    "probe": probe,
                }
                if agrees:
                    embedded_reference_shift = candidate_shift
                    repaired = [
                        (
                            max(
                                0.0,
                                float(start)
                                + (
                                    embedded_reference_shift
                                    if index < split_index
                                    else 0.0
                                ),
                            ),
                            max(
                                0.05,
                                float(end)
                                + (
                                    embedded_reference_shift
                                    if index < split_index
                                    else 0.0
                                ),
                            ),
                            cue_text,
                        )
                        for index, (start, end, cue_text)
                        in enumerate(repaired)
                    ]
                    correction += embedded_reference_shift

    sparse_preop_refinement: dict[str, object] = {
        "attempted": False,
        "accepted": False,
        "reason": "not_applicable",
    }
    if (
        strong_single_window_early_clock
        and not tri_modal_cold_open_authoritative
        and split_index <= 6
    ):
        repaired, sparse_preop_refinement = _refine_sparse_preopening_cues(
            repaired,
            split_index,
            speech_result,
            embedded_reference,
            max_adjust_seconds=(
                3.25
                if isinstance(
                    embedded_result.get(
                        "timeline_unstable_sparse_preopening_salvage"
                    ),
                    dict,
                )
                else 2.5
            ),
        )

    stat = aligned.stat()
    digest = hashlib.sha1(
        (
            f"embedded-opening-scaffold-v2-sparse-two-plateau:"
            f"{aligned.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:"
            f"{first_offset:.3f}:{post_offset:.3f}:{speech_offset:.3f}:"
            f"{split_index}:{correction:.3f}:"
            f"{embedded_reference_shift:.3f}:"
            f"{json.dumps(sparse_preop_refinement, sort_keys=True, default=str)}:"
            f"embedded-ref-v2"
        ).encode()
    ).hexdigest()[:20]
    output_dir = cache_dir / "embedded-opening-scaffold"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{digest}.srt"
    if not output.exists() or output.stat().st_size <= 0:
        write_srt(repaired, output, preserve_order=True)

    return output, _result(
        "applied",
        applied=True,
        output=str(output),
        correction_seconds=round(correction, 3),
        base_correction_seconds=round(base_correction, 3),
        residual_speech_shift_seconds=round(residual_shift, 3),
        residual_speech_refinement=residual_speech_refinement,
        embedded_reference_shift_seconds=round(embedded_reference_shift, 3),
        embedded_reference_refinement=embedded_reference_refinement,
        sparse_preop_refinement=sparse_preop_refinement,
        target_relative_clock_seconds=round(target_relative_clock, 3),
        existing_relative_clock_seconds=round(existing_relative_clock, 3),
        pre_refinement_seconds=round(pre_refinement, 3),
        post_refinement_seconds=round(post_refinement, 3),
        first_offset_seconds=round(first_offset, 3),
        post_offset_seconds=round(post_offset, 3),
        speech_offset_seconds=round(speech_offset, 3),
        first_support=first_support,
        post_support=post_support,
        early_support_override=strong_single_window_early_clock,
        single_window_evidence=single_window_evidence,
        tri_modal_cold_open=tri_modal_cold_open,
        gap_seconds=round(gap_seconds, 3),
        gap_midpoint_seconds=round(gap_midpoint, 3),
        split_cue_index=split_index,
    )


def _local_stt_text_shift_estimate(
    cues: list[tuple[float, float, str]],
    reference: Path,
    *,
    max_shift_seconds: float = 8.0,
) -> dict[str, object]:
    """Estimate a local subtitle clock from Japanese text identity, not onset density.

    This is intentionally conservative.  Each subtitle cue must independently
    match a nearby STT segment by text; accepted cue shifts must then form one
    tight clock cluster.  It exists to disambiguate short opening/cold-open
    regions where onset-only matching can lock onto unrelated dense speech.
    """
    segments = _stt_text_segments(reference)
    if len(cues) < 3 or len(segments) < 3:
        return {
            "accepted": False,
            "reason": "too_few_text_segments",
            "shift_seconds": 0.0,
        }

    prepared_segments: list[tuple[float, float, str]] = []
    for row in segments:
        try:
            start = float(row.get("start") or 0.0)
            end = float(row.get("end") or start)
        except (TypeError, ValueError):
            continue
        text = normalize_reading_text(str(row.get("text") or ""))
        if len(text) >= 2:
            prepared_segments.append((start, max(start, end), text))
    prepared_segments.sort(key=lambda row: row[0])
    if len(prepared_segments) < 3:
        return {
            "accepted": False,
            "reason": "too_few_normalized_text_segments",
            "shift_seconds": 0.0,
        }

    eligible = 0
    anchors: list[dict[str, object]] = []
    search_pad = float(max_shift_seconds) + 1.5
    for cue_index, (cue_start, cue_end, cue_text_raw) in enumerate(cues):
        cue_text = normalize_reading_text(str(cue_text_raw or ""))
        if len(cue_text) < 2:
            continue
        eligible += 1
        left = float(cue_start) - search_pad
        right = float(cue_end) + search_pad
        candidates: list[tuple[float, float, float, str]] = []
        for ref_start, ref_end, ref_text in prepared_segments:
            if ref_end < left:
                continue
            if ref_start > right:
                break
            shift = ref_start - float(cue_start)
            if abs(shift) > float(max_shift_seconds) + 0.75:
                continue
            similarity = max(
                float(fuzz.ratio(cue_text, ref_text)),
                float(fuzz.partial_ratio(cue_text, ref_text)),
            ) / 100.0
            candidates.append((similarity, -abs(shift), shift, ref_text, ref_start))
        if not candidates:
            continue
        candidates.sort(reverse=True)
        best_similarity, _neg_abs, best_shift, _best_text, best_ref_start = candidates[0]
        second_similarity = candidates[1][0] if len(candidates) > 1 else 0.0
        # Short Japanese cues are prone to accidental matches.  Require either
        # a strong absolute match or a useful margin over nearby alternatives.
        strong_enough = bool(
            best_similarity >= 0.72
            or (best_similarity >= 0.64 and best_similarity - second_similarity >= 0.08)
        )
        if not strong_enough:
            continue
        anchors.append(
            {
                "cue_index": cue_index,
                "shift_seconds": float(best_shift),
                "similarity": float(best_similarity),
                "margin": float(best_similarity - second_similarity),
                "reference_start_seconds": float(best_ref_start),
            }
        )

    if len(anchors) < 3:
        return {
            "accepted": False,
            "reason": "too_few_semantic_anchors",
            "shift_seconds": 0.0,
            "eligible_cues": eligible,
            "anchor_count": len(anchors),
            "coverage": round(len(anchors) / max(1, eligible), 4),
            "anchors": anchors,
        }

    shifts = sorted(float(row["shift_seconds"]) for row in anchors)
    # Find the densest <=1.4s clock cluster.  A local edit should move nearby
    # dialogue as one plateau, while unrelated text matches scatter broadly.
    best_cluster: list[dict[str, object]] = []
    for center in shifts:
        cluster = [
            row
            for row in anchors
            if abs(float(row["shift_seconds"]) - center) <= 0.70
        ]
        if len(cluster) > len(best_cluster):
            best_cluster = cluster
        elif len(cluster) == len(best_cluster) and cluster:
            current_mean = sum(float(row["similarity"]) for row in cluster) / len(cluster)
            best_mean = (
                sum(float(row["similarity"]) for row in best_cluster) / len(best_cluster)
                if best_cluster
                else 0.0
            )
            if current_mean > best_mean:
                best_cluster = cluster

    cluster_shifts = sorted(float(row["shift_seconds"]) for row in best_cluster)
    middle = len(cluster_shifts) // 2
    if not cluster_shifts:
        median_shift = 0.0
    elif len(cluster_shifts) % 2:
        median_shift = cluster_shifts[middle]
    else:
        median_shift = (cluster_shifts[middle - 1] + cluster_shifts[middle]) / 2.0
    mean_similarity = (
        sum(float(row["similarity"]) for row in best_cluster) / len(best_cluster)
        if best_cluster
        else 0.0
    )
    unique_reference_count = len(
        {round(float(row["reference_start_seconds"]), 3) for row in best_cluster}
    )
    spread = (
        max(cluster_shifts) - min(cluster_shifts)
        if len(cluster_shifts) >= 2
        else 0.0
    )
    coverage = len(best_cluster) / max(1, eligible)
    accepted = bool(
        len(best_cluster) >= 3
        and unique_reference_count >= 3
        and coverage >= 0.35
        and mean_similarity >= 0.70
        and spread <= 1.40
        and abs(median_shift) >= 0.20
        and abs(median_shift) <= float(max_shift_seconds) + 0.75
    )
    return {
        "accepted": accepted,
        "reason": "semantic_clock_cluster" if accepted else "semantic_clock_ambiguous",
        "shift_seconds": round(float(median_shift), 3) if accepted else 0.0,
        "best_shift_seconds": round(float(median_shift), 3),
        "eligible_cues": eligible,
        "anchor_count": len(anchors),
        "cluster_support": len(best_cluster),
        "unique_reference_count": unique_reference_count,
        "coverage": round(coverage, 4),
        "mean_similarity": round(mean_similarity, 4),
        "cluster_spread_seconds": round(spread, 3),
        "anchors": anchors,
    }


def _choose_plateau_shift(
    onset: dict[str, object],
    semantic: dict[str, object],
    *,
    onset_limit_seconds: float,
    semantic_limit_seconds: float,
    allow_sparse_cross_modal_consensus: bool = False,
) -> tuple[float, dict[str, object]]:
    """Choose a local plateau shift from onset and Japanese-text clocks.

    Normal regions still require the existing strong gates.  A very short cold
    open is special: three monotonic onset matches plus one unambiguous Japanese
    text anchor can be more trustworthy than either generic minimum-support gate
    on its own.  The sparse path therefore accepts only when the two independent
    estimates agree tightly; it never accepts one weak estimate by itself.
    """
    onset_shift = (
        float(onset.get("shift_seconds") or 0.0)
        if bool(onset.get("accepted"))
        else 0.0
    )
    semantic_shift = (
        float(semantic.get("shift_seconds") or 0.0)
        if bool(semantic.get("accepted"))
        else 0.0
    )
    try:
        semantic_support = int(semantic.get("cluster_support") or 0)
        semantic_coverage = float(semantic.get("coverage") or 0.0)
        semantic_similarity = float(semantic.get("mean_similarity") or 0.0)
        raw_semantic_spread = semantic.get("cluster_spread_seconds")
        semantic_spread = (
            float(raw_semantic_spread)
            if raw_semantic_spread not in {None, ""}
            else 999.0
        )
    except (TypeError, ValueError):
        semantic_support = 0
        semantic_coverage = semantic_similarity = 0.0
        semantic_spread = 999.0

    strong_semantic = bool(
        bool(semantic.get("accepted"))
        and semantic_support >= 3
        and semantic_coverage >= 0.35
        and semantic_similarity >= 0.70
        and semantic_spread <= 1.40
        and abs(semantic_shift) <= float(semantic_limit_seconds) + 0.75
    )
    conflicting = bool(
        strong_semantic
        and bool(onset.get("accepted"))
        and abs(semantic_shift - onset_shift) >= 1.50
    )

    # Sparse cold-open consensus: the generic onset gate wants >=45% coverage
    # and the semantic gate wants >=3 text anchors.  An 8-cue pre-opening can
    # legitimately have only three clean onsets and one distinctive spoken line.
    # Accept that low-support case only when both modalities independently land
    # on the same clock (<=0.75s apart) and each weak estimate is itself sharp.
    if allow_sparse_cross_modal_consensus:
        try:
            onset_best_shift = float(onset.get("best_shift_seconds") or 0.0)
            onset_best = onset.get("best")
            onset_best = onset_best if isinstance(onset_best, dict) else {}
            onset_best_matched = int(onset_best.get("matched") or 0)
            onset_best_coverage = float(onset_best.get("coverage") or 0.0)
            raw_onset_error = onset_best.get("mean_error_seconds")
            onset_best_error = (
                float(raw_onset_error)
                if raw_onset_error not in {None, ""}
                else float("inf")
            )
            onset_error_gain = float(onset.get("mean_error_gain_seconds") or 0.0)
            semantic_best_shift = float(semantic.get("best_shift_seconds") or 0.0)
            semantic_eligible = int(semantic.get("eligible_cues") or 0)
            semantic_anchor_count = int(semantic.get("anchor_count") or 0)
            semantic_anchors = semantic.get("anchors")
            semantic_anchors = semantic_anchors if isinstance(semantic_anchors, list) else []
        except (TypeError, ValueError):
            onset_best_shift = semantic_best_shift = 0.0
            onset_best_matched = semantic_eligible = semantic_anchor_count = 0
            onset_best_coverage = onset_error_gain = 0.0
            onset_best_error = float("inf")
            semantic_anchors = []

        strongest_anchor = max(
            (row for row in semantic_anchors if isinstance(row, dict)),
            key=lambda row: float(row.get("similarity") or 0.0),
            default={},
        )
        try:
            semantic_similarity = float(strongest_anchor.get("similarity") or 0.0)
            semantic_margin = float(strongest_anchor.get("margin") or 0.0)
        except (TypeError, ValueError):
            semantic_similarity = semantic_margin = 0.0

        sparse_consensus = bool(
            4 <= semantic_eligible <= 12
            and onset_best_matched >= 3
            and onset_best_coverage >= 0.30
            and onset_best_error <= 0.40
            and (int(onset.get("matched_gain") or 0) >= 2 or onset_error_gain >= 0.15)
            and semantic_anchor_count >= 1
            and semantic_similarity >= 0.72
            and semantic_margin >= 0.20
            and 0.20 <= abs(onset_best_shift) <= float(onset_limit_seconds) + 0.01
            and 0.20 <= abs(semantic_best_shift) <= float(semantic_limit_seconds) + 0.75
            and onset_best_shift * semantic_best_shift > 0.0
            and abs(onset_best_shift - semantic_best_shift) <= 0.75
        )
        if sparse_consensus:
            # Onset has several monotonic matches and therefore provides the
            # plateau clock; Japanese text is the independent anti-alias check.
            return onset_best_shift, {
                "mode": "sparse_cross_modal_consensus",
                "onset_shift_seconds": round(onset_best_shift, 3),
                "semantic_shift_seconds": round(semantic_best_shift, 3),
                "agreement_seconds": round(
                    abs(onset_best_shift - semantic_best_shift), 3
                ),
                "onset_matched": onset_best_matched,
                "onset_coverage": round(onset_best_coverage, 4),
                "onset_mean_error_seconds": round(onset_best_error, 4),
                "semantic_anchor_count": semantic_anchor_count,
                "semantic_similarity": round(semantic_similarity, 4),
                "semantic_margin": round(semantic_margin, 4),
            }
    if strong_semantic and (conflicting or not bool(onset.get("accepted"))):
        return semantic_shift, {
            "mode": "semantic_override" if conflicting else "semantic_only",
            "onset_shift_seconds": round(onset_shift, 3),
            "semantic_shift_seconds": round(semantic_shift, 3),
            "conflict_seconds": round(abs(semantic_shift - onset_shift), 3),
        }
    if strong_semantic and abs(semantic_shift - onset_shift) < 1.50:
        # Text identity is a better local clock; use it even when both methods
        # agree so the final value is not pulled by unrelated onset density.
        return semantic_shift, {
            "mode": "semantic_confirmed",
            "onset_shift_seconds": round(onset_shift, 3),
            "semantic_shift_seconds": round(semantic_shift, 3),
            "conflict_seconds": round(abs(semantic_shift - onset_shift), 3),
        }
    if abs(onset_shift) <= float(onset_limit_seconds) + 0.01:
        return onset_shift, {
            "mode": "onset_only",
            "onset_shift_seconds": round(onset_shift, 3),
            "semantic_shift_seconds": round(semantic_shift, 3),
        }
    return 0.0, {
        "mode": "rejected",
        "onset_shift_seconds": round(onset_shift, 3),
        "semantic_shift_seconds": round(semantic_shift, 3),
    }



def _accept_opening_plateau_refinement(
    *,
    pre_choice_mode: str,
    post_choice_mode: str,
    before_weighted: float,
    after_weighted: float,
    before_start: float,
    after_start: float,
    before_text_available: bool,
    before_text_rank: float,
    after_text_available: bool,
    after_text_rank: float,
    semantic_text_improved: bool,
    semantic_conflict_override: bool,
) -> tuple[bool, dict[str, bool]]:
    """Accept a local opening-clock repair without letting 300+ later cues vote it down.

    Sparse cross-modal consensus already means independent onset timing and
    Japanese text identity agree on the same short cold-open clock.  Whole-file
    activity is still a safety guard, but not an improvement requirement.
    """

    regular_activity_accepted = bool(
        after_weighted + 0.008 >= before_weighted
        and after_start + 0.003 >= before_start
    )
    semantic_text_not_degraded = bool(
        after_text_available
        and after_text_rank + 0.005 >= before_text_rank
    )
    sparse_cross_modal_consensus = bool(
        pre_choice_mode == "sparse_cross_modal_consensus"
        or post_choice_mode == "sparse_cross_modal_consensus"
    )
    sparse_consensus_accepted = bool(
        sparse_cross_modal_consensus
        and after_weighted + 0.030 >= before_weighted
        and after_start + 0.040 >= before_start
        and (
            semantic_text_not_degraded
            or (not before_text_available and not after_text_available)
        )
    )
    accepted = bool(
        (
            semantic_text_improved
            or (regular_activity_accepted and semantic_text_not_degraded)
            or sparse_consensus_accepted
        )
        if semantic_conflict_override
        else (regular_activity_accepted or sparse_consensus_accepted)
    )
    return accepted, {
        "regular_activity_accepted": regular_activity_accepted,
        "semantic_text_not_degraded": semantic_text_not_degraded,
        "sparse_cross_modal_consensus": sparse_cross_modal_consensus,
        "sparse_consensus_accepted": sparse_consensus_accepted,
    }

def _refine_stt_opening_plateaus(
    source: Path,
    aligned: Path,
    reference: Path,
    cache_dir: Path,
) -> tuple[Path, dict[str, object]]:
    """Micro-align the dialogue plateaus on either side of a long early OP gap."""
    try:
        source_cues = parse_srt(source)
        aligned_cues = parse_srt(aligned)
        reference_cues = parse_srt(reference)
    except OSError as exc:
        return aligned, _result(
            "stt_plateau_read_error",
            applied=False,
            error=str(exc),
        )
    if (
        len(source_cues) < 10
        or len(source_cues) != len(aligned_cues)
        or len(reference_cues) < 10
    ):
        return aligned, _result(
            "stt_plateau_structure_unavailable",
            applied=False,
            source_cues=len(source_cues),
            aligned_cues=len(aligned_cues),
            reference_cues=len(reference_cues),
        )

    gaps: list[tuple[float, int, float]] = []
    source_origin = float(source_cues[0][0])
    for index in range(1, len(source_cues)):
        previous_end = float(source_cues[index - 1][1])
        current_start = float(source_cues[index][0])
        gap = current_start - previous_end
        midpoint = (previous_end + current_start) / 2.0
        if gap >= 45.0 and midpoint - source_origin <= 240.0:
            gaps.append((gap, index, midpoint))
    if not gaps:
        return aligned, _result(
            "stt_plateau_opening_gap_not_found",
            applied=False,
        )

    gap_seconds, split_index, source_gap_midpoint = max(gaps)
    aligned_gap_midpoint = (
        float(aligned_cues[split_index - 1][1])
        + float(aligned_cues[split_index][0])
    ) / 2.0

    reference_starts = sorted(
        float(start)
        for start, _end, text in reference_cues
        if str(text or "").strip()
    )
    pre_starts = [
        float(start)
        for start, _end, text in aligned_cues[:split_index]
        if str(text or "").strip()
    ][-24:]
    post_limit = aligned_gap_midpoint + 240.0
    post_starts = [
        float(start)
        for start, _end, text in aligned_cues[split_index:]
        if str(text or "").strip() and float(start) <= post_limit
    ][:48]

    # Cold-open dialogue must only match speech that occurs before the OP.
    # Previously we searched all Whisper segments in the episode, so dense
    # dialogue/music later in the file could create a false +1s optimum.
    pre_reference_start = (min(pre_starts) - 12.0) if pre_starts else 0.0
    pre_reference_end = (
        float(aligned_cues[split_index - 1][1]) + 12.0
        if split_index > 0
        else aligned_gap_midpoint
    )
    pre_reference_starts = [
        value
        for value in reference_starts
        if pre_reference_start <= value <= pre_reference_end
    ]

    post_reference_start = (
        (min(post_starts) - 3.0) if post_starts else aligned_gap_midpoint
    )
    post_reference_end = (
        (max(post_starts) + 3.0) if post_starts else post_limit
    )
    post_reference_starts = [
        value
        for value in reference_starts
        if post_reference_start <= value <= post_reference_end
    ]

    pre_estimate = _local_speech_shift_estimate(
        pre_starts,
        pre_reference_starts,
        max_shift_seconds=12.0,
    )
    post_estimate = _local_speech_shift_estimate(
        post_starts,
        post_reference_starts,
        max_shift_seconds=3.0,
    )

    # Onset-only matching is intentionally narrow after the OP because dense
    # dialogue can create a convincing alias.  Japanese text identity is a
    # stronger independent signal and may safely reacquire a larger (<=12s)
    # post-opening clock when several actual lines agree.
    pre_semantic = _local_stt_text_shift_estimate(
        aligned_cues[:split_index],
        reference,
        max_shift_seconds=12.0,
    )
    post_semantic_cues = [
        cue
        for cue in aligned_cues[split_index:]
        if float(cue[0]) <= post_limit
    ][:48]
    post_semantic = _local_stt_text_shift_estimate(
        post_semantic_cues,
        reference,
        max_shift_seconds=12.0,
    )
    pre_shift, pre_choice = _choose_plateau_shift(
        pre_estimate,
        pre_semantic,
        onset_limit_seconds=12.0,
        semantic_limit_seconds=12.0,
        allow_sparse_cross_modal_consensus=True,
    )
    post_shift, post_choice = _choose_plateau_shift(
        post_estimate,
        post_semantic,
        onset_limit_seconds=3.0,
        semantic_limit_seconds=12.0,
    )
    if abs(pre_shift) < 0.20 and abs(post_shift) < 0.20:
        return aligned, _result(
            "stt_plateau_no_refinement",
            applied=False,
            gap_seconds=round(gap_seconds, 3),
            source_gap_midpoint=round(source_gap_midpoint, 3),
            aligned_gap_midpoint=round(aligned_gap_midpoint, 3),
            pre=pre_estimate,
            post=post_estimate,
            pre_semantic=pre_semantic,
            post_semantic=post_semantic,
            pre_choice=pre_choice,
            post_choice=post_choice,
        )

    repaired: list[tuple[float, float, str]] = []
    for index, (start, end, text) in enumerate(aligned_cues):
        shift = pre_shift if index < split_index else post_shift
        repaired.append(
            (
                max(0.0, float(start) + shift),
                max(0.05, float(end) + shift),
                text,
            )
        )

    stat = aligned.stat()
    ref_stat = reference.stat()
    digest = hashlib.sha1(
        (
            f"stt-opening-plateau-v2:"
            f"{aligned.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:"
            f"{reference.resolve()}:{ref_stat.st_size}:{ref_stat.st_mtime_ns}:"
            f"{split_index}:{pre_shift:.3f}:{post_shift:.3f}"
        ).encode()
    ).hexdigest()[:20]
    output_dir = cache_dir / "stt-opening-plateau"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{digest}.srt"
    if not output.exists() or output.stat().st_size <= 0:
        write_srt(repaired, output, preserve_order=True)

    before_activity = compare_timing_activity(aligned, reference)
    after_activity = compare_timing_activity(output, reference)
    try:
        before_weighted = float(before_activity.get("weighted") or 0.0)
        after_weighted = float(after_activity.get("weighted") or 0.0)
        before_start = float(before_activity.get("start") or 0.0)
        after_start = float(after_activity.get("start") or 0.0)
    except (TypeError, ValueError):
        before_weighted = after_weighted = before_start = after_start = 0.0

    before_text = _subtitle_stt_text_score(aligned, reference)
    after_text = _subtitle_stt_text_score(output, reference)
    try:
        before_text_rank = float(before_text.get("ranking_score") or 0.0)
        after_text_rank = float(after_text.get("ranking_score") or 0.0)
    except (TypeError, ValueError):
        before_text_rank = after_text_rank = 0.0
    pre_choice_mode = str(pre_choice.get("mode") or "")
    post_choice_mode = str(post_choice.get("mode") or "")
    semantic_override = bool(
        pre_choice_mode.startswith("semantic")
        or post_choice_mode.startswith("semantic")
    )
    semantic_conflict_override = bool(
        pre_choice_mode in {"semantic_override", "semantic_only"}
        or post_choice_mode in {"semantic_override", "semantic_only"}
    )
    semantic_text_improved = bool(
        semantic_override
        and bool(after_text.get("available"))
        and after_text_rank >= before_text_rank + 0.025
        and after_weighted + 0.030 >= before_weighted
        and after_start + 0.030 >= before_start
    )
    accepted, acceptance_meta = _accept_opening_plateau_refinement(
        pre_choice_mode=pre_choice_mode,
        post_choice_mode=post_choice_mode,
        before_weighted=before_weighted,
        after_weighted=after_weighted,
        before_start=before_start,
        after_start=after_start,
        before_text_available=bool(before_text.get("available")),
        before_text_rank=before_text_rank,
        after_text_available=bool(after_text.get("available")),
        after_text_rank=after_text_rank,
        semantic_text_improved=semantic_text_improved,
        semantic_conflict_override=semantic_conflict_override,
    )
    semantic_text_not_degraded = bool(
        acceptance_meta.get("semantic_text_not_degraded")
    )
    sparse_cross_modal_consensus = bool(
        acceptance_meta.get("sparse_cross_modal_consensus")
    )
    sparse_consensus_accepted = bool(
        acceptance_meta.get("sparse_consensus_accepted")
    )
    diagnostics = _result(
        "applied" if accepted else "stt_plateau_activity_degraded",
        applied=accepted,
        output=str(output) if accepted else str(aligned),
        gap_seconds=round(gap_seconds, 3),
        source_gap_midpoint=round(source_gap_midpoint, 3),
        aligned_gap_midpoint=round(aligned_gap_midpoint, 3),
        pre_shift_seconds=round(pre_shift, 3),
        post_shift_seconds=round(post_shift, 3),
        pre=pre_estimate,
        post=post_estimate,
        pre_semantic=pre_semantic,
        post_semantic=post_semantic,
        pre_choice=pre_choice,
        post_choice=post_choice,
        semantic_text_improved=semantic_text_improved,
        semantic_text_not_degraded=semantic_text_not_degraded,
        semantic_conflict_override=semantic_conflict_override,
        sparse_cross_modal_consensus=sparse_cross_modal_consensus,
        sparse_consensus_accepted=sparse_consensus_accepted,
        before_text=before_text,
        after_text=after_text,
        before_activity=before_activity,
        after_activity=after_activity,
    )
    if not accepted:
        output.unlink(missing_ok=True)
        return aligned, diagnostics
    return output, diagnostics


def _stt_text_segments(reference: Path) -> list[dict[str, object]]:
    """Load the cached Whisper transcript, falling back to its reference SRT."""
    transcript_path = reference.parent / "transcription.json"
    if transcript_path.is_file():
        try:
            payload = json.loads(transcript_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            payload = {}
        segments = payload.get("segments") if isinstance(payload, dict) else None
        if isinstance(segments, list):
            cleaned = [dict(row) for row in segments if isinstance(row, dict)]
            if cleaned:
                return cleaned
    try:
        cues = parse_srt(reference)
    except OSError:
        return []
    return [
        {"start": float(start), "end": float(end), "text": str(text)}
        for start, end, text in cues
        if str(text).strip()
    ]


def _subtitle_stt_text_score(aligned: Path, reference: Path) -> dict[str, object]:
    """Score whether subtitle text lands beside the same spoken Japanese text."""
    try:
        subtitle_cues = parse_srt(aligned)
        reference_cues = parse_srt(reference)
    except OSError as exc:
        return {"available": False, "reason": "read_error", "error": str(exc)}
    if not subtitle_cues or not reference_cues:
        return {"available": False, "reason": "empty_timeline"}
    weighted_score = 0.0
    total_weight = 0.0
    compared = 0
    eligible = 0
    reference_index = 0
    for start, end, text in subtitle_cues:
        source_text = normalize_reading_text(str(text))
        if len(source_text) < 2:
            continue
        eligible += 1
        left = max(0.0, float(start) - 1.25)
        right = float(end) + 1.25
        while reference_index < len(reference_cues) and float(reference_cues[reference_index][1]) < left:
            reference_index += 1
        nearby: list[str] = []
        cursor = max(0, reference_index - 1)
        while cursor < len(reference_cues):
            ref_start, ref_end, ref_text = reference_cues[cursor]
            if float(ref_start) > right:
                break
            if float(ref_end) >= left:
                normalized = normalize_reading_text(str(ref_text))
                if normalized:
                    nearby.append(normalized)
            cursor += 1
        if not nearby:
            continue
        reference_text = "".join(nearby)
        if not reference_text:
            continue
        score = max(
            float(fuzz.ratio(source_text, reference_text)),
            float(fuzz.partial_ratio(source_text, reference_text)),
        ) / 100.0
        weight = float(min(36, max(2, len(source_text))))
        weighted_score += score * weight
        total_weight += weight
        compared += 1
    coverage = compared / max(1, eligible)
    if compared < 4 or total_weight <= 0.0:
        return {
            "available": False,
            "reason": "too_few_comparisons",
            "compared_cues": compared,
            "eligible_cues": eligible,
            "coverage": round(coverage, 4),
        }
    score = weighted_score / total_weight
    ranking_score = score * min(1.0, 0.55 + coverage)
    return {
        "available": True,
        "reason": "ok",
        "score": round(score, 4),
        "ranking_score": round(ranking_score, 4),
        "coverage": round(coverage, 4),
        "compared_cues": compared,
        "eligible_cues": eligible,
    }


def _stt_text_clock_candidate(
    source: Path,
    reference: Path,
    cache_dir: Path,
    *,
    model: str,
) -> tuple[Path | None, dict[str, object]]:
    """Build a subtitle clock from semantic Japanese STT/LN-style anchors."""
    try:
        source_cues = parse_srt(source)
    except OSError as exc:
        return None, {"available": False, "accepted": False, "reason": "read_error", "error": str(exc)}
    segments = _stt_text_segments(reference)
    if not source_cues or not segments:
        return None, {"available": False, "accepted": False, "reason": "empty_timeline"}

    pieces: list[str] = []
    cue_offsets: list[tuple[int, int] | None] = []
    cursor = 0
    eligible = 0
    for _start, _end, text in source_cues:
        normalized = normalize_reading_text(str(text))
        if not normalized:
            cue_offsets.append(None)
            continue
        start_offset = cursor
        pieces.append(normalized)
        cursor += len(normalized)
        cue_offsets.append((start_offset, cursor))
        if len(normalized) >= 2:
            eligible += 1
    source_text = "".join(pieces)
    if len(source_text) < 40 or eligible < 4:
        return None, {
            "available": False,
            "accepted": False,
            "reason": "too_little_source_text",
            "eligible_cues": eligible,
        }

    segment_ends: list[float] = []
    for segment in segments:
        try:
            segment_ends.append(float(segment.get("end") or segment.get("start") or 0.0))
        except (TypeError, ValueError):
            continue
    duration = max(
        [float(source_cues[-1][1]), *segment_ends, 0.25]
    )
    try:
        alignment = align_light_novel_to_transcript(
            [{"chapter_index": 0, "title": "subtitle", "text": source_text}],
            segments,
            duration=duration,
            model=model,
        )
    except (ValueError, TypeError, KeyError) as exc:
        return None, {
            "available": False,
            "accepted": False,
            "reason": "text_alignment_failed",
            "error": str(exc),
        }

    # STT is used as a *clock for cue onsets*.  Do not use the mapped text
    # end as subtitle duration: the text-alignment curve interpolates through
    # gaps and can make a short subtitle remain visible until the next spoken
    # line (or even across an opening).  Preserve the source cue duration and
    # only move its onset, then cap it before the next mapped onset.
    mapped_starts: list[float | None] = []
    shifts: list[float] = []
    mapped = 0
    last_start = 0.0
    for cue, offsets in zip(source_cues, cue_offsets):
        source_start, _source_end, _text = cue
        if offsets is None:
            mapped_starts.append(None)
            continue
        mapped_start = audio_position_for_light_novel_offset(alignment, 0, offsets[0])
        if mapped_start is None:
            mapped_starts.append(None)
            continue
        mapped_start = max(last_start, float(mapped_start))
        mapped_starts.append(mapped_start)
        shifts.append(mapped_start - float(source_start))
        mapped += 1
        last_start = mapped_start

    next_mapped_starts: list[float | None] = [None] * len(mapped_starts)
    next_seen: float | None = None
    for index in range(len(mapped_starts) - 1, -1, -1):
        next_mapped_starts[index] = next_seen
        if mapped_starts[index] is not None:
            next_seen = mapped_starts[index]

    retimed: list[tuple[float, float, str]] = []
    for cue, mapped_start, next_mapped_start in zip(source_cues, mapped_starts, next_mapped_starts):
        source_start, source_end, text = cue
        if mapped_start is None:
            retimed.append((float(source_start), float(source_end), str(text)))
            continue
        source_duration = max(0.05, float(source_end) - float(source_start))
        mapped_end = mapped_start + source_duration
        if next_mapped_start is not None:
            mapped_end = min(mapped_end, max(mapped_start + 0.05, next_mapped_start - 0.05))
        retimed.append((mapped_start, mapped_end, str(text)))

    coverage = mapped / max(1, eligible)
    if mapped < 4 or coverage < 0.35:
        return None, {
            "available": True,
            "accepted": False,
            "reason": "too_few_mapped_cues",
            "mapped_cues": mapped,
            "eligible_cues": eligible,
            "coverage": round(coverage, 4),
            "alignment_method": alignment.get("alignment_method"),
            "matched_anchor_count": alignment.get("matched_anchor_count"),
        }

    stat = source.stat()
    ref_stat = reference.stat()
    digest = hashlib.sha1(
        (
            f"stt-text-clock-v1:{source.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:"
            f"{reference.resolve()}:{ref_stat.st_size}:{ref_stat.st_mtime_ns}:{model}"
        ).encode()
    ).hexdigest()[:20]
    output_dir = cache_dir / "stt-text-clock"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{digest}.srt"
    write_srt(retimed, output, preserve_order=True)

    transition_safety = _stt_alass_transition_safety(source, output)
    text_alignment = _subtitle_stt_text_score(output, reference)
    activity = compare_timing_activity(output, reference)
    try:
        text_score = float(text_alignment.get("score") or 0.0)
        text_coverage = float(text_alignment.get("coverage") or 0.0)
        activity_score = float(activity.get("weighted") or 0.0)
    except (TypeError, ValueError):
        text_score = text_coverage = activity_score = 0.0
    gate_failures: list[str] = []
    if not bool(transition_safety.get("accepted")):
        gate_failures.append(
            "transition_safety:" + str(transition_safety.get("reason") or "rejected")
        )
    if not bool(text_alignment.get("available")):
        gate_failures.append("text_alignment:unavailable")
    elif text_score < 0.62:
        gate_failures.append(f"text_score:{text_score:.4f}<0.6200")
    if text_coverage < 0.35:
        gate_failures.append(f"text_coverage:{text_coverage:.4f}<0.3500")
    if not bool(activity.get("available")):
        gate_failures.append("activity:unavailable")
    elif activity_score < 0.20:
        gate_failures.append(f"activity_score:{activity_score:.4f}<0.2000")
    accepted = not gate_failures
    shifts_sorted = sorted(shifts)
    median_shift = shifts_sorted[len(shifts_sorted) // 2] if shifts_sorted else 0.0
    spread = (max(shifts_sorted) - min(shifts_sorted)) if shifts_sorted else 0.0
    diagnostics = {
        "available": True,
        "accepted": accepted,
        "reason": "ok" if accepted else "text_clock_safety_gate_failed",
        "reject_reason": None if accepted else gate_failures[0],
        "gate_failures": gate_failures,
        "output": str(output) if accepted else None,
        "rejected_output": None if accepted else str(output),
        "alignment_method": alignment.get("alignment_method"),
        "fuzzy_similarity": alignment.get("fuzzy_similarity"),
        "matched_anchor_count": alignment.get("matched_anchor_count"),
        "anchor_count": alignment.get("anchor_count"),
        "segment_count": len(segments),
        "frontier": alignment.get("frontier"),
        "frontier_reason": alignment.get("frontier_reason"),
        "confidence": alignment.get("confidence"),
        "build_stages": [
            "source_parsed", "stt_segments_loaded", "semantic_alignment_built",
            "cue_clock_mapped", "transition_safety_checked",
            "semantic_score_checked", "activity_checked",
        ],
        "mapped_cues": mapped,
        "eligible_cues": eligible,
        "coverage": round(coverage, 4),
        "median_shift_seconds": round(float(median_shift), 3),
        "shift_spread_seconds": round(float(spread), 3),
        "transition_safety": transition_safety,
        "text_alignment": text_alignment,
        "activity": activity,
    }
    if not accepted:
        # Preserve the rejected clock: it is small and invaluable for benchmark/debug.
        return None, diagnostics
    return output, diagnostics

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
        ffprobe_path=ffprobe_path,
        model=config.japanese_stt_model,
        timeout_seconds=config.japanese_stt_timeout_seconds,
        # Transcription is content-addressed and expensive; force-searching
        # subtitle providers must not invalidate the speech cache.
        force=False,
    )
    if reference is None:
        return None, _result(
            "stt_reference_unavailable",
            sync_was_successful=False,
            stt=stt,
        )

    source = subtitle
    if source.suffix.casefold() in {".ass", ".ssa"}:
        source, conversion = convert_to_plain_srt(
            source,
            cache_dir,
            ffmpeg_path=ffmpeg_path,
            force=False,
            verbose=verbose,
        )
        if source.suffix.casefold() != ".srt":
            return None, _result(
                "stt_source_conversion_failed",
                sync_was_successful=False,
                stt=stt,
                source_conversion=conversion,
            )

    if config.japanese_stt_text_clock:
        text_aligned, stt_text_alignment = _stt_text_clock_candidate(
            source,
            reference,
            cache_dir,
            model=config.japanese_stt_model,
        )
    else:
        text_aligned = None
        stt_text_alignment = {
            "attempted": False,
            "accepted": False,
            "reason": "text_clock_disabled",
        }
    if text_aligned is not None and bool(stt_text_alignment.get("accepted")):
        text_activity = stt_text_alignment.get("activity")
        try:
            text_activity_score = float(text_activity.get("weighted") or 0.0) if isinstance(text_activity, dict) else 0.0
        except (TypeError, ValueError):
            text_activity_score = 0.0
        return text_aligned, _result(
            "applied",
            sync_was_successful=True,
            engine="japanese-stt+text-clock",
            selection_reason="last_resort_cached_stt_text_clock",
            timing_reference=str(reference),
            timing_reference_language="ja",
            reference_activity=text_activity,
            reference_activity_score=text_activity_score,
            reference_alignment_reliable=True,
            stt=stt,
            stt_text_alignment=stt_text_alignment,
            output=str(text_aligned),
        )

    penalties: list[float] = []
    for value in (
        float(config.alass_split_penalty),
        max(18.0, float(config.alass_split_penalty)),
        max(35.0, float(config.alass_split_penalty)),
    ):
        if not any(abs(value - existing) < 1e-6 for existing in penalties):
            penalties.append(value)

    attempts: list[dict[str, object]] = []
    last_result: dict[str, object] = _result(
        "stt_alass_no_safe_map",
        sync_was_successful=False,
        stt=stt,
    )

    for split_penalty in penalties:
        attempt_config = replace(
            config,
            alass_split_penalty=float(split_penalty),
        )
        aligned, result = synchronize_with_alass(
            reference,
            source,
            cache_dir,
            attempt_config,
            alass_path=alass_path,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            force=False,
            verbose=verbose,
        )
        result = dict(result)
        result["stt_alass_split_penalty"] = float(split_penalty)

        if not bool(result.get("sync_was_successful")):
            attempts.append(
                {
                    "split_penalty": float(split_penalty),
                    "accepted": False,
                    "reason": result.get("reason"),
                }
            )
            last_result = result
            continue

        activity = compare_timing_activity(aligned, reference)
        try:
            score = float(activity.get("weighted") or 0.0)
        except (TypeError, ValueError):
            score = 0.0

        transition_safety = _stt_alass_transition_safety(source, aligned)
        map_safe, map_reason = _stt_alass_map_safe(result, transition_safety)
        activity_safe = bool(activity.get("available")) and score >= config.japanese_stt_min_activity
        accepted = bool(map_safe and activity_safe)

        attempt_payload = {
            "split_penalty": float(split_penalty),
            "accepted": accepted,
            "reason": (
                "ok"
                if accepted
                else (
                    map_reason
                    if not map_safe
                    else "stt_activity_gate_failed"
                )
            ),
            "blocks": result.get("alass_blocks"),
            "distinct_shifts": result.get("alass_distinct_shifts"),
            "shift_spread_seconds": result.get("alass_shift_spread_seconds"),
            "activity": round(score, 4),
            "transition_safety": transition_safety,
        }
        attempts.append(attempt_payload)

        result["stt"] = stt
        result["stt_text_alignment"] = stt_text_alignment
        result["reference_activity"] = activity
        result["stt_alass_transition_safety"] = transition_safety
        result["stt_alass_map_reason"] = map_reason
        result["stt_alass_attempts"] = list(attempts)
        last_result = result

        if not accepted:
            continue

        refined, plateau_refinement = _refine_stt_opening_plateaus(
            source,
            aligned,
            reference,
            cache_dir,
        )
        if plateau_refinement.get("applied"):
            aligned = refined
            activity = compare_timing_activity(aligned, reference)
            try:
                score = float(activity.get("weighted") or 0.0)
            except (TypeError, ValueError):
                score = 0.0

        result.update(
            {
                "engine": (
                    "japanese-stt+alass+opening-plateau"
                    if plateau_refinement.get("applied")
                    else "japanese-stt+alass"
                ),
                "reason": "applied",
                "selection_reason": "last_resort_cached_stt",
                "timing_reference": str(reference),
                "timing_reference_language": "ja",
                "reference_activity": activity,
                "reference_activity_score": score,
                "reference_alignment_reliable": True,
                "stt_opening_plateau_refinement": plateau_refinement,
                "output": str(aligned),
            }
        )
        return aligned, result

    last_result.update(
        {
            "sync_was_successful": False,
            "reason": "stt_alass_no_safe_map",
            "stt": stt,
            "stt_text_alignment": stt_text_alignment,
            "stt_alass_attempts": attempts,
        }
    )
    return None, last_result

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

            exact_release_result = _exact_release_zero_offset_result(
                video, subtitle, timeline_source
            )
            if timing_reference is not None and exact_release_result is not None:
                alass_out = timeline_source
                alass_result = exact_release_result
                timeline_attempt = dict(alass_result)
                _record_timeline_debug_attempt(
                    cache_dir, video, timeline_source, timing_reference, timeline_attempt,
                    stage="synchronize_subtitle_exact_release",
                )
            elif timing_reference is not None and timeline_source.suffix.casefold() == ".srt":
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


def _timeline_validation_regression(
    result: dict[str, object],
) -> tuple[bool, dict[str, object]]:
    """Detect a timeline remap that is strictly worse than leaving cues alone.

    A piecewise edit can trade one global metric for a genuine local repair, so
    no single regression is enough.  Reject only when matched count, coverage
    and F1 all fall while mean onset error also rises by a non-trivial amount.
    That is strong evidence that the timeline optimizer manufactured a clock
    change instead of improving the candidate.
    """
    validation = result.get("timeline_validation")
    if not isinstance(validation, dict):
        return False, {"reason": "timeline_validation_unavailable"}
    before = validation.get("before")
    after = validation.get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False, {"reason": "timeline_before_after_unavailable"}
    try:
        before_matched = int(before.get("matched") or 0)
        after_matched = int(after.get("matched") or 0)
        before_coverage = float(before.get("coverage") or 0.0)
        after_coverage = float(after.get("coverage") or 0.0)
        before_f1 = float(before.get("f1") or 0.0)
        after_f1 = float(after.get("f1") or 0.0)
        before_error = float(before.get("mean_error_seconds"))
        after_error = float(after.get("mean_error_seconds"))
    except (TypeError, ValueError):
        return False, {"reason": "timeline_before_after_not_numeric"}

    regressed = bool(
        after_matched < before_matched
        and after_coverage <= before_coverage - 0.005
        and after_f1 <= before_f1 - 0.005
        and after_error >= before_error + 0.01
    )
    return regressed, {
        "reason": "timeline_remap_strictly_worse" if regressed else "ok",
        "before_matched": before_matched,
        "after_matched": after_matched,
        "before_coverage": round(before_coverage, 4),
        "after_coverage": round(after_coverage, 4),
        "before_f1": round(before_f1, 4),
        "after_f1": round(after_f1, 4),
        "before_mean_error_seconds": round(before_error, 4),
        "after_mean_error_seconds": round(after_error, 4),
    }


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
        timeline_regressed, regression = _timeline_validation_regression(result)
        if timeline_regressed:
            result["timeline_validation_regression"] = regression
            return (
                False,
                "timeline-remap ухудшил исходный тайминг по matched/coverage/F1/error; "
                "нужна другая временная опора",
            )
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
_EMBEDDED_REFERENCE_SRT_MIN_ACTIVITY = 0.80
_EMBEDDED_REFERENCE_SRT_MAX_ACTIVITY_GAP = 0.12
_EMBEDDED_REFERENCE_LANGUAGE_MIN_ACTIVITY = 0.65


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
    """Prefer clean Japanese and native SRT without accepting a broken clock.

    Explicit Japanese-only files outrank unknown files, and mixed Japanese/Chinese
    files are fallbacks. Inside the best viable language tier, a native SRT wins
    when its activity is still strong and no more than 0.12 below the best clock.
    """
    if not items:
        return [], {
            "activity_tolerance": max(0.0, float(activity_tolerance)),
            "best_activity": None,
            "format_preference_applied": False,
            "language_preference_applied": False,
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

    def language_purity(item: tuple[object, ...]) -> str:
        candidate = item[1]
        details = candidate.details if isinstance(candidate.details, dict) else {}
        return str(details.get("language_purity") or "unknown")

    def language_priority(item: tuple[object, ...]) -> int:
        return {
            "japanese_only": 2,
            "unknown": 1,
            "mixed_japanese_chinese": 0,
            "chinese_only": 0,
        }.get(language_purity(item), 1)

    structurally_valid = [item for item in items if structure_ok(item)]
    activity_pool = structurally_valid or items
    best_activity = max((activity_value(item) for item in activity_pool), default=0.0)

    viable_language_pool = [
        item
        for item in structurally_valid
        if activity_value(item) >= _EMBEDDED_REFERENCE_LANGUAGE_MIN_ACTIVITY
    ]
    language_pool = viable_language_pool or structurally_valid or items
    best_language_priority = max(
        (language_priority(item) for item in language_pool),
        default=1,
    )
    best_language_purity = max(
        (language_purity(item) for item in language_pool),
        key=lambda value: {
            "japanese_only": 2,
            "unknown": 1,
            "mixed_japanese_chinese": 0,
            "chinese_only": 0,
        }.get(value, 1),
        default="unknown",
    )
    same_language_pool = [
        item
        for item in language_pool
        if language_priority(item) == best_language_priority
    ]
    best_same_language_activity = max(
        (activity_value(item) for item in same_language_pool),
        default=best_activity,
    )

    rebuilt = []
    for _old_rank, candidate, aligned, alass_result, activity, structure in items:
        item = (_old_rank, candidate, aligned, alass_result, activity, structure)
        weighted = activity_value(item)
        valid = structure_ok(item)
        purity_priority = language_priority(item)
        best_purity = purity_priority == best_language_priority
        native_srt = bool(prefer_srt and candidate.path.suffix.casefold() == ".srt")
        activity_gap = max(0.0, best_same_language_activity - weighted)
        near_best = valid and best_purity and activity_gap <= tolerance
        acceptable_srt = bool(
            native_srt
            and valid
            and best_purity
            and weighted >= _EMBEDDED_REFERENCE_SRT_MIN_ACTIVITY
            and activity_gap <= _EMBEDDED_REFERENCE_SRT_MAX_ACTIVITY_GAP
        )
        format_preferred = bool(native_srt and best_purity and (near_best or acceptable_srt))

        rank = (
            1.0 if valid else 0.0,
            1.0 if best_purity else 0.0,
            float(purity_priority),
            1.0 if format_preferred else 0.0,
            weighted,
            float(candidate.score),
        )
        rebuilt.append((rank, candidate, aligned, alass_result, activity, structure))

    ordered = sorted(rebuilt, key=lambda item: item[0], reverse=True)
    selected = ordered[0] if ordered else None
    selected_activity = activity_value(selected) if selected is not None else 0.0
    selected_purity = language_purity(selected) if selected is not None else "unknown"
    selected_priority = language_priority(selected) if selected is not None else 1
    selected_format_preferred = bool(selected and selected[0][3] > 0.0)
    best_raw = max(items, key=activity_value) if items else None
    best_raw_candidate = best_raw[1] if best_raw is not None else None
    best_raw_purity = language_purity(best_raw) if best_raw is not None else "unknown"
    best_raw_priority = language_priority(best_raw) if best_raw is not None else 1
    selected_candidate = selected[1] if selected is not None else None
    preference_applied = bool(
        prefer_srt
        and selected_candidate is not None
        and selected_candidate.path.suffix.casefold() == ".srt"
        and best_raw_candidate is not None
        and best_raw_candidate.path != selected_candidate.path
        and selected_format_preferred
    )
    return ordered, {
        "activity_tolerance": tolerance,
        "srt_min_activity": _EMBEDDED_REFERENCE_SRT_MIN_ACTIVITY,
        "srt_max_activity_gap": _EMBEDDED_REFERENCE_SRT_MAX_ACTIVITY_GAP,
        "best_activity": round(best_activity, 6),
        "best_same_language_activity": round(best_same_language_activity, 6),
        "selected_activity": round(selected_activity, 6),
        "selected_activity_gap": round(max(0.0, best_same_language_activity - selected_activity), 6),
        "best_language_purity": best_language_purity,
        "selected_language_purity": selected_purity,
        "raw_best_language_purity": best_raw_purity,
        "format_preference_applied": preference_applied,
        "language_preference_applied": selected_priority > best_raw_priority,
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
            and _candidate_has_exact_anilist_identity(candidate)
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
    # A noisy embedded English reference can depress every independent Jimaku
    # score in exactly the same way (signs/accessibility cues are the common
    # culprit).  In that case compare the already aligned Japanese candidates
    # to each other.  Three complete exact-episode releases which share one
    # clock are stronger evidence than a noisy cross-language activity sample.
    mutual_scores: list[float] = []
    for index, (_score, left) in enumerate(qualified):
        for _other_score, right in qualified[index + 1 :]:
            mutual = compare_timing_activity(left[2], right[2])
            if mutual.get("available"):
                try:
                    mutual_scores.append(float(mutual.get("weighted") or 0.0))
                except (TypeError, ValueError):
                    pass
    mutual_median = statistics.median(mutual_scores) if mutual_scores else 0.0
    payload.update(
        {
            "median_activity": round(median_score, 4),
            "best_activity": round(best_score, 4),
            "mutual_activity_scores": [round(score, 4) for score in mutual_scores],
            "mutual_activity_median": round(mutual_median, 4),
        }
    )
    reference_consensus = median_score >= 0.88 and best_score >= 0.91
    mutual_clock_consensus = len(mutual_scores) >= 3 and mutual_median >= 0.90
    if not reference_consensus and not mutual_clock_consensus:
        payload["reason"] = "exact_timing_consensus_too_weak"
        return None, payload

    selected = max((item for _score, item in qualified), key=lambda item: item[0])
    payload.update(
        {
            "accepted": True,
            "reason": (
                "exact_jimaku_timing_consensus"
                if reference_consensus
                else "exact_jimaku_mutual_clock_consensus"
            ),
            "selected": selected[1].name,
        }
    )
    return selected, payload


def _exact_jimaku_audio_clock_consensus(
    items: list[tuple[SubtitleCandidate, Path, dict[str, object]]],
    *,
    prefer_srt: bool = True,
) -> tuple[tuple[SubtitleCandidate, Path, dict[str, object]] | None, dict[str, object]]:
    """Trust three exact releases when independent audio offsets agree tightly."""
    qualified: list[tuple[float, SubtitleCandidate, Path, dict[str, object]]] = []
    for candidate, output, result in items:
        details = candidate.details
        try:
            offset = float(result.get("offset_seconds"))
        except (TypeError, ValueError):
            continue
        if not (
            bool(result.get("sync_was_successful"))
            and abs(offset) <= 2.0
            and candidate.source == "jimaku"
            and details.get("episode_match") == "exact"
            and _candidate_has_exact_anilist_identity(candidate)
            and float(candidate.score) >= 75.0
        ):
            continue
        qualified.append((offset, candidate, output, result))

    offsets = sorted(offset for offset, *_rest in qualified)
    payload: dict[str, object] = {
        "accepted": False,
        "reason": "insufficient_exact_audio_clock_consensus",
        "qualified_candidates": len(qualified),
        "offsets_seconds": [round(value, 4) for value in offsets],
    }
    if len(qualified) < 3:
        return None, payload
    spread = max(offsets) - min(offsets)
    payload["offset_spread_seconds"] = round(spread, 4)
    if spread > 0.50:
        payload["reason"] = "exact_audio_clock_consensus_too_wide"
        return None, payload

    selected = max(
        qualified,
        key=lambda item: (
            int(item[1].details.get("language_purity") == "japanese_only"),
            int(item[1].details.get("language_purity") != "mixed_japanese_chinese"),
            int(prefer_srt and item[1].path.suffix.casefold() == ".srt"),
            float(item[1].score),
            -abs(item[0]),
        ),
    )
    payload.update(
        {
            "accepted": True,
            "reason": "exact_jimaku_audio_clock_consensus",
            "selected": selected[1].name,
        }
    )
    return (selected[1], selected[2], selected[3]), payload


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

                exact_release_result = _exact_release_zero_offset_result(
                    video, candidate.path, semantic_source
                )
                if exact_release_result is not None:
                    aligned = semantic_source
                    alass_result = exact_release_result
                    timeline_attempt = dict(alass_result)
                    debug_stage = f"optimize_candidate_{index}_exact_release"
                else:
                    aligned, alass_result = align_subtitle_timelines(
                        semantic_source,
                        timing_reference,
                        cache_dir,
                        max_offset_seconds=config.max_offset_seconds,
                        force=force,
                    )
                    timeline_attempt = dict(alass_result)
                    debug_stage = f"optimize_candidate_{index}"
                _record_timeline_debug_attempt(
                    cache_dir, video, semantic_source, timing_reference, timeline_attempt,
                    stage=debug_stage,
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
                    "video=%s raw_best=%r selected=%r best_activity=%s selected_activity=%s "
                    "activity_gap=%s max_gap=%s language=%s",
                    video.name,
                    embedded_rank_meta.get("raw_best"),
                    embedded_rank_meta.get("selected"),
                    embedded_rank_meta.get("best_activity"),
                    embedded_rank_meta.get("selected_activity"),
                    embedded_rank_meta.get("selected_activity_gap"),
                    embedded_rank_meta.get("srt_max_activity_gap"),
                    embedded_rank_meta.get("selected_language_purity"),
                )
                if verbose:
                    print(
                        "  Выбран чистый SRT с приемлемым качеством тайминга: "
                        f"{embedded_rank_meta.get('selected')}"
                    )
            risky_timeline_item = next(
                (
                    item
                    for item in sorted_prealigned
                    if bool(item[3].get("timeline_alignment_reliable"))
                    and _timeline_needs_audio_verification(item[3])
                    and isinstance(item[5], dict)
                    and item[5].get("reason") == "ok"
                    and (
                        (
                            item[1].source == "jimaku"
                            and item[1].details.get("episode_match") == "exact"
                            and _candidate_has_exact_anilist_identity(item[1])
                        )
                        or (
                            isinstance(item[3].get("timeline_validation"), dict)
                            and (
                                float(
                                    item[3]["timeline_validation"]
                                    .get("after", {})
                                    .get("f1", 0.0)
                                    or 0.0
                                ) >= 0.72
                                or bool(
                                    item[3]["timeline_validation"].get(
                                        "layered_reference_acceptance"
                                    )
                                )
                            )
                            and float(
                                item[3]["timeline_validation"].get("activity_f1", 0.0)
                                or 0.0
                            ) >= 0.72
                        )
                    )
                ),
                None,
            )
            risky_timeline_salvaged = False
            if risky_timeline_item is None:
                for item in sorted_prealigned:
                    attempt = item[3].get("timeline_alignment_attempt")
                    if not isinstance(attempt, dict):
                        continue
                    if not (
                        isinstance(item[5], dict)
                        and item[5].get("reason") == "ok"
                        and item[1].source == "jimaku"
                        and item[1].details.get("episode_match") == "exact"
                        and _candidate_has_exact_anilist_identity(item[1])
                    ):
                        continue
                    salvaged = _salvage_sparse_preopening_timeline_attempt(attempt)
                    if salvaged is None:
                        continue
                    risky_timeline_item = (
                        item[0],
                        item[1],
                        item[2],
                        salvaged,
                        item[4],
                        item[5],
                    )
                    risky_timeline_salvaged = True
                    configure_logging().info(
                        "VERIFY step=subtitle.unstable_sparse_preop video=%s candidate=%r salvage=%s",
                        video.name,
                        item[1].name,
                        salvaged.get("timeline_unstable_sparse_preopening_salvage"),
                    )
                    break
            deterministic_timeline_item = next(
                (
                    item
                    for item in sorted_prealigned
                    if bool(item[3].get("timeline_alignment_reliable"))
                    and not _timeline_needs_audio_verification(item[3])
                    and isinstance(item[5], dict)
                    and item[5].get("reason") == "ok"
                    and (
                        (
                            item[1].source == "jimaku"
                            and item[1].details.get("episode_match") == "exact"
                            and _candidate_has_exact_anilist_identity(item[1])
                        )
                        or (
                            isinstance(item[3].get("timeline_validation"), dict)
                            and (
                                float(
                                    item[3]["timeline_validation"]
                                    .get("after", {})
                                    .get("f1", 0.0)
                                    or 0.0
                                ) >= 0.72
                                or bool(
                                    item[3]["timeline_validation"].get(
                                        "layered_reference_acceptance"
                                    )
                                )
                            )
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

            if risky_timeline_item is not None:
                (
                    _risk_rank,
                    risky_candidate,
                    _embedded_aligned,
                    risky_timeline_result,
                    _risk_activity,
                    _risk_structure,
                ) = risky_timeline_item
                risk_payload = risky_timeline_result.get(
                    "timeline_early_edit_audio_verification"
                )
                configure_logging().info(
                    "VERIFY step=subtitle.early_edit_speech video=%s candidate=%r risk=%s",
                    video.name,
                    risky_candidate.name,
                    risk_payload,
                )
                speech_aligned, speech_result = _try_japanese_stt_fallback(
                    video,
                    risky_candidate.path,
                    cache_dir,
                    config,
                    ffmpeg_path=ffmpeg_path,
                    ffprobe_path=ffprobe_path,
                    alass_path=alass_path,
                    verbose=verbose,
                )
                if (
                    speech_aligned is not None
                    and bool(speech_result.get("sync_was_successful"))
                    and bool(speech_result.get("reference_alignment_reliable"))
                ):
                    salvaged_repair: dict[str, object] | None = None
                    if risky_timeline_salvaged:
                        repaired_baseline, salvaged_repair = (
                            _repair_salvaged_sparse_preop_after_bad_stt_map(
                                _embedded_aligned,
                                risky_timeline_result,
                                speech_result,
                                timing_reference,
                                cache_dir,
                            )
                        )
                        if bool(salvaged_repair.get("applied")):
                            final_result = dict(risky_timeline_result)
                            final_result.update(
                                {
                                    "reason": "applied",
                                    "sync_was_successful": True,
                                    "engine": "embedded-reference+alass+salvaged-sparse-preop",
                                    "output": str(repaired_baseline),
                                    "timing_reference": str(timing_reference),
                                    "timing_reference_language": timing_reference_result.get("language"),
                                    "timing_reference_title": timing_reference_result.get("title"),
                                    "reference_alignment_reliable": True,
                                    "selection_reason": "salvaged_sparse_preop_dual_reference",
                                    "salvaged_sparse_preop_repair": salvaged_repair,
                                    "speech_verification": {
                                        "engine": speech_result.get("engine"),
                                        "offset_seconds": speech_result.get("offset_seconds"),
                                        "stt_alass_transition_safety": speech_result.get(
                                            "stt_alass_transition_safety"
                                        ),
                                    },
                                    "candidate_selection": {
                                        "mode": "salvaged_sparse_preop_dual_reference",
                                        "candidate_count": len(candidate_list),
                                        "prealigned_count": len(prealigned),
                                        "prefer_srt": prefer_srt,
                                        "embedded_reference_ranking": embedded_rank_meta,
                                    },
                                }
                            )
                            configure_logging().warning(
                                "OVERRIDE step=subtitle.salvaged_sparse_preop video=%s candidate=%r "
                                "stt_jump=%s expected_delta=%s refinement=%s",
                                video.name,
                                risky_candidate.name,
                                salvaged_repair.get("largest_stt_jump_seconds"),
                                salvaged_repair.get("expected_clock_delta_seconds"),
                                (
                                    salvaged_repair.get("refinement", {}).get("reason")
                                    if isinstance(salvaged_repair.get("refinement"), dict)
                                    else None
                                ),
                            )
                            return risky_candidate, repaired_baseline, final_result
                        if bool(salvaged_repair.get("conflict_detected")):
                            configure_logging().warning(
                                "REJECT step=subtitle.salvaged_sparse_preop_bad_stt_map "
                                "video=%s candidate=%r reason=%s stt_jump=%s expected_delta=%s",
                                video.name,
                                risky_candidate.name,
                                salvaged_repair.get("reason"),
                                salvaged_repair.get("largest_stt_jump_seconds"),
                                salvaged_repair.get("expected_clock_delta_seconds"),
                            )
                            speech_aligned = None
                            speech_result = dict(speech_result)
                            speech_result.update(
                                {
                                    "sync_was_successful": False,
                                    "reference_alignment_reliable": False,
                                    "reason": "salvaged_sparse_preop_bad_stt_map",
                                    "salvaged_sparse_preop_repair": salvaged_repair,
                                }
                            )

                    if speech_aligned is None:
                        pass
                    else:
                        speech_aligned, opening_scaffold = (
                            _restore_embedded_opening_clock_scaffold(
                                speech_aligned,
                                risky_timeline_result,
                                speech_result,
                                cache_dir,
                                embedded_reference=timing_reference,
                            )
                        )
                        prefer_embedded, conflict_meta = (
                            _prefer_embedded_timeline_over_conflicting_speech(
                                risky_timeline_result,
                                speech_result,
                                opening_scaffold,
                            )
                        )
                        if prefer_embedded and not risky_timeline_salvaged:
                            final_result = dict(risky_timeline_result)
                            final_result.update(
                                {
                                    "reason": "applied",
                                    "sync_was_successful": True,
                                    "engine": "embedded-reference+timeline",
                                    "output": str(_embedded_aligned),
                                    "timing_reference": str(timing_reference),
                                    "timing_reference_language": timing_reference_result.get("language"),
                                    "timing_reference_title": timing_reference_result.get("title"),
                                    "reference_activity": _risk_activity,
                                    "reference_output_structure": _risk_structure,
                                    "reference_alignment_reliable": True,
                                    "selection_reason": "embedded_timeline_over_conflicting_stt",
                                    "speech_clock_conflict": conflict_meta,
                                    "speech_verification": {
                                        "engine": speech_result.get("engine"),
                                        "offset_seconds": speech_result.get("offset_seconds"),
                                        "opening_scaffold": opening_scaffold,
                                    },
                                    "candidate_selection": {
                                        "mode": "embedded_timeline_over_conflicting_stt",
                                        "candidate_count": len(candidate_list),
                                        "prealigned_count": len(prealigned),
                                        "prefer_srt": prefer_srt,
                                        "embedded_reference_ranking": embedded_rank_meta,
                                    },
                                },
                            )
                            configure_logging().warning(
                                "OVERRIDE step=subtitle.stt_clock_conflict video=%s candidate=%r speech_offset=%s timeline_post_offset=%s conflict=%s holdout_p90=%s",
                                video.name,
                                risky_candidate.name,
                                conflict_meta.get("speech_offset_seconds"),
                                conflict_meta.get("post_offset_seconds"),
                                conflict_meta.get("clock_conflict_seconds"),
                                conflict_meta.get("holdout_p90_seconds"),
                            )
                            return risky_candidate, _embedded_aligned, final_result

                        final_result = dict(speech_result)
                        if opening_scaffold.get("applied"):
                            final_result["engine"] = (
                                f"{final_result.get('engine') or 'japanese-stt+alass'}"
                                "+embedded-opening-scaffold"
                            )
                        final_result.update(
                            {
                                "reason": "applied",
                                "sync_was_successful": True,
                                "output": str(speech_aligned),
                                "selection_reason": "early_edit_japanese_speech_verification",
                                "embedded_timeline_attempt": dict(risky_timeline_result),
                                "timeline_early_edit_audio_verification": risk_payload,
                                "embedded_opening_clock_scaffold": opening_scaffold,
                                "candidate_selection": {
                                    "mode": "early_edit_japanese_speech_verification",
                                    "candidate_count": len(candidate_list),
                                    "prealigned_count": len(prealigned),
                                    "prefer_srt": prefer_srt,
                                    "embedded_reference_ranking": embedded_rank_meta,
                                },
                            },
                        )
                        configure_logging().info(
                            "ACCEPT step=subtitle.early_edit_speech video=%s candidate=%r engine=%s activity=%s",
                            video.name,
                            risky_candidate.name,
                            final_result.get("engine"),
                            (final_result.get("reference_activity") or {}).get("weighted")
                            if isinstance(final_result.get("reference_activity"), dict)
                            else None,
                        )
                        return risky_candidate, speech_aligned, final_result
                configure_logging().warning(
                    "FALLBACK step=subtitle.early_edit_speech video=%s candidate=%r reason=%s",
                    video.name,
                    risky_candidate.name,
                    speech_result.get("reason"),
                )
                use_embedded, unsafe_stt_meta = (
                    (False, {"reason": "salvaged_timeline_is_trigger_only"})
                    if risky_timeline_salvaged
                    else _strong_embedded_timeline_after_unsafe_stt(
                        risky_timeline_result,
                        speech_result,
                    )
                )
                if use_embedded:
                    final_result = dict(risky_timeline_result)
                    final_result.update(
                        {
                            "reason": "applied",
                            "sync_was_successful": True,
                            "engine": "embedded-reference+timeline",
                            "output": str(_embedded_aligned),
                            "timing_reference": str(timing_reference),
                            "timing_reference_language": timing_reference_result.get("language"),
                            "timing_reference_title": timing_reference_result.get("title"),
                            "reference_activity": _risk_activity,
                            "reference_output_structure": _risk_structure,
                            "reference_alignment_reliable": True,
                            "selection_reason": "embedded_timeline_after_unsafe_stt_map",
                            "unsafe_stt_map": unsafe_stt_meta,
                            "speech_verification": {
                                "reason": speech_result.get("reason"),
                                "attempts": speech_result.get("stt_alass_attempts"),
                            },
                            "candidate_selection": {
                                "mode": "embedded_timeline_after_unsafe_stt_map",
                                "candidate_count": len(candidate_list),
                                "prealigned_count": len(prealigned),
                                "prefer_srt": prefer_srt,
                                "embedded_reference_ranking": embedded_rank_meta,
                            },
                        }
                    )
                    configure_logging().warning(
                        "OVERRIDE step=subtitle.unsafe_stt_map video=%s candidate=%r "
                        "largest_jump=%s transient_excursions=%s holdout_p90=%s",
                        video.name,
                        risky_candidate.name,
                        unsafe_stt_meta.get("largest_unsupported_jump_seconds"),
                        unsafe_stt_meta.get("transient_excursions"),
                        unsafe_stt_meta.get("timeline_holdout_p90_seconds"),
                    )
                    return risky_candidate, _embedded_aligned, final_result

            embedded_consensus_pool = [
                item
                for item in sorted_prealigned
                if not _timeline_needs_audio_verification(item[3])
            ]
            consensus_item, consensus = _exact_jimaku_timing_consensus(
                embedded_consensus_pool
            )
            if consensus_item is not None:
                _rank, candidate, aligned, alass_result, activity, structure = consensus_item

                # A strong mutual subtitle consensus can still share the same wrong
                # pre-opening clock (for example, different releases cut the cold
                # open identically).  If the rejected timeline attempt carries a
                # sparse-preopening signal, require Japanese speech to arbitrate the
                # first dialogue cues before trusting the global subtitle clock.
                consensus_attempt = alass_result.get("timeline_alignment_attempt")
                if isinstance(consensus_attempt, dict):
                    salvaged_consensus = _salvage_sparse_preopening_timeline_attempt(
                        consensus_attempt
                    )
                    if salvaged_consensus is not None:
                        speech_aligned, speech_result = _try_japanese_stt_fallback(
                            video,
                            candidate.path,
                            cache_dir,
                            config,
                            ffmpeg_path=ffmpeg_path,
                            ffprobe_path=ffprobe_path,
                            alass_path=alass_path,
                            verbose=verbose,
                        )
                        if (
                            speech_aligned is not None
                            and bool(speech_result.get("sync_was_successful"))
                            and bool(speech_result.get("reference_alignment_reliable"))
                        ):
                            repaired_consensus, consensus_repair = (
                                _repair_salvaged_sparse_preop_after_bad_stt_map(
                                    aligned,
                                    salvaged_consensus,
                                    speech_result,
                                    timing_reference,
                                    cache_dir,
                                )
                            )
                            if bool(consensus_repair.get("applied")):
                                repaired_activity = compare_timing_activity(
                                    repaired_consensus, timing_reference
                                )
                                final_result = dict(alass_result)
                                final_result.update(
                                    {
                                        "reason": "applied",
                                        "sync_was_successful": True,
                                        "engine": "embedded-reference+alass+trusted-clock+salvaged-sparse-preop",
                                        "output": str(repaired_consensus),
                                        "timing_reference": str(timing_reference),
                                        "timing_reference_language": timing_reference_result.get("language"),
                                        "timing_reference_title": timing_reference_result.get("title"),
                                        "reference_activity": repaired_activity,
                                        "reference_output_structure": structure,
                                        "reference_alignment_reliable": True,
                                        "selection_reason": "trusted_clock_with_salvaged_sparse_preop",
                                        "salvaged_sparse_preop_repair": consensus_repair,
                                        "speech_verification": {
                                            "engine": speech_result.get("engine"),
                                            "offset_seconds": speech_result.get("offset_seconds"),
                                            "stt_alass_transition_safety": speech_result.get(
                                                "stt_alass_transition_safety"
                                            ),
                                        },
                                    }
                                )
                                configure_logging().warning(
                                    "OVERRIDE step=subtitle.trusted_clock_sparse_preop "
                                    "video=%s candidate=%r refinement=%s",
                                    video.name,
                                    candidate.name,
                                    (
                                        consensus_repair.get("refinement", {}).get("reason")
                                        if isinstance(consensus_repair.get("refinement"), dict)
                                        else None
                                    ),
                                )
                                return candidate, repaired_consensus, final_result

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
                [
                    item
                    for item in sorted_prealigned
                    if not _timeline_needs_audio_verification(item[3])
                ][:5]
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
    audio_consensus_item, audio_consensus = _exact_jimaku_audio_clock_consensus(
        evaluated, prefer_srt=prefer_srt
    )
    if audio_consensus_item is not None:
        candidate, output, evaluation_result = audio_consensus_item
        final_result = dict(evaluation_result)
        final_result.update(
            {
                "reason": "applied",
                "sync_was_successful": True,
                "engine": "ffsubsync+trusted-exact-jimaku-clock",
                "output": str(output),
                "selection_reason": "exact_jimaku_audio_clock_consensus",
                "candidate_selection": {
                    "mode": "exact_jimaku_audio_clock_consensus",
                    "candidate_count": len(candidate_list),
                    "consensus": audio_consensus,
                },
                "reference_alignment_reliable": True,
            }
        )
        configure_logging().info(
            "ACCEPT step=subtitle.audio_clock_consensus video=%s candidate=%r offsets=%s spread=%s",
            video.name, candidate.name,
            audio_consensus.get("offsets_seconds"),
            audio_consensus.get("offset_spread_seconds"),
        )
        return candidate, output, final_result
    selection_pool = successful_evaluated or evaluated

    def alignment_value(item: tuple[SubtitleCandidate, Path, dict[str, object]]) -> float:
        raw = item[2].get("alignment_score")
        try:
            return float(raw) if raw is not None else float("-inf")
        except (TypeError, ValueError):
            return float("-inf")

    def candidate_language_priority(
        item: tuple[SubtitleCandidate, Path, dict[str, object]],
    ) -> int:
        candidate = item[0]
        details = candidate.details if isinstance(candidate.details, dict) else {}
        purity = str(details.get("language_purity") or "unknown")
        return {
            "japanese_only": 2,
            "unknown": 1,
            "mixed_japanese_chinese": 0,
            "chinese_only": 0,
        }.get(purity, 1)

    # Language purity is a hard preference tier, not just a filename bonus.
    # Otherwise a bilingual CHS+JPN file with lots of duplicate cues can win
    # purely because its raw alignment score is numerically larger.
    best_language_priority = max(
        (candidate_language_priority(item) for item in selection_pool),
        default=1,
    )
    preferred_language_pool = [
        item
        for item in selection_pool
        if candidate_language_priority(item) == best_language_priority
    ] or selection_pool

    best_alignment = max(
        (alignment_value(item) for item in preferred_language_pool),
        default=float("-inf"),
    )
    tolerance = max(
        max(0.0, srt_tolerance_absolute),
        abs(best_alignment) * max(0.0, srt_tolerance_ratio) if best_alignment != float("-inf") else 0.0,
    )
    near_best = [
        item for item in preferred_language_pool
        if alignment_value(item) >= best_alignment - tolerance
    ] or preferred_language_pool

    def candidate_score(item: tuple[SubtitleCandidate, Path, dict[str, object]]) -> tuple[int, float, float]:
        candidate, _, _ = item
        native_srt = int(prefer_srt and candidate.path.suffix.casefold() == ".srt")
        return native_srt, candidate.score, alignment_value(item)

    primary_item = max(near_best, key=candidate_score)

    def fallback_score(
        item: tuple[SubtitleCandidate, Path, dict[str, object]],
    ) -> tuple[int, int, int, float, int, float]:
        candidate, _, result = item
        exact_identity = int(
            candidate.source == "jimaku"
            and candidate.details.get("episode_match") in {"exact", "range", "absolute"}
            and _candidate_has_exact_anilist_identity(candidate)
        )
        successful = int(bool(result.get("sync_was_successful")))
        native_srt = int(prefer_srt and candidate.path.suffix.casefold() == ".srt")
        return (
            exact_identity,
            successful,
            candidate_language_priority(item),
            alignment_value(item),
            native_srt,
            candidate.score,
        )

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
            "language_purity": (
                candidate.details.get("language_purity")
                if isinstance(candidate.details, dict)
                else None
            ),
            "bilingual_cjk": bool(
                candidate.details.get("bilingual_cjk")
                if isinstance(candidate.details, dict)
                else False
            ),
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
            "best_language_priority": best_language_priority,
            "preferred_language_count": len(preferred_language_pool),
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
